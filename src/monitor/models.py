"""Core value types shared by providers, detectors and notifiers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]


class Side(str, Enum):
    """Inferred aggressor side.

    The consolidated tape does not carry a side flag, so anything other than
    UNKNOWN here is an *inference* (print price relative to the prevailing
    bid/ask, i.e. the Lee-Ready tick rule) and should be read as such.
    """

    BUY = "buy"
    SELL = "sell"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Bar:
    """A single OHLCV bar, timestamped at the bar's *open*, in US/Eastern."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def notional(self) -> float:
        """Approximate traded value, using the bar's typical price."""
        return ((self.high + self.low + self.close) / 3.0) * self.volume


@dataclass(frozen=True)
class Trade:
    """A single reported trade (block print or dark pool print)."""

    ticker: str
    ts: datetime
    price: float
    size: int
    venue: str | None = None
    is_off_exchange: bool = False
    side: Side = Side.UNKNOWN
    nbbo_bid: float | None = None
    nbbo_ask: float | None = None
    raw_id: str | None = None

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass(frozen=True)
class OptionTrade:
    """An options flow event (sweep / block / split)."""

    ticker: str
    ts: datetime
    premium: float
    option_type: str  # "call" | "put"
    strike: float
    expiry: str  # ISO date
    size: int
    volume: int | None = None
    open_interest: int | None = None
    trade_type: str | None = None  # sweep | block | split
    side: Side = Side.UNKNOWN
    underlying_price: float | None = None
    raw_id: str | None = None

    @property
    def dte(self) -> int | None:
        try:
            exp = datetime.fromisoformat(self.expiry).date()
        except (ValueError, TypeError):
            return None
        return (exp - self.ts.date()).days

    @property
    def moneyness_pct(self) -> float | None:
        """How far in/out of the money, as a signed % of the underlying.

        Positive means in-the-money for the given option type.
        """
        if not self.underlying_price:
            return None
        raw = (self.underlying_price - self.strike) / self.underlying_price * 100.0
        return raw if self.option_type == "call" else -raw


@dataclass(frozen=True)
class OptionVolumeSnapshot:
    """Chain-level option volume for an underlying, from IBKR.

    Day-cumulative, so a ratio against the average only means something once
    enough of the session has run — see the option_volume detector.
    """

    ticker: str
    today_volume: float | None
    average_volume: float | None
    call_volume: float | None = None
    put_volume: float | None = None

    @property
    def ratio(self) -> float | None:
        if not self.today_volume or not self.average_volume:
            return None
        if self.average_volume <= 0:
            return None
        return self.today_volume / self.average_volume

    @property
    def call_put_skew(self) -> float | None:
        """Calls per put. High means the activity is one-sided to the upside."""
        if self.call_volume is None or not self.put_volume:
            return None
        return self.call_volume / self.put_volume


@dataclass(frozen=True)
class InsiderTransaction:
    """One non-derivative or derivative line off an SEC Form 4."""

    ticker: str
    issuer_name: str
    insider_name: str
    insider_title: str
    is_director: bool
    is_officer: bool
    is_ten_pct_owner: bool
    transaction_code: str  # P=purchase, S=sale, A=grant, M=option exercise, ...
    acquired_disposed: str  # "A" | "D"
    shares: float
    price_per_share: float
    transaction_date: str  # ISO date the trade happened
    filed_at: datetime  # when the Form 4 hit EDGAR
    accession: str
    is_derivative: bool = False
    is_10b5_1: bool = False
    shares_owned_after: float | None = None
    url: str | None = None

    @property
    def notional(self) -> float:
        return self.shares * self.price_per_share

    @property
    def value_known(self) -> bool:
        """Some Form 4s state no price (weighted-average fills, gifts).

        Those transactions are real and must not be silently dropped by a
        dollar threshold, so callers check this before filtering on notional.
        """
        return self.price_per_share > 0

    @property
    def role(self) -> str:
        bits = []
        if self.is_officer and self.insider_title:
            bits.append(self.insider_title)
        elif self.is_officer:
            bits.append("Officer")
        if self.is_director:
            bits.append("Director")
        if self.is_ten_pct_owner:
            bits.append("10% Owner")
        return ", ".join(bits) or "Insider"


@dataclass
class Alert:
    """A notification-ready finding."""

    ticker: str
    detector: str
    severity: Severity
    headline: str
    occurred_at: datetime
    lines: list[str] = field(default_factory=list)
    url: str | None = None
    dedup_parts: tuple[str, ...] = ()

    @property
    def dedup_id(self) -> str:
        """Stable identity, so a replayed window never double-notifies."""
        payload = "|".join((self.detector, self.ticker, *self.dedup_parts))
        return hashlib.sha1(payload.encode()).hexdigest()[:20]
