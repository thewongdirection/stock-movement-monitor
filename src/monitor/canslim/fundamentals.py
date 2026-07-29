"""Assembling the fact set the grader scores.

Everything here is best-effort. A missing endpoint costs one letter, not the
whole card — which is why each block is wrapped individually and appends a note
rather than raising. A grader that refuses to say anything because 13F data was
unavailable is less useful than one that grades six letters and tells you which
one it could not reach.

Quarterly comparisons are computed here rather than read from a growth endpoint.
FMP's quarterly growth figures are *sequential*, and CAN SLIM's C requires the
same quarter a year earlier — using the endpoint directly makes every seasonal
business look like it collapses each January.
"""

from __future__ import annotations

import logging

from ..sources.base import SourceError
from .grader import Facts

log = logging.getLogger("monitor.canslim")

#: The index the L and M letters are measured against.
BENCHMARK = "SPY"


def gather(ticker: str, fmp, benchmark: str = BENCHMARK, days: int = 260) -> Facts:
    facts = Facts(ticker=ticker.upper())

    def attempt(label: str, fn) -> None:
        try:
            fn()
        except SourceError as exc:
            facts.notes.append(f"{label} unavailable — {exc.detail}")
            log.info("canslim %s: %s unavailable (%s)", ticker, label, exc.detail)
        except (KeyError, TypeError, ValueError) as exc:
            facts.notes.append(f"{label} was malformed — {exc}")

    def load_quote() -> None:
        quote = fmp.quote(ticker)
        facts.price = _float(quote.get("price"))
        facts.high_52w = _float(quote.get("yearHigh"))
        facts.low_52w = _float(quote.get("yearLow"))
        facts.shares_outstanding = _float(quote.get("sharesOutstanding"))

    def load_quarters() -> None:
        rows = fmp.income_statements(ticker, limit=12, quarterly=True)
        facts.quarters = [
            {"date": row.get("date"), "eps": _float(row.get("epsdiluted") or row.get("eps")),
             "revenue": _float(row.get("revenue"))}
            for row in rows
        ]

    def load_years() -> None:
        rows = fmp.income_statements(ticker, limit=5, quarterly=False)
        facts.years = [
            {"date": row.get("date"), "eps": _float(row.get("epsdiluted") or row.get("eps")),
             "revenue": _float(row.get("revenue"))}
            for row in rows
        ]

    def load_metrics() -> None:
        rows = fmp.key_metrics(ticker, limit=4)
        if rows:
            facts.roe = _float(rows[0].get("roe"))
            facts.debt_to_equity = _float(rows[0].get("debtToEquity"))

    def load_history() -> None:
        rows = fmp.daily_history(ticker, days=days)
        series = list(reversed(rows))                 # FMP returns newest first
        facts.closes = [_float(r.get("close")) or 0.0 for r in series]
        facts.volumes = [int(r.get("volume") or 0) for r in series]

    def load_benchmark() -> None:
        rows = fmp.daily_history(benchmark, days=days)
        facts.index_closes = [_float(r.get("close")) or 0.0 for r in reversed(rows)]

    attempt("quote", load_quote)
    attempt("quarterly statements", load_quarters)
    attempt("annual statements", load_years)
    attempt("key metrics", load_metrics)
    attempt("daily history", load_history)
    attempt(f"{benchmark} history", load_benchmark)

    if facts.institutional_holders is None:
        facts.notes.append(
            "Institutional sponsorship (I) needs 13F holder counts, which the "
            "monitor does not fetch — the grader skill covers it interactively."
        )
    return facts


def _float(value) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
