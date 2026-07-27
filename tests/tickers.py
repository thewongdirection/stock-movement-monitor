"""Realistic ticker profiles for tests.

Detection logic that only ever sees one synthetic mega-cap will pass its tests
and then misbehave on a $3 biotech. Each profile below encodes a genuinely
different shape of market data — price scale, liquidity, volume steadiness —
because those are the axes the thresholds actually interact with.
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from monitor.market_calendar import ET, is_trading_day
from monitor.models import Bar


@dataclass(frozen=True)
class Profile:
    symbol: str
    price: float
    #: Shares in a typical 5-minute bar.
    bar_volume: int
    #: Coefficient of variation on that volume — how erratic the name is.
    volume_noise: float
    #: Average daily volume in shares.
    adv: float
    note: str

    @property
    def bar_notional(self) -> float:
        return self.price * self.bar_volume


PROFILES: dict[str, Profile] = {
    # Enormous, steady, tight — a 5-minute bar is worth millions.
    "AAPL": Profile("AAPL", 336.80, 250_000, 0.18, 55_000_000, "mega-cap, very liquid"),
    "MSFT": Profile("MSFT", 512.40, 120_000, 0.20, 22_000_000, "mega-cap"),
    # Habitually erratic: high natural volume variance, so a fixed z-score
    # threshold behaves differently here than in AAPL.
    "TSLA": Profile("TSLA", 402.15, 300_000, 0.55, 88_000_000, "high-beta, erratic volume"),
    "NVDA": Profile("NVDA", 178.90, 500_000, 0.45, 190_000_000, "most-traded, erratic"),
    # Mid-cap: bar notional near the default min_bar_notional threshold.
    "CRWD": Profile("CRWD", 412.00, 9_000, 0.30, 2_100_000, "mid-cap growth"),
    "PLTR": Profile("PLTR", 62.30, 90_000, 0.40, 41_000_000, "retail-heavy mid-cap"),
    # Small-cap: thin enough that %-of-ADV matters far more than dollar size.
    "SMCI": Profile("SMCI", 38.75, 22_000, 0.50, 9_500_000, "small-cap, volatile"),
    # Low-priced: a 50,000-share print is only $185k here, so share-count and
    # dollar thresholds disagree sharply.
    "PENNY": Profile("PENNY", 3.70, 40_000, 0.65, 6_000_000, "low-priced, share-count trap"),
    # Illiquid: below min_bar_notional almost always. Should stay silent.
    "THIN": Profile("THIN", 14.20, 700, 0.80, 180_000, "illiquid micro-cap"),
    # Very high priced: few shares, large dollars.
    "BRK.A": Profile("BRK.A", 742_000.0, 3, 0.40, 900, "ultra-high price, tiny share counts"),
}


def bars_for(
    profile: Profile | str,
    *,
    sessions: int = 21,
    slots: int = 12,
    anchor: datetime | None = None,
    seed: int | None = None,
    interval_minutes: int = 5,
) -> list[Bar]:
    """Deterministic bars matching a profile's price and liquidity character."""
    p = PROFILES[profile] if isinstance(profile, str) else profile
    anchor = anchor or datetime(2026, 7, 27, 10, 0, tzinfo=ET)
    # crc32, not hash(): Python randomises string hashing per process, so
    # seeding from hash() would make these fixtures differ between runs and
    # produce tests that pass locally and fail in CI on a bad seed.
    rnd = random.Random(seed if seed is not None else zlib.crc32(p.symbol.encode()))

    days: list[datetime] = []
    cursor = anchor
    while len(days) < sessions:
        if is_trading_day(cursor.date()):
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.sort()

    bars: list[Bar] = []
    price = p.price
    for day in days:
        for slot in range(slots):
            ts = day + timedelta(minutes=interval_minutes * slot)
            # Bounded so the volume never goes non-positive on a noisy profile.
            factor = max(0.15, 1.0 + rnd.gauss(0, p.volume_noise))
            volume = max(1, int(p.bar_volume * factor))
            drift = 1.0 + rnd.gauss(0, 0.0015)
            close = price * drift
            bars.append(
                Bar(
                    ts=ts,
                    open=price,
                    high=max(price, close) * 1.0008,
                    low=min(price, close) * 0.9992,
                    close=close,
                    volume=volume,
                )
            )
            price = close
    return bars


def spike(bars: list[Bar], multiple: float, move_pct: float = 2.5) -> list[Bar]:
    """Replace the last bar with one carrying `multiple`x its slot's normal volume.

    Deliberately measured against *the same clock slot on previous sessions*,
    not against the last bar's own randomly-drawn volume. The detector
    normalises by time of day, so if the fixture multiplied a random value the
    resulting RVOL would be random too — "10x" has to actually mean 10x for the
    threshold assertions to mean anything.
    """
    out = list(bars)
    last = out[-1]
    slot = last.ts.time()
    baseline = [
        b.volume
        for b in out[:-1]
        if b.ts.time() == slot and b.ts.date() != last.ts.date()
    ]
    typical = (sum(baseline) / len(baseline)) if baseline else last.volume

    close = last.open * (1 + move_pct / 100)
    out[-1] = Bar(
        ts=last.ts,
        open=last.open,
        high=max(last.open, close) * 1.002,
        low=min(last.open, close) * 0.999,
        close=close,
        volume=max(1, int(typical * multiple)),
    )
    return out
