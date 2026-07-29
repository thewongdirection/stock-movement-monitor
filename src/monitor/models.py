"""The domain types, and what each one is honestly able to say.

Two facts about US market data shape almost everything here, so they are worth
stating once at the top:

**The consolidated tape carries no aggressor flag.** A trade print has price,
size, venue and condition codes. It does not say whether the buyer or the seller
initiated. Any "institutional buying" claim is an *inference* — usually from
where the print sat relative to the bid/ask. `Side` therefore defaults to
UNKNOWN and has to be argued for, never assumed.

**Volume is not position-taking.** A million contracts can trade and leave open
interest unchanged, because the same contracts changed hands all day. Only
`OptionChain.oi_change` shows positions that were opened *and held overnight*,
which is the closest thing to proof that someone actually took a position.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"low": 0, "medium": 1, "high": 2}[self.value]

    @property
    def icon(self) -> str:
        return {"low": "🔵", "medium": "🟠", "high": "🔴"}[self.value]

    @property
    def notifies(self) -> bool:
        """Low severity lands in the chat without buzzing the phone."""
        return self is not Severity.LOW


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
    UNKNOWN = "unknown"


def escalate(base: Severity, *promotions: bool) -> Severity:
    """Raise severity one step per condition met. Never above HIGH."""
    rank = min(base.rank + sum(1 for p in promotions if p), Severity.HIGH.rank)
    return [Severity.LOW, Severity.MEDIUM, Severity.HIGH][rank]


@dataclass(frozen=True)
class Bar:
    """One OHLCV interval, timestamped at its *start*, in US/Eastern.

    Eastern, not UTC, because the baseline compares a bar against the same clock
    minute on previous sessions. Bucketing by UTC would silently compare 09:30
    against 10:30 across a daylight-saving change.
    """

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def notional(self) -> float:
        """Rough dollars traded. Close price, not VWAP — this is a filter, not a fill."""
        return self.close * self.volume

    @property
    def move_pct(self) -> float:
        return (self.close - self.open) / self.open * 100 if self.open else 0.0


@dataclass(frozen=True)
class Trade:
    """A single printed trade.

    `side` is inferred and frequently UNKNOWN. See the module docstring.
    """

    ticker: str
    ts: datetime
    price: float
    size: int
    venue: str | None = None
    off_exchange: bool = False
    side: Side = Side.UNKNOWN
    ref: str | None = None

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass(frozen=True)
class OptionContract:
    """One listed option, with its open interest as of a given date.

    `open_interest` is an end-of-day figure. OCC computes it overnight, so the
    freshest number available during a session is yesterday's close. That
    latency is inherent, not an implementation shortcut.
    """

    ticker: str
    expiry: str            # ISO date
    strike: float
    right: str             # "call" | "put"
    open_interest: int
    volume: int = 0
    as_of: str = ""        # ISO date the OI figure belongs to

    @property
    def key(self) -> str:
        return f"{self.ticker}|{self.expiry}|{self.strike:g}|{self.right}"

    def label(self) -> str:
        return f"{self.expiry} ${self.strike:g} {self.right.upper()}"


@dataclass
class OptionChain:
    """A ticker's contracts today, against the same contracts yesterday.

    The whole point of this type: `oi_change` is the only figure in this project
    that demonstrates a position was *taken* rather than merely traded.
    """

    ticker: str
    as_of: str
    contracts: list[OptionContract] = field(default_factory=list)
    previous: dict[str, int] = field(default_factory=dict)   # key -> yesterday's OI

    def oi_change(self, contract: OptionContract) -> int | None:
        """Contracts opened (positive) or closed (negative) since the last snapshot.

        None when there is no prior snapshot — a first run knows nothing about
        change, and guessing zero would read as "nothing happened".
        """
        before = self.previous.get(contract.key)
        return None if before is None else contract.open_interest - before

    @property
    def has_baseline(self) -> bool:
        return bool(self.previous)


@dataclass(frozen=True)
class InsiderTrade:
    """A Form 4 line.

    The one signal here where direction is genuinely knowable: a person with the
    best view of the business chose to buy or sell. The cost is latency — Form 4
    is due within two business days of the trade, so this is always news about
    something that already happened.
    """

    ticker: str
    insider: str
    title: str
    code: str              # P=purchase S=sale A=grant M=exercise F=tax G=gift
    shares: float
    price: float
    traded_on: str         # ISO date
    filed_at: datetime
    accession: str
    is_officer: bool = False
    is_director: bool = False
    is_ten_percent: bool = False
    planned_10b5_1: bool = False
    shares_after: float | None = None
    url: str | None = None

    @property
    def value(self) -> float:
        return self.shares * self.price

    @property
    def is_purchase(self) -> bool:
        return self.code == "P"

    @property
    def is_open_market_sale(self) -> bool:
        return self.code == "S"

    @property
    def filing_lag_days(self) -> int:
        traded = datetime.fromisoformat(self.traded_on).date()
        return (self.filed_at.date() - traded).days


@dataclass
class Alert:
    """Something worth telling you about, plus what it does and doesn't mean."""

    ticker: str
    signal: str
    severity: Severity
    headline: str
    occurred_at: datetime
    facts: list[str] = field(default_factory=list)
    #: The single interpretive line: what this is evidence of, what to check.
    #: Never a recommendation — several of these signals carry no direction at
    #: all, and inventing one would be worse than saying so.
    read: str = ""
    caveats: list[str] = field(default_factory=list)
    url: str | None = None
    #: Parts that make this event unique, so a re-poll never notifies twice.
    identity: tuple[str, ...] = ()

    @property
    def dedup_key(self) -> str:
        payload = "|".join((self.signal, self.ticker, *self.identity))
        return hashlib.sha1(payload.encode()).hexdigest()[:20]


@dataclass
class SourceIssue:
    """A data source that cannot be trusted right now.

    Silence from a broken feed is indistinguishable from a quiet market, which
    is the failure this project most needs to avoid. Every one of these is
    surfaced rather than logged and forgotten.
    """

    source: str
    ticker: str
    kind: str              # unreachable | stale | corrupt | empty
    detail: str

    def line(self) -> str:
        icon = {"unreachable": "🔌", "stale": "🧊", "corrupt": "☣️", "empty": "␀"}
        scope = f"{self.source}/{self.ticker}" if self.ticker != "*" else self.source
        return f"{icon.get(self.kind, '•')} {scope} — {self.kind}: {self.detail}"


def humanise(value: float) -> str:
    """2_452_910 -> '2.45M', 12_000 -> '12K'. Used everywhere alerts quote a size.

    Trailing zeros are stripped because '12.00K shares' reads like a precision
    claim the number does not have.
    """
    for cut, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= cut:
            scaled = f"{value / cut:,.2f}".rstrip("0").rstrip(".")
            return f"{scaled}{suffix}"
    return f"{value:,.0f}"


def money(value: float) -> str:
    return f"${humanise(value)}"


def ago(delta: timedelta) -> str:
    minutes = delta.total_seconds() / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    if minutes < 36 * 60:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f} days"
