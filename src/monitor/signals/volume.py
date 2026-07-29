"""Unusual volume for the time of day.

The most frequent signal here and the least conclusive, which is why its read
never claims a direction it cannot support. It answers one question — "is more
being traded right now than normally is at this hour?" — and leaves what that
means to the price action alongside it.

Measured against thirteen sessions of real 30-minute bars, `rvol_threshold` is
the binding constraint by a wide margin: sweeping `zscore_threshold` from 1.5 to
4.0 changed nothing, because every bar that cleared RVOL also cleared z. That is
worth knowing before tuning — the z-score is a guard against a distorted median,
not a second opinion.
"""

from __future__ import annotations

from datetime import timedelta

from ..clock import session_for, slot_of, to_et
from ..models import Alert, Bar, Severity, humanise, money
from .base import Baseline, Context, build_baseline
from .reads import VOLUME_CAVEATS, volume_read


class VolumeSignal:
    name = "volume"

    def evaluate(self, ctx: Context) -> list[Alert]:
        if not ctx.bars:
            return []

        interval = ctx.config.get("poll.bar_minutes")
        max_age = timedelta(minutes=ctx.config.get("poll.max_bar_age_minutes"))
        min_samples = ctx.config.get("poll.min_baseline_samples")

        alerts: list[Alert] = []
        for bar in _candidates(ctx, interval, max_age):
            baseline = build_baseline(ctx.bars, bar, interval, min_samples)
            if baseline is None:
                continue
            alert = self._judge(ctx, bar, baseline, interval)
            if alert is not None:
                alerts.append(alert)
        return alerts

    def _judge(self, ctx: Context, bar: Bar, baseline: Baseline, interval: int) -> Alert | None:
        rvol = baseline.rvol(bar.volume)
        zscore = baseline.zscore(bar.volume)
        if rvol is None:
            return None

        rvol_min = ctx.setting("signals.volume.rvol_threshold")
        z_min = ctx.setting("signals.volume.zscore_threshold")
        move_min = ctx.setting("signals.volume.min_price_move_pct")
        notional_min = ctx.setting("signals.volume.min_notional")
        combine = ctx.setting("signals.volume.combine")

        move = bar.move_pct
        checks = {
            "rvol": rvol >= rvol_min,
            # A flat standard deviation means every sample was identical, which
            # is not evidence against an anomaly — treat it as "no opinion"
            # rather than a failed check.
            "zscore": zscore is None or zscore >= z_min,
            "move": abs(move) >= move_min,
        }
        met = sum(1 for ok in checks.values() if ok)
        triggered = all(checks.values()) if combine == "all" else any(checks.values())
        if not triggered or bar.notional < notional_min:
            return None

        session = session_for(bar.ts.date())
        near_close = bool(session and session.close_at - bar.ts <= timedelta(minutes=60))

        severity = Severity.MEDIUM
        if rvol >= rvol_min * 2 or (checks["move"] and abs(move) >= max(move_min, 0.5) * 3):
            severity = Severity.HIGH
        elif combine == "any" and met < 2:
            severity = Severity.LOW

        slot = slot_of(bar.ts, interval)
        direction = "surged" if move >= 0.3 else "fell" if move <= -0.3 else "churned"
        facts = [
            f"Volume {humanise(bar.volume)} vs {humanise(baseline.median)} median "
            f"for the {slot} slot — {rvol:.1f}x",
            f"Price {bar.open:,.2f} → {bar.close:,.2f} ({move:+.2f}%)",
            f"Notional {money(bar.notional)} in the {interval}-minute bar",
            f"Baseline from {baseline.samples} prior sessions at the same slot",
        ]
        if zscore is not None:
            facts.insert(1, f"Z-score {zscore:+.1f} against the same-slot mean")
        if combine == "any":
            passed = ", ".join(k for k, ok in checks.items() if ok)
            facts.append(f"Triggered on {passed} (combine: any)")

        return Alert(
            ticker=ctx.ticker,
            signal=self.name,
            severity=severity,
            headline=(
                f"{ctx.ticker} {direction} on {rvol:.1f}x normal {slot} volume "
                f"({move:+.2f}%)"
            ),
            occurred_at=bar.ts,
            facts=facts,
            read=volume_read(move, rvol, near_close),
            caveats=list(VOLUME_CAVEATS),
            identity=(bar.ts.isoformat(),),
        )


def _candidates(ctx: Context, interval: int, max_age: timedelta) -> list[Bar]:
    """Closed bars recent enough to still be worth reporting.

    Two constraints, both load-bearing. A bar that has not closed yet is missing
    part of its volume, and reporting it would understate every RVOL and then
    contradict itself on the next poll. A bar older than `max_bar_age_minutes`
    is history — telling you about it now would be worse than silence, because
    it reads as something happening rather than something that happened.
    """
    now = to_et(ctx.now)
    span = timedelta(minutes=interval)
    return [
        bar for bar in ctx.bars
        if bar.ts + span <= now and now - bar.ts <= max_age
    ]
