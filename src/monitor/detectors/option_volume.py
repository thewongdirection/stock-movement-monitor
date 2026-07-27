"""L3-lite — the whole option chain is busier than normal.

Where `options_flow` needs a paid feed to say *"someone paid $2M for August
190 calls"*, this says only *"today's option volume in this name is 3x its
average, skewed to calls"*. Much coarser — no strike, no premium, no direction
— but it comes from IBKR's underlying option-volume fields, so it costs nothing
extra and works without an options-flow subscription.

Read it as confirmation rather than a signal on its own. Elevated chain volume
around an earnings date or an index rebalance is routine, which is why the
put/call skew is reported alongside the ratio: a 3x day that is 90% calls says
something a 3x day split evenly does not.

One structural limit worth knowing: these are *day-cumulative* figures, so the
ratio only becomes meaningful once enough of the session has elapsed to compare
against a full-day average. `min_session_pct` handles that — without it, every
morning would look quiet and every afternoon busy.
"""

from __future__ import annotations

from .. import market_calendar as cal
from ..models import Alert, Severity
from .base import Context, Detector, escalate


class OptionVolumeDetector(Detector):
    name = "option_volume"
    level = "L3-lite"
    requires = "option_volume"

    def run(self, ctx: Context) -> list[Alert]:
        settings = ctx.settings
        snap = ctx.option_volume
        if not snap or snap.ratio is None:
            return []
        if not self.cooled_down(ctx):
            return []

        # Compare like with like: a day-cumulative figure against a full-day
        # average is meaningless an hour into the session.
        elapsed = _session_fraction(ctx)
        floor = float(settings["min_session_pct"]) / 100.0
        if elapsed is None:
            return []
        if elapsed < floor:
            ctx.note(
                f"option_volume held back — only {elapsed * 100:.0f}% of the session "
                f"has elapsed, below min_session_pct={settings['min_session_pct']:.0f}%"
            )
            return []

        # Pro-rate the average to the part of the session that has actually run,
        # so a genuine 3x at 11am is not hidden until the close.
        projected_ratio = snap.ratio / elapsed
        threshold = float(settings["min_ratio"])
        if projected_ratio < threshold:
            return []

        total = (snap.today_volume or 0)
        if total < float(settings["min_contracts"]):
            return []

        skew = snap.call_put_skew
        severity = escalate(
            Severity.LOW,
            projected_ratio >= threshold * 2,
            skew is not None and (skew >= 3.0 or skew <= 0.33),
        )

        lines = [
            f"Option volume <b>{projected_ratio:.1f}×</b> its average "
            f"(pace-adjusted; {elapsed * 100:.0f}% of the session elapsed)",
            f"{total:,.0f} contracts today vs {snap.average_volume:,.0f} typical",
        ]
        if snap.call_volume is not None and snap.put_volume is not None:
            lines.append(
                f"Calls {snap.call_volume:,.0f} · puts {snap.put_volume:,.0f}"
                + (f" ({skew:.1f}:1 call/put)" if skew else "")
            )
        lines.append(
            "<i>Chain-level activity only — no strike, premium or direction. "
            "Treat as confirmation, and check for an earnings date before "
            "reading anything into it.</i>"
        )

        return self.finish(
            ctx,
            [
                Alert(
                    ticker=ctx.ticker,
                    detector=self.name,
                    severity=severity,
                    headline=f"Unusual option volume — {projected_ratio:.1f}× normal",
                    occurred_at=ctx.now,
                    lines=lines,
                    # One per ticker per session: it's a cumulative daily figure,
                    # so re-alerting as it climbs would just be the same fact.
                    dedup_parts=(ctx.now.astimezone(cal.ET).date().isoformat(),),
                )
            ],
        )


def _session_fraction(ctx: Context) -> float | None:
    """How much of the regular session has elapsed, as a fraction."""
    now_et = ctx.now.astimezone(cal.ET)
    into = cal.minutes_into_session(now_et)
    if into is None:
        return None
    close = cal.session_close(now_et.date())
    total = (close.hour * 60 + close.minute) - (
        cal.REGULAR_OPEN.hour * 60 + cal.REGULAR_OPEN.minute
    )
    if total <= 0:
        return None
    return min(1.0, max(0.0, into / total))
