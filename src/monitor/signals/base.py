"""What every signal is handed, and what it must give back.

A signal receives already-fetched data and returns alerts. It does no I/O, which
is what makes the whole set testable against captured history and what stops one
slow feed from being fetched four times.

The `Context` is deliberately per-ticker. Cross-ticker reasoning — "the feed is
frozen because nothing advanced anywhere" — belongs to health checking, not to a
signal, because a signal that can see the whole watchlist starts making claims
it cannot support from one name's data.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from ..clock import slot_of
from ..config import Config
from ..models import Alert, Bar, InsiderTrade, OptionChain, Trade
from ..store import Store


@dataclass
class Context:
    ticker: str
    now: datetime
    config: Config
    store: Store
    bars: list[Bar] = field(default_factory=list)
    chain: OptionChain | None = None
    trades: list[Trade] = field(default_factory=list)
    filings: list[InsiderTrade] = field(default_factory=list)

    def setting(self, path: str):
        """Read a setting with this ticker's overrides applied."""
        return self.config.get(path, self.ticker)

    @property
    def latest(self) -> Bar | None:
        return self.bars[-1] if self.bars else None


class Signal(Protocol):
    name: str

    def evaluate(self, ctx: Context) -> list[Alert]:
        """Zero or more alerts. Never raises for ordinary absence of data."""


@dataclass(frozen=True)
class Baseline:
    """What the same clock slot normally looks like on other days.

    Same *slot*, not a rolling window over recent bars. Volume at 09:30 is
    several times volume at 12:30 on a perfectly ordinary day, so a rolling
    comparison would flag every open as an anomaly and never notice a genuinely
    heavy lunchtime.
    """

    slot: str
    samples: int
    median: float
    mean: float
    stdev: float

    def rvol(self, volume: float) -> float | None:
        return volume / self.median if self.median > 0 else None

    def zscore(self, volume: float) -> float | None:
        return (volume - self.mean) / self.stdev if self.stdev > 0 else None


def build_baseline(bars: list[Bar], target: Bar, slot_minutes: int,
                   min_samples: int) -> Baseline | None:
    """Volume distribution for `target`'s slot on *other* days.

    Returns None rather than a fabricated baseline when there are too few
    samples. An RVOL computed from two observations is a number, not evidence,
    and a monitor that quietly emits one is worse than one that says nothing.
    """
    slot = slot_of(target.ts, slot_minutes)
    day = target.ts.date()
    volumes = [
        float(bar.volume) for bar in bars
        if bar.ts.date() != day and slot_of(bar.ts, slot_minutes) == slot
    ]
    if len(volumes) < min_samples:
        return None
    return Baseline(
        slot=slot,
        samples=len(volumes),
        median=statistics.median(volumes),
        mean=statistics.fmean(volumes),
        stdev=statistics.stdev(volumes) if len(volumes) > 1 else 0.0,
    )


def registry(config: Config) -> list[Signal]:
    """Every enabled signal, in the order alerts should be read.

    Open interest leads because it is the only one that shows a position was
    actually taken; volume follows because it is the most frequent and the least
    conclusive.
    """
    from .blocks import BlockSignal
    from .insider import InsiderSignal
    from .open_interest import OpenInterestSignal
    from .volume import VolumeSignal

    candidates: list[Signal] = [
        OpenInterestSignal(), InsiderSignal(), BlockSignal(), VolumeSignal(),
    ]
    return [s for s in candidates if config.get(f"signals.{s.name}.enabled")]
