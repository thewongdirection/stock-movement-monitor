"""Interactive Brokers Client Portal Gateway — bars and option open interest.

The gateway runs locally and proxies your own IBKR account, so there is no API
key: authentication is the browser session you established when you started it.
That has two consequences the code has to handle rather than assume away.

**The certificate is self-signed**, because the gateway serves
`https://localhost:5000`. TLS verification is therefore off for this source
specifically — see `ibkr.verify_tls`. This is safe only because the connection
never leaves the machine; pointing `ibkr.base_url` at a remote host with
verification off would be a real exposure, and `verify` says so.

**Sessions expire.** A gateway that has not been re-authenticated returns
plausible-looking empty results rather than a 401, so every call path starts by
checking `/iserver/auth/status` and fails loudly instead of reporting a quiet
market.

Two response quirks are handled explicitly because both are silent when wrong:
bar volume arrives in hundreds, and the market-data snapshot must be called
twice before it is populated.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone

from ..clock import ET, now_et
from ..models import Bar, OptionChain, OptionContract
from .base import BadResponse, EmptyResponse, HttpClient, Unreachable

log = logging.getLogger("monitor.sources.ibkr")

#: Gateway bar vocabulary. Keys are minutes; the gateway rejects anything else.
BAR_SIZES = {1: "1min", 2: "2min", 3: "3min", 5: "5min", 10: "10min",
             15: "15min", 30: "30min", 60: "1h", 120: "2h", 240: "4h"}

FIELD_LAST = "31"


class IbkrSource:
    name = "ibkr"

    def __init__(
        self,
        http: HttpClient,
        base_url: str = "https://localhost:5000/v1/api",
        *,
        volume_multiplier: float = 100.0,
        oi_field: str = "7638",
        option_volume_field: str = "7089",
        snapshot_batch: int = 50,
        strike_window_pct: float = 10.0,
        max_contracts: int = 240,
        expiries: int = 3,
        sleeper=time.sleep,
    ):
        self.http = http
        self.base = base_url.rstrip("/")
        self.volume_multiplier = volume_multiplier
        self.oi_field = str(oi_field)
        self.option_volume_field = str(option_volume_field)
        self.snapshot_batch = max(1, snapshot_batch)
        self.strike_window_pct = strike_window_pct
        self.max_contracts = max_contracts
        self.expiries = expiries
        self._sleep = sleeper
        self._conids: dict[str, int] = {}

    # -- session ------------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def check_auth(self) -> dict:
        """Confirm the gateway is up *and* logged in.

        An expired session is the failure mode that matters: the gateway keeps
        answering, just with nothing in it.
        """
        status = self.http.get_json(self._url("iserver/auth/status"))
        if not isinstance(status, dict):
            raise BadResponse(self.name, f"unexpected auth status payload: {status!r}")
        if not status.get("authenticated"):
            raise Unreachable(
                self.name,
                "gateway is running but not authenticated — open "
                f"{self.base.split('/v1')[0]} in a browser and log in",
            )
        if status.get("competing"):
            raise Unreachable(
                self.name,
                "another session has taken over this IBKR login; market data will be empty",
            )
        return status

    # -- contract resolution ------------------------------------------------
    def conid(self, ticker: str) -> int:
        ticker = ticker.upper()
        if ticker in self._conids:
            return self._conids[ticker]
        rows = self.http.get_json(
            self._url("iserver/secdef/search"),
            {"symbol": ticker, "name": "false", "secType": "STK"},
            ticker=ticker,
        )
        if not isinstance(rows, list) or not rows:
            raise EmptyResponse(self.name, f"no contract found for {ticker}", ticker)
        for row in rows:
            if str(row.get("symbol", "")).upper() == ticker and row.get("conid"):
                self._conids[ticker] = int(row["conid"])
                return self._conids[ticker]
        raise EmptyResponse(
            self.name,
            f"secdef/search returned {len(rows)} rows but none with symbol {ticker}",
            ticker,
        )

    def _sections(self, ticker: str) -> list[dict]:
        rows = self.http.get_json(
            self._url("iserver/secdef/search"),
            {"symbol": ticker.upper(), "name": "false", "secType": "STK"},
            ticker=ticker,
        )
        for row in rows or []:
            if str(row.get("symbol", "")).upper() == ticker.upper():
                return row.get("sections", []) or []
        return []

    # -- bars ---------------------------------------------------------------
    def bars(self, ticker: str, minutes: int, sessions: int) -> list[Bar]:
        size = BAR_SIZES.get(minutes)
        if size is None:
            raise BadResponse(
                self.name,
                f"gateway has no {minutes}-minute bar; choose one of "
                f"{', '.join(str(m) for m in sorted(BAR_SIZES))}",
                ticker,
            )
        self.check_auth()
        # Calendar days, not sessions — ask for enough to cover weekends and
        # holidays, then let the caller's session logic do the filtering.
        span = max(2, int(sessions * 1.6) + 4)
        payload = self.http.get_json(
            self._url("iserver/marketdata/history"),
            {"conid": self.conid(ticker), "period": f"{span}d",
             "bar": size, "outsideRth": "false"},
            ticker=ticker,
        )
        if not isinstance(payload, dict):
            raise BadResponse(self.name, f"expected an object, got {type(payload).__name__}", ticker)
        rows = payload.get("data") or []
        if not rows:
            raise EmptyResponse(
                self.name,
                "history returned no bars — usually an expired gateway session "
                "or a missing market-data subscription",
                ticker,
            )

        bars: list[Bar] = []
        for row in rows:
            try:
                stamp = datetime.fromtimestamp(int(row["t"]) / 1000, tz=timezone.utc).astimezone(ET)
                bars.append(Bar(
                    ts=stamp,
                    open=float(row["o"]),
                    high=float(row["h"]),
                    low=float(row["l"]),
                    close=float(row["c"]),
                    # The gateway reports volume in hundreds. Getting this wrong
                    # would not disturb RVOL — it is a ratio, so the factor
                    # cancels — but it would move every notional threshold by
                    # two orders of magnitude.
                    volume=int(round(float(row["v"]) * self.volume_multiplier)),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise BadResponse(self.name, f"malformed bar {row!r}: {exc}", ticker) from exc

        bars.sort(key=lambda b: b.ts)
        return bars

    # -- market data snapshots ----------------------------------------------
    def snapshot(self, conids: list[int], fields: list[str], ticker: str = "*") -> dict[int, dict]:
        """Fetch fields for a batch of contracts.

        The gateway builds a subscription on the first request and answers it on
        the second, so a single call routinely returns rows containing only
        `conid`. The prime-then-read below is the documented behaviour, not a
        workaround for flakiness.
        """
        out: dict[int, dict] = {}
        field_param = ",".join(fields)
        for start in range(0, len(conids), self.snapshot_batch):
            batch = conids[start:start + self.snapshot_batch]
            params = {"conids": ",".join(str(c) for c in batch), "fields": field_param}
            self.http.get_json(self._url("iserver/marketdata/snapshot"), params, ticker=ticker)
            self._sleep(1.0)
            rows = self.http.get_json(self._url("iserver/marketdata/snapshot"), params, ticker=ticker)
            for row in rows or []:
                if isinstance(row, dict) and row.get("conid") is not None:
                    out[int(row["conid"])] = row
        return out

    def spot(self, ticker: str) -> float:
        rows = self.snapshot([self.conid(ticker)], [FIELD_LAST], ticker)
        row = rows.get(self.conid(ticker), {})
        price = _number(row.get(FIELD_LAST))
        if price is None:
            raise EmptyResponse(self.name, "no last price for the underlying", ticker)
        return price

    # -- option chain -------------------------------------------------------
    def chain(self, ticker: str, max_days_to_expiry: int) -> OptionChain:
        """Contracts near the money, with their open interest.

        Bounded on three axes — expiries, strike distance, and a hard contract
        cap — because an unbounded chain on a liquid name is thousands of
        contracts and one `secdef/info` request per strike per right.
        """
        ticker = ticker.upper()
        self.check_auth()
        underlying = self.conid(ticker)
        price = self.spot(ticker)
        today = now_et().date()
        horizon = today + timedelta(days=max_days_to_expiry)

        months = _option_months(self._sections(ticker))
        if not months:
            raise EmptyResponse(self.name, "no listed options for this underlying", ticker)
        months = [m for m in months if _month_start(m) is None or _month_start(m) <= horizon]
        months = months[:self.expiries]

        lo = price * (1 - self.strike_window_pct / 100)
        hi = price * (1 + self.strike_window_pct / 100)

        wanted: dict[int, tuple[str, float, str]] = {}   # conid -> (expiry, strike, right)
        for month in months:
            strikes = self.http.get_json(
                self._url("iserver/secdef/strikes"),
                {"conid": underlying, "sectype": "OPT", "month": month, "exchange": "SMART"},
                ticker=ticker,
            ) or {}
            for right, key in (("call", "call"), ("put", "put")):
                for strike in strikes.get(key, []) or []:
                    strike = _number(strike)
                    if strike is None or not (lo <= strike <= hi):
                        continue
                    if len(wanted) >= self.max_contracts:
                        break
                    for info in self._contract_info(ticker, underlying, month, strike, right):
                        wanted[info[0]] = info[1]

        if not wanted:
            raise EmptyResponse(
                self.name,
                f"no contracts within {self.strike_window_pct:g}% of ${price:,.2f} "
                f"expiring inside {max_days_to_expiry} days",
                ticker,
            )

        fields = [self.oi_field, self.option_volume_field]
        rows = self.snapshot(list(wanted), fields, ticker)

        contracts: list[OptionContract] = []
        missing_oi = 0
        for conid, (expiry, strike, right) in wanted.items():
            row = rows.get(conid, {})
            oi = _number(row.get(self.oi_field))
            if oi is None:
                missing_oi += 1
                continue
            contracts.append(OptionContract(
                ticker=ticker,
                expiry=expiry,
                strike=strike,
                right=right,
                open_interest=int(oi),
                volume=int(_number(row.get(self.option_volume_field)) or 0),
                as_of=today.isoformat(),
            ))

        if not contracts:
            raise BadResponse(
                self.name,
                f"none of {len(wanted)} contracts returned field {self.oi_field} "
                "(open interest). Confirm the field id for your gateway build and "
                "set ibkr.oi_field accordingly",
                ticker,
            )
        if missing_oi:
            log.warning("%s/%s: %d of %d contracts had no open interest field",
                        self.name, ticker, missing_oi, len(wanted))

        return OptionChain(ticker=ticker, as_of=today.isoformat(), contracts=contracts)

    def _contract_info(self, ticker: str, underlying: int, month: str,
                       strike: float, right: str) -> list[tuple[int, tuple[str, float, str]]]:
        """Resolve one strike into its contracts. A month holds several expiries."""
        rows = self.http.get_json(
            self._url("iserver/secdef/info"),
            {"conid": underlying, "sectype": "OPT", "month": month,
             "strike": strike, "right": right[0].upper()},
            ticker=ticker,
        )
        out: list[tuple[int, tuple[str, float, str]]] = []
        for row in rows or []:
            conid = row.get("conid")
            maturity = str(row.get("maturityDate") or "")
            if not conid or len(maturity) != 8:
                continue
            expiry = f"{maturity[:4]}-{maturity[4:6]}-{maturity[6:]}"
            out.append((int(conid), (expiry, float(strike), right)))
        return out


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #

def _number(value: object) -> float | None:
    """Parse a gateway numeric field.

    Values arrive as strings, sometimes suffixed ('1.2M') or prefixed with a
    quality marker ('C5.25' for a close-derived price). Returning None rather
    than 0.0 for junk matters: zero open interest is a real, meaningful value.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lstrip("CcHh").replace(",", "")
    multiplier = 1.0
    if text[-1:].upper() in ("K", "M", "B"):
        multiplier = {"K": 1e3, "M": 1e6, "B": 1e9}[text[-1].upper()]
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def _option_months(sections: list[dict]) -> list[str]:
    """Pull the OPT section's month list, e.g. ['JUL26', 'AUG26', 'SEP26']."""
    for section in sections:
        if str(section.get("secType", "")).upper() == "OPT":
            raw = section.get("months") or ""
            return [m.strip() for m in str(raw).split(";") if m.strip()]
    return []


_MONTHS = {name: i for i, name in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def _month_start(token: str) -> date | None:
    """'AUG26' -> date(2026, 8, 1). None when the token is not recognised."""
    token = token.strip().upper()
    if len(token) != 5 or token[:3] not in _MONTHS:
        return None
    try:
        year = 2000 + int(token[3:])
    except ValueError:
        return None
    return date(year, _MONTHS[token[:3]], 1)
