"""L1 — unusual volume, normalised by time of day.

The naive version of this detector (bar volume vs. a flat daily average) fires
on every open, every close and every Friday afternoon lull. The fix is to
compare each bar only against *the same clock minute on previous sessions*:
09:35 is measured against other 09:35s, not against 14:00. Intraday volume
follows a pronounced U-shape, and normalising it away is the single biggest
noise reduction available here.

Two statistics are reported, because analysts quote both:

* **RVOL** — bar volume / normal volume for that slot. Scale-free, intuitive.
* **z-score** — how many standard deviations above that slot's mean. Accounts
  for names whose volume is naturally erratic.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from .. import market_calendar as cal
from ..models import Alert, Bar, Severity
from .base import Context, Detector, combine_ok, escalate, money, shares

INTERVAL_MINUTES = {"1min": 1, "5min": 5, "15min": 15, "30min": 30}

#: Below this many same-slot observations the baseline is not worth trusting.
MIN_BASELINE_SAMPLES = 5


class VolumeAnomalyDetector(Detector):
    name = "volume_anomaly"
    level = "L1"
    requires = "bars"

    def run(self, ctx: Context) -> list[Alert]:
        settings = ctx.settings
        interval = str(settings["bar_interval"])
        step = INTERVAL_MINUTES.get(interval, 5)

        if not ctx.bars:
            ctx.note("no bars returned; volume detector skipped")
            return []
        if not self.cooled_down(ctx):
            return []

        sessions = sorted({b.ts.date() for b in ctx.bars})
        if len(sessions) < 2:
            ctx.note("only one session of bars available; need history for a baseline")
            return []

        today = sessions[-1]
        baseline_days = set(sessions[:-1][-int(settings["baseline_sessions"]) :])
        baseline = _slot_baseline(ctx.bars, baseline_days)

        since, cold = ctx.state.since(
            self.name, ctx.ticker, ctx.now, ctx.cold_start_minutes
        )
        candidates = [
            b
            for b in ctx.bars
            if b.ts.date() == today
            and b.ts > since
            and _is_complete(b, step, ctx.now)
        ]
        if cold and candidates:
            ctx.note(
                f"no saved state for volume_anomaly; only bars from the last "
                f"{ctx.cold_start_minutes} min considered"
            )

        alerts: list[Alert] = []
        newest: datetime | None = None
        for bar in candidates:
            newest = bar.ts if newest is None else max(newest, bar.ts)
            alert = self._test(ctx, bar, baseline, step, interval)
            if alert:
                alerts.append(alert)

        if newest is not None:
            ctx.state.set_watermark(self.name, ctx.ticker, newest)
        return self.finish(ctx, alerts)

    def _test(
        self,
        ctx: Context,
        bar: Bar,
        baseline: dict[tuple[int, int], list[int]],
        step: int,
        interval: str,
    ) -> Alert | None:
        settings = ctx.settings

        into = cal.minutes_into_session(bar.ts)
        if into is None:
            return None  # extended-hours bar; volume baselines don't apply
        if into < int(settings["warmup_minutes"]):
            return None
        remaining = cal.minutes_until_close(bar.ts)
        if remaining is not None and remaining < int(settings["skip_last_minutes"]):
            return None
        if bar.notional < float(settings["min_bar_notional"]):
            return None

        samples = baseline.get((bar.ts.hour, bar.ts.minute), [])
        if len(samples) < MIN_BASELINE_SAMPLES:
            return None

        mean = statistics.fmean(samples)
        if mean <= 0:
            return None
        stdev = statistics.stdev(samples) if len(samples) > 1 else 0.0

        rvol = bar.volume / mean
        zscore = (bar.volume - mean) / stdev if stdev > 0 else None
        move_pct = (bar.close - bar.open) / bar.open * 100 if bar.open else 0.0

        rvol_ok = rvol >= float(settings["rvol_threshold"])
        z_ok = (
            zscore >= float(settings["zscore_threshold"]) if zscore is not None else None
        )
        if not combine_ok(str(settings["combine"]), rvol_ok, z_ok):
            return None

        required_move = float(settings["min_price_move_pct"])
        if required_move > 0 and abs(move_pct) < required_move:
            return None

        # Severity is graded on RVOL, not the z-score. In a name whose volume
        # is very steady the standard deviation is tiny, so even a mild spike
        # scores an enormous z and would pin every alert to HIGH. RVOL is a
        # ratio and stays comparable across tickers; the z-score is still
        # reported, it just doesn't drive the grading.
        ratio = rvol / float(settings["rvol_threshold"])
        severity = escalate(Severity.LOW, ratio >= 2.0, ratio >= 5.0)
        if abs(move_pct) >= 2.0:
            severity = escalate(severity, True)

        direction = "up" if move_pct > 0 else "down" if move_pct < 0 else "flat"
        arrow = {"up": "▲", "down": "▼", "flat": "→"}[direction]
        lines = [
            f"RVOL <b>{rvol:.1f}×</b> normal for {bar.ts:%H:%M} ET"
            + (f" · z-score <b>{zscore:.1f}</b>" if zscore is not None else ""),
            f"Volume {shares(bar.volume)} vs {shares(mean)} typical "
            f"({len(samples)}-session baseline)",
            f"Price {arrow} {move_pct:+.2f}% to ${bar.close:,.2f} · "
            f"~{money(bar.notional)} traded",
        ]
        if direction != "flat":
            lines.append(
                f"<i>Net {direction}ward pressure on the bar — a proxy for side, "
                "not a measured buy/sell split.</i>"
            )

        return Alert(
            ticker=ctx.ticker,
            detector=self.name,
            severity=severity,
            headline=f"Unusual volume — {rvol:.1f}× normal",
            occurred_at=bar.ts,
            lines=lines,
            dedup_parts=(interval, bar.ts.isoformat()),
        )


def _slot_baseline(
    bars: list[Bar], days: set
) -> dict[tuple[int, int], list[int]]:
    """Volume observations grouped by clock slot across the baseline sessions."""
    grouped: dict[tuple[int, int], list[int]] = defaultdict(list)
    for bar in bars:
        if bar.ts.date() in days:
            grouped[(bar.ts.hour, bar.ts.minute)].append(bar.volume)
    return grouped


def _is_complete(bar: Bar, step_minutes: int, now: datetime) -> bool:
    """Only judge bars that have finished forming.

    A partially filled bar always looks quiet and would poison both the alert
    and, on the next run, the watermark.
    """
    return bar.ts + timedelta(minutes=step_minutes) <= now
