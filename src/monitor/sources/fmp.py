"""Financial Modeling Prep — intraday bars, and the fundamentals CAN SLIM needs.

A note on plans, because it costs money to discover it the hard way: the
`historical-chart` and `insider-trading` endpoints used here are **not on the
free tier**. FMP's free key returns a 403 with an upgrade message for both. A
Starter plan or above is required. `monitor verify` reports exactly that rather
than letting it surface as a mysterious empty watchlist.

FMP timestamps intraday bars in US market time already, so they are localised
to Eastern rather than converted. Treating them as UTC would shift every bar by
four or five hours and silently compare the open against lunchtime.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..clock import ET
from ..models import Bar
from .base import BadResponse, EmptyResponse, HttpClient, NotConfigured, require

BASE = "https://financialmodelingprep.com/api"

#: FMP's interval vocabulary. Anything else has to be resampled, and silently
#: substituting a nearby interval would corrupt every baseline built from it.
INTERVALS = {1: "1min", 5: "5min", 15: "15min", 30: "30min", 60: "1hour", 240: "4hour"}


class FmpSource:
    name = "fmp"

    def __init__(self, api_key: str | None, client: HttpClient):
        self.api_key = api_key
        self.http = client

    def _get(self, path: str, ticker: str = "*", **params: Any) -> Any:
        key = require(self.api_key, self.name, "FMP_API_KEY")
        return self.http.get_json(f"{BASE}/{path}", {**params, "apikey": key}, ticker=ticker)

    # -- bars ---------------------------------------------------------------
    def bars(self, ticker: str, minutes: int, sessions: int) -> list[Bar]:
        interval = INTERVALS.get(minutes)
        if interval is None:
            raise NotConfigured(
                self.name,
                f"FMP has no {minutes}-minute interval; choose one of "
                f"{', '.join(str(m) for m in sorted(INTERVALS))}",
                ticker,
            )
        payload = self._get(f"v3/historical-chart/{interval}/{ticker.upper()}", ticker=ticker)
        return _parse_bars(payload, ticker, self.name)

    # -- fundamentals (used by the CAN SLIM grader) -------------------------
    def quote(self, ticker: str) -> dict[str, Any]:
        payload = self._get(f"v3/quote/{ticker.upper()}", ticker=ticker)
        if not isinstance(payload, list) or not payload:
            raise EmptyResponse(self.name, "no quote returned", ticker)
        return payload[0]

    def income_statements(self, ticker: str, limit: int = 12, quarterly: bool = True) -> list[dict]:
        return self._get(
            f"v3/income-statement/{ticker.upper()}",
            ticker=ticker, limit=limit, period="quarter" if quarterly else "annual",
        ) or []

    def key_metrics(self, ticker: str, limit: int = 8) -> list[dict]:
        return self._get(
            f"v3/key-metrics/{ticker.upper()}", ticker=ticker, limit=limit, period="quarter"
        ) or []

    def daily_history(self, ticker: str, days: int = 300) -> list[dict]:
        payload = self._get(
            f"v3/historical-price-full/{ticker.upper()}", ticker=ticker, timeseries=days
        )
        if isinstance(payload, dict):
            return payload.get("historical", []) or []
        return []

    def insider_trades(self, ticker: str, limit: int = 100) -> list[dict]:
        """FMP's Form 4 mirror. Convenient, but SEC EDGAR is the primary source."""
        return self._get(
            "v4/insider-trading", ticker=ticker, symbol=ticker.upper(), page=0, limit=limit
        ) or []


def _parse_bars(payload: Any, ticker: str, source: str) -> list[Bar]:
    if isinstance(payload, dict):
        # FMP reports plan and rate-limit problems as a 200 with an error body.
        message = payload.get("Error Message") or payload.get("message") or str(payload)[:200]
        raise BadResponse(source, message, ticker)
    if not isinstance(payload, list):
        raise BadResponse(source, f"expected a list of bars, got {type(payload).__name__}", ticker)
    if not payload:
        raise EmptyResponse(source, "no intraday bars returned", ticker)

    bars: list[Bar] = []
    for row in payload:
        try:
            stamp = datetime.strptime(row["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
            bars.append(Bar(
                ts=stamp,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=int(row["volume"]),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise BadResponse(source, f"malformed bar {row!r}: {exc}", ticker) from exc

    bars.sort(key=lambda b: b.ts)
    return bars


def parse_fmp_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()
