"""L2 — individual large prints.

"Block trade" has a textbook definition: 10,000 shares or $200,000 notional,
the threshold the NYSE has used for decades. In 2026 that is a rounding error
in a mega-cap, which is why `preset` offers larger sizings and why the
%-of-ADV test exists — what makes a print meaningful is its size relative to
the name's normal liquidity, not its absolute size.

A caveat worth stating plainly: with Unusual Whales as the trades provider,
this detector and `dark_pool` read the *same* off-exchange print stream and
differ only in how they threshold it. They cooperate through
``ctx.claimed_prints`` so a single print can't alert twice, but you will get
the clearest results by enabling one of the two.
"""

from __future__ import annotations

from datetime import datetime

from ..models import Alert, Severity, Side, Trade
from .base import Context, Detector, combine_ok, esc, escalate, money, shares


class BlockTradeDetector(Detector):
    name = "block_trades"
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
                f"no saved state for block_trades; only prints from the last "
                f"{ctx.cold_start_minutes} min considered"
            )

        min_shares = float(settings["min_shares"])
        min_notional = float(settings["min_notional"])
        pct_adv = float(settings["min_pct_of_adv"])
        off_only = bool(settings["off_exchange_only"])

        alerts: list[Alert] = []
        newest: datetime | None = None
        for print_ in ctx.prints:
            if print_.ts <= since:
                continue
            newest = print_.ts if newest is None else max(newest, print_.ts)
            if off_only and not print_.is_off_exchange:
                continue

            key = _print_key(print_)
            if key in ctx.claimed_prints:
                continue

            size_ok = print_.size >= min_shares
            value_ok = print_.notional >= min_notional
            if not combine_ok(str(settings["combine"]), size_ok, value_ok):
                continue

            adv_share = None
            if ctx.adv and ctx.adv > 0:
                adv_share = print_.size / ctx.adv * 100
                if pct_adv > 0 and adv_share < pct_adv:
                    continue
            elif pct_adv > 0:
                ctx.note(
                    "average daily volume unavailable, so the block_trades "
                    "min_pct_of_adv test was skipped (needs the bars provider)"
                )

            ctx.claimed_prints.add(key)
            alerts.append(_build_alert(self.name, ctx, print_, adv_share))

        if newest is not None:
            ctx.state.set_watermark(self.name, ctx.ticker, newest)
        return self.finish(ctx, alerts)


def _print_key(print_: Trade) -> str:
    """Identity for a print, so two detectors don't both claim it."""
    return print_.raw_id or f"{print_.ts.isoformat()}|{print_.price}|{print_.size}"


def _build_alert(
    detector: str, ctx: Context, print_: Trade, adv_share: float | None
) -> Alert:
    severity = escalate(
        Severity.MEDIUM,
        print_.notional >= 10_000_000,
        adv_share is not None and adv_share >= 1.0,
    )

    lines = [
        f"<b>{shares(print_.size)}</b> shares at ${print_.price:,.2f} "
        f"= <b>{money(print_.notional)}</b>",
        f"Printed {print_.ts.astimezone().strftime('%H:%M:%S %Z')}"
        + (f" · venue {esc(print_.venue)}" if print_.venue else "")
        + (" · off-exchange" if print_.is_off_exchange else ""),
    ]
    if adv_share is not None:
        lines.append(f"That is <b>{adv_share:.2f}%</b> of average daily volume")
    lines.append(_side_line(print_))

    return Alert(
        ticker=ctx.ticker,
        detector=detector,
        severity=severity,
        headline=f"Large print — {money(print_.notional)}",
        occurred_at=print_.ts,
        lines=lines,
        dedup_parts=(_print_key(print_),),
    )


def _side_line(print_: Trade) -> str:
    """Describe the side honestly: it is inferred, never reported."""
    if print_.side is Side.UNKNOWN:
        return (
            "<i>Side undetermined — the tape carries no buy/sell flag, and this "
            "print gave no usable quote context.</i>"
        )
    context = ""
    if print_.nbbo_bid and print_.nbbo_ask:
        mid = (print_.nbbo_bid + print_.nbbo_ask) / 2
        context = (
            f" (print ${print_.price:,.2f} vs mid ${mid:,.2f}, "
            f"bid ${print_.nbbo_bid:,.2f} / ask ${print_.nbbo_ask:,.2f})"
        )
    return (
        f"<i>Likely <b>{print_.side.value}</b>-initiated{context} — inferred from "
        "the print's position in the spread, not reported.</i>"
    )
