"""Shared fixtures.

Everything here is deterministic: fixed clocks, in-memory databases, captured
bars. No test touches the network, and none of them depend on what the market
did today.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from monitor.clock import ET
from monitor.config import Config, load
from monitor.models import Bar, InsiderTrade, OptionChain, OptionContract, Trade
from monitor.store import Store

FIXTURES = Path(__file__).parent / "fixtures"

#: A Friday, mid-session. Chosen because it is a plain trading day: no holiday,
#: no half day, and far from a DST boundary.
NOW = datetime(2026, 7, 24, 12, 0, tzinfo=ET)


@pytest.fixture
def store() -> Store:
    made = Store(":memory:")
    yield made
    made.close()


@pytest.fixture
def config() -> Config:
    made = Config()
    made.values["watchlist"] = ["NVDA", "MSFT"]
    return made


@pytest.fixture
def now() -> datetime:
    return NOW


def make_bars(*, sessions: int = 10, slot_volumes: dict[str, int] | None = None,
              base_volume: int = 1_000_000, price: float = 200.0,
              end: datetime = NOW, interval: int = 30) -> list[Bar]:
    """Build a tidy history: `sessions` days of identical bars ending at `end`.

    Uniform by design — a test that wants an anomaly injects one, and anything
    the signal fires on is therefore the injected thing and not noise.
    """
    slots = [(9, 30), (10, 0), (10, 30), (11, 0), (11, 30), (12, 0)]
    bars: list[Bar] = []
    day = end.date()
    made = 0
    while made < sessions:
        from monitor.clock import is_trading_day
        if not is_trading_day(day):
            day -= timedelta(days=1)
            continue
        for hour, minute in slots:
            stamp = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)
            if stamp + timedelta(minutes=interval) > end:
                continue
            key = f"{hour:02d}:{minute:02d}"
            volume = (slot_volumes or {}).get(key, base_volume)
            bars.append(Bar(ts=stamp, open=price, high=price * 1.002,
                            low=price * 0.998, close=price, volume=volume))
        made += 1
        day -= timedelta(days=1)
    bars.sort(key=lambda b: b.ts)
    return bars


def spike(bars: list[Bar], *, at: datetime, volume: int, close: float) -> list[Bar]:
    """Replace one bar with an anomalous one."""
    return [
        Bar(ts=b.ts, open=b.open, high=max(b.high, close), low=min(b.low, close),
            close=close, volume=volume) if b.ts == at else b
        for b in bars
    ]


def make_chain(ticker: str = "NVDA", *, as_of: str = "2026-07-24",
               contracts: list[tuple[float, str, int]] | None = None,
               previous: dict[str, int] | None = None) -> OptionChain:
    rows = contracts or [(200.0, "call", 52_400), (195.0, "put", 16_200)]
    made = [
        OptionContract(ticker=ticker, expiry="2026-08-21", strike=strike,
                       right=right, open_interest=oi, as_of=as_of)
        for strike, right, oi in rows
    ]
    return OptionChain(ticker=ticker, as_of=as_of, contracts=made,
                       previous=previous or {})


def make_filing(**kwargs) -> InsiderTrade:
    defaults = dict(
        ticker="NVDA", insider="DOE JANE", title="Chief Financial Officer",
        code="P", shares=12_000.0, price=205.44, traded_on="2026-07-21",
        filed_at=datetime(2026, 7, 23, 9, 0, tzinfo=ET),
        accession="0001045810-26-000091", is_officer=True, shares_after=148_200.0,
    )
    defaults.update(kwargs)
    return InsiderTrade(**defaults)


def make_trade(**kwargs) -> Trade:
    defaults = dict(ticker="NVDA", ts=NOW - timedelta(minutes=5),
                    price=205.0, size=30_000)
    defaults.update(kwargs)
    return Trade(**defaults)


@pytest.fixture
def replay_dir(tmp_path: Path) -> Path:
    """A minimal replay capture with two sessions of NVDA bars and two chains."""
    root = tmp_path / "replay"
    (root / "bars").mkdir(parents=True)
    (root / "options").mkdir()
    (root / "insider").mkdir()

    bars = make_bars(sessions=8)
    (root / "bars" / "NVDA.json").write_text(json.dumps({
        "ticker": "NVDA", "interval_minutes": 30,
        "bars": [{"ts": b.ts.isoformat(), "o": b.open, "h": b.high,
                  "l": b.low, "c": b.close, "v": b.volume} for b in bars],
    }))
    (root / "options" / "NVDA.json").write_text(json.dumps({
        "ticker": "NVDA",
        "snapshots": {
            "2026-07-23": [{"expiry": "2026-08-21", "strike": 200, "right": "call", "oi": 34_051}],
            "2026-07-24": [{"expiry": "2026-08-21", "strike": 200, "right": "call", "oi": 52_400}],
        },
    }))
    (root / "insider" / "NVDA.json").write_text(json.dumps({"filings": [{
        "insider": "DOE JANE", "title": "CFO", "code": "P", "shares": 12_000,
        "price": 205.44, "traded_on": "2026-07-21",
        "filed_at": "2026-07-23T09:00:00-04:00", "accession": "acc-1",
        "is_officer": True,
    }]}))
    return root


@pytest.fixture
def config_file(tmp_path: Path, replay_dir: Path) -> Path:
    """A config wired entirely to replay, so a test can drive the real CLI."""
    path = tmp_path / "config.yaml"
    path.write_text(f"""
watchlist: [NVDA]
sources:
  bars: replay
  options: replay
  insider: replay
  trades: off
  replay_dir: {replay_dir}
canslim:
  enabled: false
notify:
  channel: console
state:
  path: {tmp_path / 'monitor.db'}
""")
    return path
