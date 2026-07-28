"""L2 — dark pool / off-exchange prints, thresholded on value and liquidity share.

Where `block_trades` thinks in share counts (the traditional block definition),
this thinks in dollars and in percentage of average daily volume, which is how
off-exchange activity is normally assessed.

What off-exchange data can and cannot tell you: prints reach the consolidated
tape through a FINRA trade reporting facility, so you learn a trade happened
away from the lit exchanges, its size and its price — but *not which venue*.
Per-ATS attribution is published by FINRA separately, weekly, with a multi-week
lag. Anything claiming to name the pool in real time is inferring.
"""

from __future__ import annotations

from datetime import datetime

from ..models import Alert, Severity
from .base import Context, Detector, combine_ok, escalate, money, shares
from .block_trades import _print_key, _side_line
from .reads import dark_pool_read


class DarkPoolDetector(Detector):
    name = "dark_pool"
    level = "L2"
    requires = "trades"

    def run(self, ctx: Context) -> list[Alert]:
        settings = ctx.settings
        if not ctx.prints:
            return []
        if not self.cooled_down(ctx):
            return []

        since, cold = ctx.state.since(
            self.name, ctx.ticker, ctx.now, ctx.cold_start_minutes
        )
        if cold:
            ctx.note(
                f"no saved state for dark_pool; only prints from the last "
                f"{ctx.cold_start_minutes} min considered"
            )

        min_notional = float(settings["min_notional"])
        pct_adv = float(settings["min_pct_of_adv"])
        warned_adv = False

        alerts: list[Alert] = []
        newest: datetime | None = None
        for print_ in ctx.prints:
            if print_.ts <= since:
                continue
            newest = print_.ts if newest is None else max(newest, print_.ts)
            if not print_.is_off_exchange:
                continue

            key = _print_key(print_)
            if key in ctx.claimed_prints:
                continue

            value_ok = print_.notional >= min_notional
            adv_share = None
            adv_ok: bool | None = None
            if ctx.adv and ctx.adv > 0:
                adv_share = print_.size / ctx.adv * 100
                adv_ok = adv_share >= pct_adv if pct_adv > 0 else None
            elif pct_adv > 0 and not warned_adv:
                warned_adv = True
                ctx.note(
                    "average daily volume unavailable, so the dark_pool "
                    "min_pct_of_adv test was skipped (needs the bars provider)"
                )

            if not combine_ok(str(settings["combine"]), value_ok, adv_ok):
                continue

            ctx.claimed_prints.add(key)
            severity = escalate(
                Severity.MEDIUM,
                print_.notional >= min_notional * 5,
                adv_share is not None and adv_share >= 1.0,
            )
            lines = [
                f"<b>{shares(print_.size)}</b> shares at ${print_.price:,.2f} "
                f"= <b>{money(print_.notional)}</b> off-exchange",
                f"Printed {print_.ts.astimezone().strftime('%H:%M:%S %Z')}",
            ]
            if adv_share is not None:
                lines.append(f"<b>{adv_share:.2f}%</b> of average daily volume")
            lines.append(_side_line(print_))
            lines.append(
                "<i>Venue is not disclosed on the tape — off-exchange only. "
                "FINRA publishes per-ATS detail weekly, well after the fact.</i>"
            )

            alerts.append(
                Alert(
                    ticker=ctx.ticker,
                    detector=self.name,
                    severity=severity,
                    headline=f"Dark pool print — {money(print_.notional)}",
                    occurred_at=print_.ts,
                    lines=lines,
                    read=dark_pool_read(adv_share, print_.notional),
                    dedup_parts=(key,),
                )
            )

        if newest is not None:
            ctx.state.set_watermark(self.name, ctx.ticker, newest)
        return self.finish(ctx, alerts)
