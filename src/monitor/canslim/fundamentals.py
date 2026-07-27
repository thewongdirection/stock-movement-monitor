"""Fundamental inputs for the C, A and I letters.

Sourced from FMP, following the skill's ladder position for it. Two traps the
skill's data guide calls out explicitly, both handled here:

1. **Quarterly growth must be a same-quarter year-over-year compare.** FMP's
   ``income-statement-growth`` at quarterly period is *sequential*
   quarter-on-quarter, which is not what CAN SLIM's C asks for and would flag
   every seasonal business wrongly. So quarterly statements are pulled raw and
   each quarter compared to the one four rows back.
2. **Quarterly statements are plan-gated on lower FMP tiers.** When the call is
   denied, the letter is marked unknown with the reason rather than scored on
   absent data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..providers.base import ProviderError, RateLimitedSession

log = logging.getLogger(__name__)


@dataclass
class Quarter:
    label: str
    eps: float | None
    revenue: float | None
    eps_growth_yoy: float | None = None
    revenue_growth_yoy: float | None = None


@dataclass
class AnnualYear:
    label: str
    eps: float | None
    revenue: float | None
    eps_growth: float | None = None
    roe: float | None = None


@dataclass
class Fundamentals:
    ticker: str
    company: str = ""
    sector: str = ""
    industry: str = ""
    price: float | None = None
    market_cap: float | None = None
    beta: float | None = None
    shares_outstanding: float | None = None
    pe_ratio: float | None = None
    eps_ttm: float | None = None
    dividend_yield: float | None = None
    next_earnings: str | None = None

    quarters: list[Quarter] = field(default_factory=list)
    years: list[AnnualYear] = field(default_factory=list)
    latest_roe: float | None = None
    debt_to_equity: float | None = None
    institutional_holders: int | None = None
    institutional_change: int | None = None

    #: Human-readable reasons a field is missing, surfaced in the report.
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    @property
    def has_quarterly(self) -> bool:
        return any(q.eps_growth_yoy is not None for q in self.quarters)

    @property
    def has_annual(self) -> bool:
        return len([y for y in self.years if y.eps is not None]) >= 2


class FMPFundamentals:
    """Fetch the CAN SLIM fundamental inputs, degrading rather than failing."""

    def __init__(self, api_key: str, base_url: str, timeout: int = 30):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.http = RateLimitedSession(min_interval=0.3, timeout=timeout)

    def _get(self, path: str, **params: Any) -> Any:
        return self.http.get_json(
            f"{self.base_url}{path}", params={**params, "apikey": self.api_key}
        )

    def fetch(self, ticker: str) -> Fundamentals:
        out = Fundamentals(ticker=ticker.upper())
        if not self.api_key:
            out.warn("no FMP_API_KEY set — C, A and I could not be graded on data")
            return out

        self._profile(ticker, out)
        self._quarterly(ticker, out)
        self._annual(ticker, out)
        self._ownership(ticker, out)
        self._calendar(ticker, out)
        return out

    # -- pieces -----------------------------------------------------------
    def _profile(self, ticker: str, out: Fundamentals) -> None:
        try:
            rows = self._get(f"/api/v3/profile/{ticker}")
        except ProviderError as exc:
            out.warn(f"company profile unavailable ({_short(exc)})")
            return
        row = rows[0] if isinstance(rows, list) and rows else {}
        if not isinstance(row, dict):
            return
        out.company = str(row.get("companyName") or "")
        out.sector = str(row.get("sector") or "")
        out.industry = str(row.get("industry") or "")
        out.price = _num(row.get("price"))
        out.market_cap = _num(row.get("mktCap"))
        out.beta = _num(row.get("beta"))
        out.dividend_yield = _num(row.get("lastDiv"))

    def _quarterly(self, ticker: str, out: Fundamentals) -> None:
        try:
            rows = self._get(
                f"/api/v3/income-statement/{ticker}", period="quarter", limit=12
            )
        except ProviderError as exc:
            out.warn(
                "quarterly income statements unavailable "
                f"({_short(exc)}) — C is graded as unknown. This endpoint is "
                "plan-gated on lower FMP tiers."
            )
            return
        if not isinstance(rows, list) or not rows:
            out.warn("FMP returned no quarterly statements — C is graded as unknown")
            return

        # Newest first from FMP; keep that order and look 4 rows back for the
        # same quarter a year earlier. This is the compare CAN SLIM's C wants;
        # a sequential quarter-on-quarter figure would misjudge any seasonal name.
        quarters: list[Quarter] = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            eps = _num(row.get("epsdiluted")) or _num(row.get("eps"))
            revenue = _num(row.get("revenue"))
            label = str(row.get("period") or "") + " " + str(row.get("calendarYear") or "")
            quarter = Quarter(label=label.strip() or str(row.get("date", "")), eps=eps, revenue=revenue)
            year_ago = rows[idx + 4] if idx + 4 < len(rows) else None
            if isinstance(year_ago, dict):
                prior_eps = _num(year_ago.get("epsdiluted")) or _num(year_ago.get("eps"))
                prior_rev = _num(year_ago.get("revenue"))
                quarter.eps_growth_yoy = _growth(eps, prior_eps)
                quarter.revenue_growth_yoy = _growth(revenue, prior_rev)
            quarters.append(quarter)
        out.quarters = quarters[:8]
        if not out.has_quarterly:
            out.warn(
                "fewer than five quarters of history — no year-over-year compare "
                "was possible for C"
            )

    def _annual(self, ticker: str, out: Fundamentals) -> None:
        try:
            rows = self._get(
                f"/api/v3/income-statement/{ticker}", period="annual", limit=5
            )
        except ProviderError as exc:
            out.warn(f"annual income statements unavailable ({_short(exc)}) — A is unknown")
            rows = []
        years: list[AnnualYear] = []
        if isinstance(rows, list):
            for idx, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                eps = _num(row.get("epsdiluted")) or _num(row.get("eps"))
                year = AnnualYear(
                    label=str(row.get("calendarYear") or row.get("date", ""))[:4],
                    eps=eps,
                    revenue=_num(row.get("revenue")),
                )
                prior = rows[idx + 1] if idx + 1 < len(rows) else None
                if isinstance(prior, dict):
                    year.eps_growth = _growth(
                        eps, _num(prior.get("epsdiluted")) or _num(prior.get("eps"))
                    )
                years.append(year)
        out.years = years

        try:
            metrics = self._get(f"/api/v3/key-metrics/{ticker}", period="annual", limit=5)
        except ProviderError as exc:
            out.warn(f"key metrics unavailable ({_short(exc)}) — ROE unknown for A")
            return
        if isinstance(metrics, list) and metrics and isinstance(metrics[0], dict):
            out.latest_roe = _pct(metrics[0].get("roe"))
            out.debt_to_equity = _num(metrics[0].get("debtToEquity"))
            out.pe_ratio = _num(metrics[0].get("peRatio"))
            for year, metric in zip(out.years, metrics):
                if isinstance(metric, dict):
                    year.roe = _pct(metric.get("roe"))

    def _ownership(self, ticker: str, out: Fundamentals) -> None:
        """Institutional sponsorship for I. Frequently plan-gated."""
        try:
            rows = self._get(
                "/api/v4/institutional-ownership/symbol-ownership",
                symbol=ticker,
                includeCurrentQuarter="true",
            )
        except ProviderError as exc:
            out.warn(
                f"institutional ownership unavailable ({_short(exc)}) — I is graded "
                "on what the price action implies, not on holder counts"
            )
            return
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return
        current = rows[0]
        out.institutional_holders = _int(current.get("investorsHolding"))
        change = _int(current.get("investorsHoldingChange"))
        if change is None and len(rows) > 1 and isinstance(rows[1], dict):
            previous = _int(rows[1].get("investorsHolding"))
            if out.institutional_holders is not None and previous is not None:
                change = out.institutional_holders - previous
        out.institutional_change = change

    def _calendar(self, ticker: str, out: Fundamentals) -> None:
        try:
            rows = self._get("/api/v3/earnings-calendar-confirmed", symbol=ticker, limit=1)
        except ProviderError:
            # Purely cosmetic (an essentials-block field); not worth a warning.
            return
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            out.next_earnings = str(rows[0].get("date") or "") or None

    def close(self) -> None:
        self.http.close()


# --------------------------------------------------------------------------
def _growth(current: float | None, prior: float | None) -> float | None:
    """Percentage growth, guarding the cases that make EPS growth meaningless.

    Growth from a negative or zero base is not a percentage anyone can
    interpret — "EPS grew 400% from -0.05 to 0.15" is noise — so it returns
    None and the letter falls back to being described in absolute terms.
    """
    if current is None or prior is None:
        return None
    if prior <= 0:
        return None
    return (current - prior) / abs(prior) * 100.0


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    num = _num(value)
    return int(num) if num is not None else None


def _pct(value: Any) -> float | None:
    """FMP reports ROE as a fraction; CAN SLIM thresholds are in percent."""
    num = _num(value)
    return num * 100.0 if num is not None else None


def _short(exc: ProviderError) -> str:
    text = str(exc)
    return text.split("|")[0].strip()[:90]
