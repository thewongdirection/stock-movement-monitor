"""Financial Modeling Prep — intraday bars for the L1 volume detector.

FMP has shipped several API generations (``/api/v3`` and the newer ``/stable``)
with different URL shapes, and which one a key can reach depends on the plan.
Rather than guess, the provider tries each known shape once and remembers the
first that answers, so a plan difference costs one wasted request per process
instead of a code change.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..models import Bar
from .base import ProviderError, RateLimitedSession, SetupError, unwrap_list

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

INTERVAL_ALIASES = {"1min": "1min", "5min": "5min", "15min": "15min"}


class FMPProvider:
    name = "fmp"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://financialmodelingprep.com",
        timeout: int = 30,
    ):
        if not api_key:
            raise SetupError(
                "FMP_API_KEY is not set. The MCP connector in your chat session "
                "does not give this script a key — create one at "
                "financialmodelingprep.com/developer and add it as a repository secret."
            )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.http = RateLimitedSession(min_interval=0.25, timeout=timeout)
        self._working_shape: str | None = None

    # -- URL shapes -------------------------------------------------------
    def _candidates(self, symbol: str, interval: str) -> list[tuple[str, str, dict]]:
        return [
            (
                "v3",
                f"{self.base_url}/api/v3/historical-chart/{interval}/{symbol}",
                {},
            ),
            (
                "stable",
                f"{self.base_url}/stable/historical-chart/{interval}",
                {"symbol": symbol},
            ),
            (
                "stable-suffixed",
                f"{self.base_url}/stable/historical-chart-{interval}",
                {"symbol": symbol},
            ),
        ]

    def intraday_bars(
        self, symbol: str, interval: str, lookback_days: int = 45
    ) -> list[Bar]:
        """Intraday bars, oldest first, timestamped in US/Eastern."""
        interval = INTERVAL_ALIASES.get(interval, "5min")
        today = datetime.now(ET).date()
        params_common = {
            "from": (today - timedelta(days=lookback_days)).isoformat(),
            "to": today.isoformat(),
            "apikey": self.api_key,
        }

        shapes = self._candidates(symbol, interval)
        if self._working_shape:
            shapes = [s for s in shapes if s[0] == self._working_shape] + [
                s for s in shapes if s[0] != self._working_shape
            ]

        last_error: ProviderError | None = None
        for shape, url, extra in shapes:
            try:
                payload = self.http.get_json(url, params={**params_common, **extra})
            except ProviderError as exc:
                # A 404/403 means "wrong shape for this plan" — try the next one.
                if exc.status in (403, 404) and len(shapes) > 1:
                    last_error = exc
                    log.debug("FMP shape %s rejected (%s); trying next", shape, exc.status)
                    continue
                raise
            self._working_shape = shape
            return _parse_bars(payload)

        raise last_error or ProviderError(f"no FMP endpoint shape worked for {symbol}")

    def daily_bars(self, symbol: str, years: float = 2.0) -> list[Bar]:
        """Daily OHLCV — what the CAN SLIM technicals need, not intraday."""
        today = datetime.now(ET).date()
        start = today - timedelta(days=int(365 * years))
        payload = self.http.get_json(
            f"{self.base_url}/api/v3/historical-price-full/{symbol}",
            params={
                "from": start.isoformat(),
                "to": today.isoformat(),
                "apikey": self.api_key,
            },
        )
        return _parse_bars(payload)

    def average_daily_volume(
        self, bars: list[Bar], sessions: int, now: datetime | None = None
    ) -> float | None:
        """ADV derived from the intraday bars we already hold — no extra call.

        `now` comes from the run rather than the wall clock so that "today" means
        the same day the rest of the run is reasoning about.
        """
        by_day: dict[date, int] = {}
        for bar in bars:
            by_day[bar.ts.date()] = by_day.get(bar.ts.date(), 0) + bar.volume
        if not by_day:
            return None
        # Drop today: a partial session would drag the average down.
        today = (now.astimezone(ET) if now else datetime.now(ET)).date()
        complete = [v for d, v in sorted(by_day.items()) if d != today]
        if not complete:
            return None
        window = complete[-sessions:]
        return sum(window) / len(window)

    def close(self) -> None:
        self.http.close()


def _parse_bars(payload: object) -> list[Bar]:
    rows = unwrap_list(payload, keys=("historical", "data", "results"))
    bars: list[Bar] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = row.get("date") or row.get("datetime") or row.get("timestamp")
        try:
            ts = _parse_ts(stamp)
            bars.append(
                Bar(
                    ts=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row.get("volume") or 0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    bars.sort(key=lambda b: b.ts)
    return bars


def _parse_ts(stamp: object) -> datetime:
    if isinstance(stamp, (int, float)):
        return datetime.fromtimestamp(float(stamp), tz=ET)
    text = str(stamp).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    # FMP returns naive timestamps already in US/Eastern.
    return dt.replace(tzinfo=ET) if dt.tzinfo is None else dt.astimezone(ET)
