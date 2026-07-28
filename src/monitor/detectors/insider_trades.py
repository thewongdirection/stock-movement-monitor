"""SEC Form 4 — known insider transactions.

How analysts actually read these filings, and how that shapes the defaults:

* **Purchases carry the signal.** An insider buying on the open market spent
  their own money. There is one reason to do that.
* **Sales are noisy.** Diversification, a house, a tax bill and a genuine loss
  of confidence all produce the same filing, which is why the default dollar
  floor for sales is five times the one for purchases.
* **10b5-1 sales are close to uninformative.** They were scheduled months
  earlier under a pre-arranged plan, so they are excluded by default.
* **Cluster buys are the strongest documented signal** — several insiders at
  one company buying independently inside a short window. Those escalate to
  high severity.
* **Grants and tax withholding are compensation mechanics**, not decisions, and
  are off by default.

Timing reality: Form 4 is due within two business days *of the trade*. The
alert is fast relative to the filing, but the trade itself is already a few
days old. Every alert states both dates so the lag is visible.
"""

from __future__ import annotations

from datetime import datetime

from ..models import Alert, InsiderTransaction, Severity
from ..providers.sec_edgar import category_for, label_for
from .base import Context, Detector, esc, escalate, money, shares
from .reads import insider_read

SENIOR_TITLE_HINTS = (
    "chief executive",
    "ceo",
    "chief financial",
    "cfo",
    "president",
    "chair",
    "chief operating",
    "coo",
)


class InsiderTradeDetector(Detector):
    name = "insider_trades"
    level = "Form 4"
    requires = "insider"

    def run(self, ctx: Context) -> list[Alert]:
        settings = ctx.settings
        if not ctx.insider_transactions:
            return []

        wanted = set(settings["alert_on"])
        include_derivative = bool(settings["include_derivative"])
        exclude_planned_sales = bool(settings["exclude_10b5_1_sales"])
        floor_buy = float(settings["min_notional_purchase"])
        floor_sell = float(settings["min_notional_sale"])

        newest: datetime | None = None
        reportable: list[tuple[InsiderTransaction, str]] = []

        for txn in sorted(ctx.insider_transactions, key=lambda t: t.filed_at):
            newest = txn.filed_at if newest is None else max(newest, txn.filed_at)

            category = category_for(txn.transaction_code)
            if category not in wanted:
                continue
            if txn.is_derivative and not include_derivative:
                continue
            if category == "sale" and exclude_planned_sales and txn.is_10b5_1:
                continue

            floor = floor_sell if category == "sale" else floor_buy
            # A filing that states no price is still a real transaction, so it
            # bypasses the dollar floor rather than vanishing.
            if txn.value_known and txn.notional < floor:
                continue

            reportable.append((txn, category))

        # Record every buyer before grading any of them. Two insiders whose
        # filings land in the same run are a cluster, and both alerts should
        # say so — not just whichever happened to be processed second.
        window = int(settings["cluster_window_days"])
        buyers_seen = 0
        if any(category == "purchase" for _, category in reportable):
            for txn, category in reportable:
                if category == "purchase":
                    ctx.state.record_insider_buyer(ctx.ticker, txn.insider_name, ctx.now)
            buyers_seen = ctx.state.recent_insider_buyers(ctx.ticker, ctx.now, window)

        alerts = [
            _build_alert(
                self.name,
                ctx,
                txn,
                category,
                buyers_seen if category == "purchase" else 0,
                int(settings["cluster_min_insiders"]),
                window,
            )
            for txn, category in reportable
        ]

        if newest is not None:
            ctx.state.set_watermark(self.name, ctx.ticker, newest)
        return self.finish(ctx, alerts)


def _build_alert(
    detector: str,
    ctx: Context,
    txn: InsiderTransaction,
    category: str,
    cluster_count: int,
    cluster_min: int,
    cluster_window: int,
) -> Alert:
    is_cluster = category == "purchase" and cluster_count >= cluster_min
    senior = any(hint in txn.insider_title.lower() for hint in SENIOR_TITLE_HINTS)

    if category == "purchase":
        base = Severity.MEDIUM
        severity = escalate(
            base, txn.notional >= 1_000_000, is_cluster, senior
        )
        verb = "bought"
    elif category == "sale":
        base = Severity.LOW
        severity = escalate(base, txn.notional >= 5_000_000, senior and txn.notional >= 1_000_000)
        verb = "sold"
    else:
        severity = Severity.LOW
        verb = "reported"

    value = money(txn.notional) if txn.value_known else "value not stated"
    headline = f"Insider {category} — {value}"

    lines = [
        f"<b>{esc(txn.insider_name)}</b> ({esc(txn.role) or 'Insider'}) {verb} "
        f"<b>{shares(txn.shares)}</b> shares"
        + (f" at ${txn.price_per_share:,.2f}" if txn.value_known else ""),
        f"{esc(label_for(txn.transaction_code))} · code "
        f"<code>{esc(txn.transaction_code) or '?'}</code>"
        + (" · derivative line" if txn.is_derivative else ""),
        _timing_line(txn),
    ]

    if txn.shares_owned_after is not None:
        lines.append(f"Holds {shares(txn.shares_owned_after)} shares afterwards")

    if is_cluster:
        lines.append(
            f"🔴 <b>Cluster buy</b> — {cluster_count} distinct insiders have bought "
            f"in the last {cluster_window} days. This is the strongest insider "
            "pattern there is."
        )
    if txn.is_10b5_1:
        lines.append(
            "<i>Flagged as a pre-arranged Rule 10b5-1 plan transaction — decided "
            "months earlier, so it carries little information.</i>"
        )
    if not txn.value_known:
        lines.append(
            "<i>The filing states no price per share (often a weighted-average "
            "fill detailed in a footnote), so no dollar value was applied.</i>"
        )

    return Alert(
        ticker=ctx.ticker,
        detector=detector,
        severity=severity,
        headline=headline,
        occurred_at=txn.filed_at,
        lines=lines,
        url=txn.url,
        read=insider_read(
            is_purchase=category == "purchase",
            cluster_count=cluster_count,
            cluster_min=cluster_min,
            is_ten_pct_owner=txn.is_ten_pct_owner,
            is_officer=txn.is_officer,
            is_director=txn.is_director,
        ),
        dedup_parts=(
            txn.accession,
            txn.transaction_code,
            txn.transaction_date,
            f"{txn.shares:.4f}",
            f"{txn.price_per_share:.4f}",
            "d" if txn.is_derivative else "n",
        ),
    )


def _timing_line(txn: InsiderTransaction) -> str:
    """Make the trade-to-filing lag explicit — it is always non-zero."""
    filed = txn.filed_at.date().isoformat()
    if not txn.transaction_date:
        return f"Filed {filed}"
    try:
        traded = datetime.fromisoformat(txn.transaction_date).date()
    except ValueError:
        return f"Traded {txn.transaction_date} · filed {filed}"
    lag = (txn.filed_at.date() - traded).days
    suffix = "same day" if lag == 0 else f"{lag} day{'s' if lag != 1 else ''} later"
    return f"Traded {txn.transaction_date} · filed {filed} ({suffix})"
