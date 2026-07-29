"""Form 4 filings — the one place direction is stated rather than inferred.

Two design choices carry most of the value here.

**Planned sales are suppressed by default.** A 10b5-1 plan is adopted months
before it executes, precisely so the insider cannot be accused of trading on
what they know. Reporting those sales as signal would bury the handful of
discretionary trades that mean something under a steady drip of scheduled ones.

**Clusters are tracked across polls.** Two officers buying three weeks apart is
the single most informative insider pattern available, and no individual poll
can see it — the first purchase is long gone by the time the second is filed.
That is what `Store.record_insider_buyer` is for.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..models import Alert, InsiderTrade, Severity, escalate, humanise, money
from .base import Context
from .reads import INSIDER_CAVEATS, insider_read

#: Titles where a purchase carries the most weight, because these people see
#: the numbers before anyone else does.
SENIOR = ("chief executive", "ceo", "chief financial", "cfo", "president", "chairman")


class InsiderSignal:
    name = "insider"

    def evaluate(self, ctx: Context) -> list[Alert]:
        if not ctx.filings:
            return []

        min_value = ctx.setting("signals.insider.min_value")
        include_planned = ctx.setting("signals.insider.include_planned_sales")
        window_days = ctx.setting("signals.insider.cluster_window_days")
        min_buyers = ctx.setting("signals.insider.cluster_min_buyers")

        alerts: list[Alert] = []
        purchases: list[InsiderTrade] = []

        for trade in sorted(ctx.filings, key=lambda t: t.filed_at, reverse=True):
            if trade.value < min_value:
                continue
            if trade.is_open_market_sale and trade.planned_10b5_1 and not include_planned:
                continue
            if trade.is_purchase:
                purchases.append(trade)
                ctx.store.record_insider_buyer(
                    ctx.ticker, trade.insider, trade.traded_on, trade.value
                )
            alerts.append(self._single(ctx, trade, min_value))

        cluster = self._cluster(ctx, purchases, window_days, min_buyers)
        if cluster is not None:
            alerts.insert(0, cluster)
        return alerts

    # -- one filing ---------------------------------------------------------
    def _single(self, ctx: Context, trade: InsiderTrade, min_value: float) -> Alert:
        senior = any(word in (trade.title or "").lower() for word in SENIOR)

        if trade.is_purchase:
            severity = escalate(
                Severity.MEDIUM, senior, trade.value >= min_value * 10
            )
            verb = "bought"
        elif trade.planned_10b5_1:
            severity = Severity.LOW
            verb = "sold (10b5-1 plan)"
        else:
            severity = escalate(Severity.LOW, senior or trade.value >= min_value * 10)
            verb = "sold"

        facts = [
            f"{trade.insider} — {trade.title or 'insider'}",
            f"{verb.capitalize()} {humanise(trade.shares)} shares at "
            f"${trade.price:,.2f} — {money(trade.value)}",
            f"Traded {trade.traded_on}, filed {trade.filed_at:%Y-%m-%d} "
            f"({trade.filing_lag_days} day{'s' if trade.filing_lag_days != 1 else ''} later)",
        ]
        if trade.shares_after is not None:
            # `sharesOwnedFollowingTransaction` is post-trade, so a purchase is
            # measured against the new position and a sale against the old one.
            # Using one formula for both would overstate every sale.
            if trade.is_purchase and trade.shares_after > 0:
                share = trade.shares / trade.shares_after * 100
                facts.append(
                    f"Holds {humanise(trade.shares_after)} shares afterwards — "
                    f"this trade was {share:.0f}% of it"
                )
            else:
                before = trade.shares_after + trade.shares
                share = trade.shares / before * 100 if before else 0.0
                facts.append(
                    f"Holds {humanise(trade.shares_after)} shares afterwards — "
                    f"sold {share:.0f}% of the prior position"
                )
        roles = [label for label, flag in (("officer", trade.is_officer),
                                          ("director", trade.is_director),
                                          ("10% owner", trade.is_ten_percent)) if flag]
        if roles:
            facts.append("Filed as " + ", ".join(roles))

        return Alert(
            ticker=ctx.ticker,
            signal=self.name,
            severity=severity,
            headline=f"{ctx.ticker} insider {verb} — {money(trade.value)} by {trade.insider}",
            occurred_at=trade.filed_at,
            facts=facts,
            read=insider_read(trade),
            caveats=list(INSIDER_CAVEATS),
            url=trade.url,
            identity=(trade.accession, trade.code, trade.traded_on, f"{trade.shares:g}"),
        )

    # -- several buyers -----------------------------------------------------
    def _cluster(self, ctx: Context, purchases: list[InsiderTrade],
                 window_days: int, min_buyers: int) -> Alert | None:
        if not purchases:
            return None
        since = (ctx.now.date() - timedelta(days=window_days)).isoformat()
        rows = ctx.store.insider_buyers_since(ctx.ticker, since)
        buyers = {row["insider"]: row for row in rows}
        if len(buyers) < min_buyers:
            return None

        total = sum(float(row["value"]) for row in buyers.values())
        newest = max(purchases, key=lambda t: t.traded_on)
        facts = [
            f"{name} — {money(float(row['value']))} on {row['traded_on']}"
            for name, row in sorted(buyers.items(), key=lambda kv: kv[1]["traded_on"], reverse=True)
        ]
        facts.append(f"{len(buyers)} distinct buyers totalling {money(total)} "
                     f"within {window_days} days")

        return Alert(
            ticker=ctx.ticker,
            signal=self.name,
            severity=Severity.HIGH,
            headline=f"{ctx.ticker} insider cluster — {len(buyers)} buyers, {money(total)}",
            occurred_at=newest.filed_at,
            facts=facts,
            read=insider_read(newest, cluster_size=len(buyers)),
            caveats=list(INSIDER_CAVEATS),
            # Keyed on the buyer set, so a third buyer joining is new news while
            # a re-poll of the same two is not.
            identity=("cluster", *sorted(buyers)),
        )
