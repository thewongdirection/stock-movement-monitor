from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from monitor import config as config_mod
from monitor.detectors.base import Context
from monitor.market_calendar import ET
from monitor.models import Bar, InsiderTransaction, OptionTrade, Side, Trade
from monitor.state import State

#: A Monday, mid-morning, inside the regular session. Six minutes after the
#: last synthetic bar closes, so that bar counts as complete and falls inside
#: the default 30-minute cold-start window.
NOW = datetime(2026, 7, 27, 11, 1, tzinfo=ET)
LAST_BAR_ANCHOR = datetime(2026, 7, 27, 10, 0, tzinfo=ET)


@pytest.fixture
def state(tmp_path):
    with State(tmp_path / "test.db") as s:
        yield s


def make_config(**detector_overrides) -> config_mod.Config:
    """A config with every detector on, then selectively overridden."""
    detectors = {name: {"enabled": True} for name in config_mod.DETECTOR_SPECS}
    for name, settings in detector_overrides.items():
        detectors.setdefault(name, {}).update(settings)
    return config_mod.from_dict({"tickers": ["TEST"], "detectors": detectors})


def make_context(state, settings, now=None, **kwargs) -> Context:
    return Context(
        ticker="TEST",
        now=now or NOW,
        settings=settings,
        state=state,
        cold_start_minutes=kwargs.pop("cold_start_minutes", 30),
        **kwargs,
    )


def build_bars(
    *,
    sessions: int = 21,
    slots: int = 12,
    base_volume: int = 100_000,
    jitter: int = 2_000,
    price: float = 100.0,
    last_day: datetime | None = None,
) -> list[Bar]:
    """Synthetic 5-minute bars with a flat, low-variance baseline.

    Sessions run 10:00-11:00 ET, deliberately away from the open so the
    warmup filter doesn't interfere. Volume varies by a small deterministic
    amount so the standard deviation is non-zero but tiny — that makes a
    planted spike unambiguous.
    """
    anchor = last_day or LAST_BAR_ANCHOR
    bars: list[Bar] = []
    day_offsets = _trading_day_offsets(anchor, sessions)
    for day_index, day in enumerate(day_offsets):
        for slot in range(slots):
            ts = day + timedelta(minutes=5 * slot)
            volume = base_volume + ((day_index * 7 + slot * 3) % 5) * jitter
            bars.append(
                Bar(
                    ts=ts,
                    open=price,
                    high=price * 1.001,
                    low=price * 0.999,
                    close=price,
                    volume=volume,
                )
            )
    bars.sort(key=lambda b: b.ts)
    return bars


def _trading_day_offsets(anchor: datetime, sessions: int) -> list[datetime]:
    from monitor.market_calendar import is_trading_day

    days: list[datetime] = []
    cursor = anchor
    while len(days) < sessions:
        if is_trading_day(cursor.date()):
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def spike_last_bar(bars: list[Bar], multiple: float) -> list[Bar]:
    """Replace the final bar with one carrying `multiple`x its volume."""
    out = list(bars)
    last = out[-1]
    out[-1] = Bar(
        ts=last.ts,
        open=last.open,
        high=last.open * 1.03,
        low=last.open * 0.999,
        close=last.open * 1.025,
        volume=int(last.volume * multiple),
    )
    return out


def make_trade(
    *,
    size: int = 50_000,
    price: float = 100.0,
    minutes_ago: int = 2,
    now: datetime | None = None,
    bid: float | None = 99.98,
    ask: float | None = 100.02,
    off_exchange: bool = True,
    raw_id: str | None = "print-1",
) -> Trade:
    now = now or NOW
    return Trade(
        ticker="TEST",
        ts=now - timedelta(minutes=minutes_ago),
        price=price,
        size=size,
        venue="TRF",
        is_off_exchange=off_exchange,
        side=Side.BUY if bid and ask and price > (bid + ask) / 2 else Side.UNKNOWN,
        nbbo_bid=bid,
        nbbo_ask=ask,
        raw_id=raw_id,
    )


def make_option_trade(
    *,
    premium: float = 250_000,
    option_type: str = "call",
    strike: float = 110.0,
    days_out: int = 30,
    volume: int | None = 5_000,
    open_interest: int | None = 1_000,
    trade_type: str = "sweep",
    minutes_ago: int = 2,
    now: datetime | None = None,
    underlying: float | None = 100.0,
    raw_id: str = "flow-1",
) -> OptionTrade:
    now = now or NOW
    ts = now - timedelta(minutes=minutes_ago)
    return OptionTrade(
        ticker="TEST",
        ts=ts,
        premium=premium,
        option_type=option_type,
        strike=strike,
        expiry=(ts.date() + timedelta(days=days_out)).isoformat(),
        size=100,
        volume=volume,
        open_interest=open_interest,
        trade_type=trade_type,
        underlying_price=underlying,
        raw_id=raw_id,
    )


def make_insider_txn(
    *,
    code: str = "P",
    shares: float = 5_000,
    price: float = 100.0,
    traded_days_ago: int = 2,
    insider: str = "Jane Doe",
    title: str = "Chief Executive Officer",
    is_officer: bool = True,
    is_director: bool = False,
    is_10b5_1: bool = False,
    is_derivative: bool = False,
    accession: str = "0001234567-26-000001",
    now: datetime | None = None,
) -> InsiderTransaction:
    now = now or NOW
    traded = (now - timedelta(days=traded_days_ago)).date()
    return InsiderTransaction(
        ticker="TEST",
        issuer_name="Test Corp",
        insider_name=insider,
        insider_title=title,
        is_director=is_director,
        is_officer=is_officer,
        is_ten_pct_owner=False,
        transaction_code=code,
        acquired_disposed="A" if code == "P" else "D",
        shares=shares,
        price_per_share=price,
        transaction_date=traded.isoformat(),
        filed_at=now - timedelta(minutes=10),
        accession=accession,
        is_derivative=is_derivative,
        is_10b5_1=is_10b5_1,
        shares_owned_after=50_000,
        url="https://www.sec.gov/example",
    )
