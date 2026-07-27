"""Unusual Whales — pre-computed L2 (dark pool / block prints) and L3 (options flow).

This is the piece that lets a cron job reach L2/L3 fidelity: UW has already done
the tick-level detection, so we poll REST instead of holding a websocket open.

Two deliberate design choices, both because UW serves 403 to unauthenticated
requests including on their own documentation:

1. **Endpoint paths come from config**, defaulted in ``config.UW_DEFAULT_PATHS``.
   A path that has moved is a YAML edit, not a code change.
2. **Field extraction is tolerant.** Every value is read through a list of
   candidate key names, so a renamed field degrades one attribute instead of
   crashing the run.

Run ``monitor verify`` once after adding your key — it probes each configured
path and prints exactly which ones answered and which need correcting.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from ..models import OptionTrade, Side, Trade
from .base import (
    ProviderError,
    RateLimitedSession,
    SetupError,
    first_present,
    unwrap_list,
)

log = logging.getLogger(__name__)


class UnusualWhalesProvider:
    name = "unusual_whales"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        paths: Mapping[str, str],
        timeout: int = 30,
    ):
        if not api_key:
            raise SetupError(
                "UW_API_KEY is not set. Generate one at "
                "unusualwhales.com/settings/api-dashboard and add it as a "
                "repository secret."
            )
        self.base_url = base_url.rstrip("/")
        self.paths = dict(paths)
        self.http = RateLimitedSession(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            min_interval=0.35,
            timeout=timeout,
        )

    def _url(self, key: str, **fmt: Any) -> str:
        try:
            template = self.paths[key]
        except KeyError as exc:
            raise ProviderError(f"no configured UW path for {key!r}") from exc
        return self.base_url + template.format(**fmt)

    # -- L2 ---------------------------------------------------------------
    def dark_pool_prints(self, ticker: str, limit: int = 200) -> list[Trade]:
        payload = self.http.get_json(
            self._url("dark_pool_ticker", ticker=ticker), params={"limit": limit}
        )
        return [t for t in (_to_trade(row, ticker) for row in unwrap_list(payload)) if t]

    # -- L3 ---------------------------------------------------------------
    def flow_alerts(self, ticker: str, limit: int = 200) -> list[OptionTrade]:
        """Per-ticker flow alerts, falling back to the market-wide feed."""
        try:
            payload = self.http.get_json(
                self._url("ticker_flow_alerts", ticker=ticker), params={"limit": limit}
            )
        except ProviderError as exc:
            if exc.status not in (400, 404):
                raise
            log.debug("per-ticker flow path unavailable (%s); using market-wide", exc.status)
            payload = self.http.get_json(
                self._url("flow_alerts"),
                params={"ticker_symbol": ticker, "limit": limit},
            )
        rows = unwrap_list(payload)
        out = []
        for row in rows:
            trade = _to_option_trade(row, ticker)
            # The market-wide feed needs filtering; the per-ticker one is a no-op.
            if trade and trade.ticker.upper() == ticker.upper():
                out.append(trade)
        return out

    # -- diagnostics ------------------------------------------------------
    def probe(self, ticker: str) -> list[tuple[str, str, str]]:
        """(path key, resolved url, outcome) for every configured path."""
        results = []
        for key, template in sorted(self.paths.items()):
            fmt: dict[str, Any] = {}
            if "{ticker}" in template:
                fmt["ticker"] = ticker
            if "{interval}" in template:
                fmt["interval"] = "5m"
            url = self.base_url + template.format(**fmt)
            try:
                payload = self.http.get_json(url, params={"limit": 1})
                rows = unwrap_list(payload)
                shape = (
                    f"ok — {len(rows)} row(s), keys: "
                    + ", ".join(sorted(rows[0])[:8]
                                if rows and isinstance(rows[0], dict) else ["<empty>"])
                )
                results.append((key, url, shape))
            except ProviderError as exc:
                results.append((key, url, f"FAILED — {exc}"))
        return results

    def close(self) -> None:
        self.http.close()


# --------------------------------------------------------------------------
# Tolerant field extraction
# --------------------------------------------------------------------------
TS_KEYS = ("executed_at", "created_at", "timestamp", "time", "date", "start_time")
PRICE_KEYS = ("price", "fill_price", "avg_price", "trade_price")
SIZE_KEYS = ("size", "quantity", "shares", "total_size", "volume")
BID_KEYS = ("nbbo_bid", "bid", "best_bid")
ASK_KEYS = ("nbbo_ask", "ask", "best_ask")
VENUE_KEYS = ("market_center", "exchange", "venue", "mkt_center")
ID_KEYS = ("tracking_id", "id", "trade_id", "uuid")
PREMIUM_KEYS = ("total_premium", "premium", "notional", "total_prem")


def _num(payload: Mapping[str, Any], keys: Iterable[str]) -> float | None:
    raw = first_present(payload, tuple(keys))
    if raw is None or raw == "":
        return None
    try:
        return float(str(raw).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _ts(payload: Mapping[str, Any]) -> datetime | None:
    raw = first_present(payload, TS_KEYS)
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Heuristic: values this large are milliseconds.
        seconds = float(raw) / 1000.0 if float(raw) > 1e11 else float(raw)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _infer_side(price: float | None, bid: float | None, ask: float | None) -> Side:
    """Lee-Ready quote rule: compare the print to the prevailing midpoint.

    This is an inference, not ground truth — the tape carries no side flag.
    """
    if price is None or bid is None or ask is None or ask <= bid:
        return Side.UNKNOWN
    mid = (bid + ask) / 2.0
    # A tenth of the spread of slack keeps midpoint crosses out of the buckets.
    tolerance = (ask - bid) * 0.1
    if price > mid + tolerance:
        return Side.BUY
    if price < mid - tolerance:
        return Side.SELL
    return Side.UNKNOWN


def _to_trade(row: Any, ticker: str) -> Trade | None:
    if not isinstance(row, Mapping):
        return None
    price = _num(row, PRICE_KEYS)
    size = _num(row, SIZE_KEYS)
    ts = _ts(row)
    if price is None or size is None or ts is None:
        return None
    bid, ask = _num(row, BID_KEYS), _num(row, ASK_KEYS)
    venue = first_present(row, VENUE_KEYS)
    explicit = str(first_present(row, ("side",), "") or "").lower()
    side = {"buy": Side.BUY, "sell": Side.SELL}.get(
        explicit, _infer_side(price, bid, ask)
    )
    return Trade(
        ticker=str(first_present(row, ("ticker", "symbol"), ticker)).upper(),
        ts=ts,
        price=price,
        size=int(size),
        venue=str(venue) if venue is not None else None,
        # Everything on the dark pool endpoint is by definition off-exchange.
        is_off_exchange=True,
        side=side,
        nbbo_bid=bid,
        nbbo_ask=ask,
        raw_id=str(first_present(row, ID_KEYS, "") or "") or None,
    )


def _to_option_trade(row: Any, ticker: str) -> OptionTrade | None:
    if not isinstance(row, Mapping):
        return None
    ts = _ts(row)
    if ts is None:
        return None

    size = _num(row, ("total_size", "size", "volume", "quantity")) or 0
    premium = _num(row, PREMIUM_KEYS)
    price = _num(row, ("price", "avg_price", "fill_price"))
    if premium is None and price is not None and size:
        premium = price * size * 100  # standard 100-share contract multiplier
    if premium is None:
        return None

    option_type = str(
        first_present(row, ("type", "option_type", "put_call", "contract_type"), "")
        or ""
    ).lower()
    if option_type.startswith("c"):
        option_type = "call"
    elif option_type.startswith("p"):
        option_type = "put"
    else:
        return None

    strike = _num(row, ("strike", "strike_price"))
    expiry = first_present(row, ("expiry", "expiration", "expires_at", "expiry_date"))
    if strike is None or expiry is None:
        return None

    explicit_side = str(first_present(row, ("side",), "") or "").lower()
    side = {"ask": Side.BUY, "buy": Side.BUY, "bid": Side.SELL, "sell": Side.SELL}.get(
        explicit_side, Side.UNKNOWN
    )

    return OptionTrade(
        ticker=str(first_present(row, ("ticker", "underlying_symbol", "symbol"), ticker)).upper(),
        ts=ts,
        premium=premium,
        option_type=option_type,
        strike=strike,
        expiry=str(expiry)[:10],
        size=int(size),
        volume=_int_or_none(_num(row, ("volume", "day_volume", "total_volume"))),
        open_interest=_int_or_none(_num(row, ("open_interest", "oi", "prev_oi"))),
        trade_type=_trade_type(row),
        side=side,
        underlying_price=_num(row, ("underlying_price", "stock_price", "spot")),
        raw_id=str(first_present(row, ID_KEYS, "") or "") or None,
    )


def _int_or_none(value: float | None) -> int | None:
    return int(value) if value is not None else None


def _trade_type(row: Mapping[str, Any]) -> str:
    explicit = str(
        first_present(row, ("trade_type", "execution_type", "alert_type"), "") or ""
    ).lower()
    for candidate in ("sweep", "block", "split"):
        if candidate in explicit:
            return candidate
    if first_present(row, ("has_sweep", "is_sweep")):
        return "sweep"
    if first_present(row, ("has_floor", "is_block", "has_block")):
        return "block"
    if first_present(row, ("has_multileg", "is_split")):
        return "split"
    return "unknown"
