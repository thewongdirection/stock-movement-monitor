"""Single prints large enough that a person had to authorise them.

A block is one trade, not a busy minute. That distinction is the whole value:
five hundred thousand shares crossing in one print means an institution moved a
position deliberately, where the same volume spread over an hour could be
anything.

This signal needs individual trade prints, which is why it ships disabled. FMP
and the IBKR gateway both stop at bars, and there is no honest way to recover a
block from an OHLCV candle — a heavy bar with a narrow range is *suggestive* of
a cross, and acting on that suggestion would mean labelling an inference as an
observation. Point `sources.trades` at captured tick data and it turns on.

Note also that "off-exchange" is not "dark pool" in the way the phrase is
usually meant. Off-exchange prints are reported through a FINRA TRF and include
internalised retail flow, which is the opposite of institutional intent.
"""

from __future__ import annotations

from datetime import timedelta

from ..models import Alert, Severity, Side, Trade, escalate, humanise, money
from .base import Context
from .reads import BLOCK_CAVEATS, block_read


class BlockSignal:
    name = "blocks"

    def evaluate(self, ctx: Context) -> list[Alert]:
        if not ctx.trades:
            return []

        min_shares = ctx.setting("signals.blocks.min_shares")
        min_notional = ctx.setting("signals.blocks.min_notional")
        inst_shares = ctx.setting("signals.blocks.institutional_shares")
        inst_notional = ctx.setting("signals.blocks.institutional_notional")
        mega_shares = ctx.setting("signals.blocks.mega_shares")
        mega_notional = ctx.setting("signals.blocks.mega_notional")
        off_only = ctx.setting("signals.blocks.off_exchange_only")
        cap = ctx.setting("signals.blocks.max_per_ticker")
        max_age = timedelta(minutes=ctx.config.get("poll.max_bar_age_minutes"))

        candidates = [
            trade for trade in ctx.trades
            if (not off_only or trade.off_exchange)
            and ctx.now - trade.ts <= max_age
            # Either leg qualifies: 10,000 shares of a $4 stock and 500 shares
            # of a $900 stock are both blocks, by different definitions.
            and (trade.size >= min_shares or trade.notional >= min_notional)
        ]
        candidates.sort(key=lambda t: t.notional, reverse=True)

        alerts = []
        for trade in candidates[:cap]:
            # Qualifying uses OR so both a cheap stock and an expensive one can
            # produce a block. Escalating uses AND, because with OR the dollar
            # leg swallows everything: at $205 a share, 24,400 shares already
            # clears the $5M "mega" threshold, so every institutional print in a
            # large-cap name would arrive as the loudest severity there is.
            severity = escalate(
                Severity.LOW,
                trade.size >= inst_shares and trade.notional >= inst_notional,
                trade.size >= mega_shares and trade.notional >= mega_notional,
            )
            alerts.append(Alert(
                ticker=ctx.ticker,
                signal=self.name,
                severity=severity,
                headline=(
                    f"{ctx.ticker} block print — {humanise(trade.size)} shares, "
                    f"{money(trade.notional)}"
                ),
                occurred_at=trade.ts,
                facts=_facts(trade, inst_shares, mega_shares),
                read=block_read(trade.notional, trade.size,
                                trade.off_exchange, trade.side.value),
                caveats=list(BLOCK_CAVEATS),
                identity=(trade.ref or f"{trade.ts.isoformat()}|{trade.size}|{trade.price}",),
            ))
        return alerts


def _facts(trade: Trade, inst_shares: int, mega_shares: int) -> list[str]:
    if trade.size >= mega_shares:
        size_class = "mega block"
    elif trade.size >= inst_shares:
        size_class = "institutional block"
    else:
        size_class = "block"

    facts = [
        f"{humanise(trade.size)} shares at ${trade.price:,.2f} — {money(trade.notional)} ({size_class})",
        f"Printed {trade.ts:%H:%M:%S} ET"
        + (f" via {trade.venue}" if trade.venue else ""),
    ]
    facts.append(
        "Reported off-exchange (FINRA TRF) — includes both dark-pool crosses and "
        "internalised retail flow"
        if trade.off_exchange else "Printed on a lit exchange"
    )
    facts.append(
        f"Quote-inferred side: {trade.side.value}"
        if trade.side is not Side.UNKNOWN
        else "Side unknown — the consolidated tape carries no aggressor flag"
    )
    return facts
