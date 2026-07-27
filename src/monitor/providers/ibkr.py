"""Interactive Brokers — finer bars, real dollar ADV, and option-volume ratio.

**What IBKR can and cannot do for this monitor**, because the distinction
decides your architecture:

*Can* — aggregate volume, well. Bars down to 30 seconds (finer than FMP's
1-minute floor), a genuine 90-day average dollar volume, 52-week statistics,
and today's option volume for the underlying against its average. That last one
is a real unusual-activity signal you get *without* an options-flow
subscription, which is why `option_volume` exists as a detector.

*Cannot* — individual block or dark-pool prints, through this API surface.
Tick-by-tick trade data does exist in IBKR's native TWS API
(``reqTickByTickData`` with "AllLast"), but that is a socket protocol against a
running TWS instance, not REST. So L2 print detection still comes from Unusual
Whales.

**The deployment catch.** IBKR has no simple API key. This talks to the Client
Portal Web API, which means a Client Portal Gateway (or TWS) running *and
interactively authenticated*, with a session that needs re-authentication
roughly daily. That works on a machine you control. It does **not** work in a
GitHub Actions runner — there is nothing there to log in. So:

* Cron on GitHub Actions → use FMP for bars, and leave IBKR off.
* Self-hosted (home box, VPS, container next to the gateway) → IBKR gives you
  better bars, true ADV, and the option-volume signal.

Field IDs and paths are config values with tolerant parsing, for the same
reason as Unusual Whales: they vary by gateway version, and ``monitor verify``
tells you which ones answered.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from ..models import Bar
from .base import ProviderError, RateLimitedSession, SetupError, first_present

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

#: Bar sizes the Client Portal API accepts, keyed by our interval names.
BAR_SIZES = {
    "30sec": "30sec",
    "1min": "1min",
    "5min": "5min",
    "15min": "15min",
}

INTERVAL_MINUTES = {"30sec": 0.5, "1min": 1, "5min": 5, "15min": 15}

#: Client Portal market-data field IDs. Overridable in config because they have
#: shifted between gateway builds.
DEFAULT_FIELDS = {
    "last": "31",
    "volume": "87",
    "avg_volume": "7282",
    "high_52w": "7293",
    "low_52w": "7294",
    "option_volume_today": "7607",
    "option_volume_avg": "7289",
}

#: Client Portal history reports stock volume in *lots* of 100 shares, unlike
#: every other source here. Left configurable, and the engine cross-checks the
#: summed day volume against the snapshot to catch a wrong value.
DEFAULT_VOLUME_MULTIPLIER = 100


class IBKRProvider:
    name = "ibkr"

    def __init__(
        self,
        base_url: str,
        *,
        fields: Mapping[str, str] | None = None,
        volume_multiplier: int = DEFAULT_VOLUME_MULTIPLIER,
        timeout: int = 30,
        verify_tls: bool = False,
    ):
        if not base_url:
            raise SetupError(
                "IBKR needs a Client Portal Gateway URL (providers.ibkr.base_url, "
                "typically https://localhost:5000). IBKR has no API key — a "
                "gateway must be running and logged in, which is why this "
                "provider cannot be used from a GitHub Actions runner."
            )
        self.base_url = base_url.rstrip("/")
        self.fields = {**DEFAULT_FIELDS, **(fields or {})}
        self.volume_multiplier = max(1, int(volume_multiplier))
        self.http = RateLimitedSession(
            headers={"Accept": "application/json"}, min_interval=0.2, timeout=timeout
        )
        # The gateway serves a self-signed certificate on localhost by default.
        # This is a loopback connection to software the user is running, not a
        # public endpoint, so skipping verification here is deliberate and
        # scoped to this session only.
        self.http.session.verify = verify_tls
        self._conids: dict[str, int] = {}

    # -- session ----------------------------------------------------------
    def check_auth(self) -> None:
        """Fail with something actionable when the gateway isn't logged in."""
        try:
            status = self.http.get_json(f"{self.base_url}/v1/api/iserver/auth/status")
        except ProviderError as exc:
            raise SetupError(
                f"cannot reach the IBKR gateway at {self.base_url} ({exc}). Start "
                "the Client Portal Gateway and log in, then retry."
            ) from exc
        if not isinstance(status, dict) or not status.get("authenticated"):
            raise SetupError(
                "the IBKR gateway is reachable but not authenticated. Open its "
                "web page and log in — Client Portal sessions expire roughly "
                "daily, which is the main operational cost of using IBKR here."
            )

    # -- contracts --------------------------------------------------------
    def conid_for(self, symbol: str) -> int:
        """Resolve a symbol to a contract id, preferring the US primary listing."""
        symbol = symbol.upper()
        if symbol in self._conids:
            return self._conids[symbol]
        payload = self.http.get_json(
            f"{self.base_url}/v1/api/iserver/secdef/search",
            params={"symbol": symbol, "name": "false", "secType": "STK"},
        )
        rows = payload if isinstance(payload, list) else []
        best: int | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            # Exact symbol only: a search for AAPL also returns AAPU, AAPB and
            # a pile of leveraged ETFs that would silently grade the wrong name.
            if str(row.get("symbol", "")).upper() != symbol:
                continue
            conid = row.get("conid")
            if conid is None:
                continue
            listing = str(row.get("description") or row.get("listingExchange") or "")
            if best is None or listing.upper() in {"NASDAQ", "NYSE", "ARCA", "BATS"}:
                best = int(conid)
                if listing.upper() in {"NASDAQ", "NYSE"}:
                    break
        if best is None:
            raise ProviderError(
                f"IBKR returned no exact STK match for {symbol}. Check the symbol; "
                "note that search results include leveraged ETFs on the same root."
            )
        self._conids[symbol] = best
        return best

    # -- bars -------------------------------------------------------------
    def intraday_bars(
        self, symbol: str, interval: str, lookback_days: int = 45
    ) -> list[Bar]:
        bar_size = BAR_SIZES.get(interval, "5min")
        conid = self.conid_for(symbol)
        # The gateway caps history per request; ask in whole days.
        period = f"{max(1, min(lookback_days, 60))}d"
        payload = self.http.get_json(
            f"{self.base_url}/v1/api/iserver/marketdata/history",
            params={
                "conid": conid,
                "period": period,
                "bar": bar_size,
                "outsideRth": "false",
            },
        )
        return parse_history(payload, self.volume_multiplier)

    def average_daily_volume(
        self, bars: list[Bar], sessions: int, now: datetime | None = None
    ) -> float | None:
        """Prefer IBKR's own 90-day average; fall back to summing bars."""
        by_day: dict[Any, int] = {}
        for bar in bars:
            by_day[bar.ts.date()] = by_day.get(bar.ts.date(), 0) + bar.volume
        if not by_day:
            return None
        today = (now.astimezone(ET) if now else datetime.now(ET)).date()
        complete = [v for d, v in sorted(by_day.items()) if d != today]
        if not complete:
            return None
        window = complete[-sessions:]
        return sum(window) / len(window)

    # -- snapshot ---------------------------------------------------------
    def snapshot(self, symbol: str) -> dict[str, float | None]:
        """Live figures: last, day volume, average volume, 52-week range, option volume."""
        conid = self.conid_for(symbol)
        wanted = ",".join(self.fields.values())
        payload = self.http.get_json(
            f"{self.base_url}/v1/api/iserver/marketdata/snapshot",
            params={"conids": conid, "fields": wanted},
        )
        rows = payload if isinstance(payload, list) else []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        return {
            name: _loose_number(row.get(field_id))
            for name, field_id in self.fields.items()
        }

    def option_volume_ratio(self, symbol: str) -> tuple[float | None, float | None, float | None]:
        """(today's option volume, its average, the ratio) for the underlying.

        A coarse but genuine unusual-activity read: it says the whole option
        chain is busier than normal, without naming the trade. Cheaper than an
        options-flow subscription and useful as a confirmation signal.
        """
        snap = self.snapshot(symbol)
        today = snap.get("option_volume_today")
        average = snap.get("option_volume_avg")
        if not today or not average or average <= 0:
            return today, average, None
        return today, average, today / average

    # -- diagnostics ------------------------------------------------------
    def probe(self, symbol: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        try:
            self.check_auth()
            out.append(("auth", "ok — gateway authenticated"))
        except SetupError as exc:
            return [("auth", f"FAILED — {exc}")]
        try:
            conid = self.conid_for(symbol)
            out.append(("secdef/search", f"ok — {symbol} resolved"))
            _ = conid
        except ProviderError as exc:
            out.append(("secdef/search", f"FAILED — {exc}"))
            return out
        try:
            bars = self.intraday_bars(symbol, "5min", lookback_days=5)
            sessions = sorted({b.ts.date() for b in bars})
            out.append(
                ("marketdata/history", f"ok — {len(bars)} bars over {len(sessions)} session(s)")
            )
        except ProviderError as exc:
            out.append(("marketdata/history", f"FAILED — {exc}"))
        try:
            snap = self.snapshot(symbol)
            present = ", ".join(k for k, v in snap.items() if v is not None) or "nothing"
            missing = [k for k, v in snap.items() if v is None]
            note = f"ok — got {present}"
            if missing:
                note += (
                    f"; missing {', '.join(missing)} — correct the field ids under "
                    "providers.ibkr.fields"
                )
            out.append(("marketdata/snapshot", note))
        except ProviderError as exc:
            out.append(("marketdata/snapshot", f"FAILED — {exc}"))
        return out

    def close(self) -> None:
        self.http.close()


def parse_history(payload: Any, volume_multiplier: int) -> list[Bar]:
    """Parse Client Portal history, which comes in two shapes across versions.

    Newer gateways return ``data: [{t,o,c,h,l,v}]``; some builds (and the MCP
    surface) return parallel arrays keyed ``time``/``open``/``close``/etc.
    Both are handled so a gateway upgrade doesn't break the monitor.
    """
    if not isinstance(payload, Mapping):
        return []

    rows = payload.get("data")
    if isinstance(rows, list) and rows and isinstance(rows[0], Mapping):
        bars = [_bar_from_row(r, volume_multiplier) for r in rows]
        return sorted((b for b in bars if b), key=lambda b: b.ts)

    times = payload.get("time") or payload.get("t")
    if isinstance(times, list):
        return _bars_from_columns(payload, times, volume_multiplier)
    return []


def _bar_from_row(row: Mapping[str, Any], multiplier: int) -> Bar | None:
    stamp = first_present(row, ("t", "time", "timestamp"))
    try:
        ts = _to_et(stamp)
        return Bar(
            ts=ts,
            open=float(row["o"]),
            high=float(row["h"]),
            low=float(row["l"]),
            close=float(row["c"]),
            volume=int(round(float(row.get("v") or 0) * multiplier)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _bars_from_columns(
    payload: Mapping[str, Any], times: list, multiplier: int
) -> list[Bar]:
    def column(*names: str) -> list:
        for name in names:
            value = payload.get(name)
            if isinstance(value, list):
                return value
        return []

    opens = column("open", "o")
    highs = column("high", "h")
    lows = column("low", "l")
    closes = column("close", "c")
    volumes = column("volume", "v")

    bars: list[Bar] = []
    for i, stamp in enumerate(times):
        try:
            bars.append(
                Bar(
                    ts=_to_et(stamp),
                    open=float(opens[i]),
                    high=float(highs[i]),
                    low=float(lows[i]),
                    close=float(closes[i]),
                    # Column-shaped responses report actual shares, not lots.
                    volume=int(round(float(volumes[i]))),
                )
            )
        except (IndexError, TypeError, ValueError):
            continue
    bars.sort(key=lambda b: b.ts)
    return bars


def _to_et(stamp: Any) -> datetime:
    if isinstance(stamp, (int, float)):
        seconds = float(stamp) / 1000.0 if float(stamp) > 1e11 else float(stamp)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(ET)
    text = str(stamp).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET)


def _loose_number(value: Any) -> float | None:
    """Parse IBKR's display strings: '1.2M', '4.95e7', '336.80', 'C336.80'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    # Some fields are prefixed with a condition letter (e.g. 'C' for a close).
    if text[0].isalpha() and len(text) > 1:
        text = text[1:]
    multiplier = 1.0
    if text and text[-1].upper() in {"K", "M", "B"}:
        multiplier = {"K": 1e3, "M": 1e6, "B": 1e9}[text[-1].upper()]
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None
