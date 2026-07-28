"""L3 — unusual options flow.

The conventional filters, and why each one is there:

* **$100k premium floor** — the usual dividing line between retail noise and
  size worth looking at.
* **volume > open interest** — implies the position is being *opened* rather
  than existing contracts changing hands. This is the single most useful
  unusual-flow filter.
* **sweeps** — an order filled across several exchanges at once, which is what
  someone in a hurry to get filled looks like.
* **exclude deep in-the-money** — deep ITM size is frequently a stock
  substitute or an assignment mechanic, not a directional bet.
* **cap days-to-expiry** — long-dated LEAPS flow is usually hedging.

Note that big call premium is not automatically bullish: it could equally be a
hedge, or a call sold against stock. Where the data does not say who initiated,
the alert says so rather than guessing a direction.
"""

from __future__ import annotations

from datetime import datetime

from ..models import Alert, OptionTrade, Severity, Side
from .base import Context, Detector, esc, escalate, money
from .reads import options_flow_read


class OptionsFlowDetector(Detector):
    name = "options_flow"
    level = "L3"
    requires = "flow"

    def run(self, ctx: Context) -> list[Alert]:
        settings = ctx.settings
        if not ctx.option_trades:
            return []
        if not self.cooled_down(ctx):
            return []

        since, cold = ctx.state.since(
            self.name, ctx.ticker, ctx.now, ctx.cold_start_minutes
        )
        if cold:
            ctx.note(
                f"no saved state for options_flow; only trades from the last "
                f"{ctx.cold_start_minutes} min considered"
            )

        min_premium = float(settings["min_premium"])
        allowed_types = set(settings["trade_types"])
        require_vol_gt_oi = bool(settings["require_volume_gt_oi"])
        min_dte, max_dte = int(settings["min_dte"]), int(settings["max_dte"])
        deep_itm = float(settings["exclude_deep_itm_pct"])

        alerts: list[Alert] = []
        newest: datetime | None = None
        for trade in ctx.option_trades:
            if trade.ts <= since:
                continue
            newest = trade.ts if newest is None else max(newest, trade.ts)

            if trade.premium < min_premium:
                continue
            # An unrecognised execution style is kept: better a mislabelled
            # alert than a silently dropped $2M sweep.
            if trade.trade_type not in allowed_types and trade.trade_type != "unknown":
                continue
            if require_vol_gt_oi:
                if trade.volume is None or trade.open_interest is None:
                    ctx.note(
                        "flow rows lack volume/open-interest, so "
                        "require_volume_gt_oi could not be applied"
                    )
                elif trade.volume <= trade.open_interest:
                    continue

            dte = trade.dte
            if dte is not None and not (min_dte <= dte <= max_dte):
                continue

            moneyness = trade.moneyness_pct
            if deep_itm > 0 and moneyness is not None and moneyness > deep_itm:
                continue

            severity = escalate(
                Severity.MEDIUM,
                trade.premium >= min_premium * 10,
                trade.trade_type == "sweep",
                dte is not None and dte <= 7 and trade.premium >= min_premium * 3,
            )
            alerts.append(_build_alert(self.name, ctx, trade, severity))

        if newest is not None:
            ctx.state.set_watermark(self.name, ctx.ticker, newest)
        return self.finish(ctx, alerts)


def _build_alert(
    detector: str, ctx: Context, trade: OptionTrade, severity: Severity
) -> Alert:
    kind = trade.option_type.upper()
    expiry_bit = trade.expiry
    dte = trade.dte
    if dte is not None:
        expiry_bit += f" ({dte}d)"

    lines = [
        f"<b>{money(trade.premium)}</b> premium · {kind} ${trade.strike:,.2f} "
        f"exp {expiry_bit}",
        f"{trade.size:,} contracts"
        + (f" · {esc(trade.trade_type)}" if trade.trade_type != "unknown" else ""),
    ]

    if trade.volume is not None and trade.open_interest is not None:
        ratio = (
            f" ({trade.volume / trade.open_interest:.1f}× OI)"
            if trade.open_interest
            else " (no prior OI)"
        )
        lines.append(
            f"Volume {trade.volume:,} vs open interest {trade.open_interest:,}{ratio}"
            + (
                " — consistent with a newly opened position"
                if trade.volume > trade.open_interest
                else ""
            )
        )

    if trade.underlying_price:
        moneyness = trade.moneyness_pct
        state = "ITM" if (moneyness or 0) > 0 else "OTM"
        lines.append(
            f"Underlying ${trade.underlying_price:,.2f} · "
            f"{abs(moneyness or 0):.1f}% {state}"
        )

    if trade.side is Side.UNKNOWN:
        lines.append(
            f"<i>Direction not stated in the feed. Large {kind.lower()} premium is "
            "not automatically directional — it may be a hedge or written against "
            "stock.</i>"
        )
    else:
        lines.append(
            f"<i>Reported as {trade.side.value}-side (opening aggressor).</i>"
        )

    return Alert(
        ticker=ctx.ticker,
        detector=detector,
        severity=severity,
        headline=f"Options flow — {money(trade.premium)} {kind}",
        occurred_at=trade.ts,
        lines=lines,
        read=options_flow_read(
            kind,
            trade.moneyness_pct,
            dte,
            (trade.volume / trade.open_interest)
            if trade.volume and trade.open_interest
            else None,
        ),
        dedup_parts=(
            trade.raw_id
            or f"{trade.ts.isoformat()}|{kind}|{trade.strike}|{trade.expiry}|{trade.premium:.0f}",
        ),
    )
