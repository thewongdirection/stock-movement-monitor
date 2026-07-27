"""Provider payload parsing and message formatting — no network involved.

The Unusual Whales tests matter more than they look: their API docs are not
readable without an account, so these lock in the tolerant-extraction contract.
A renamed field should cost one attribute, never the whole row.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from monitor.models import Alert, Severity, Side
from monitor.notify.base import ConsoleNotifier, format_alert, chunk
from monitor.providers.fmp import _parse_bars
from monitor.providers.unusual_whales import (
    _infer_side,
    _to_option_trade,
    _to_trade,
)


# --------------------------------------------------------------------------
# FMP bars
# --------------------------------------------------------------------------
def test_parses_a_bare_list_of_bars():
    bars = _parse_bars(
        [
            {"date": "2026-07-27 10:05:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100},
            {"date": "2026-07-27 10:00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 200},
        ]
    )
    assert len(bars) == 2
    # Sorted oldest-first regardless of the order the API returned.
    assert bars[0].ts < bars[1].ts
    assert bars[0].volume == 200


def test_parses_an_enveloped_response():
    bars = _parse_bars(
        {"historical": [{"date": "2026-07-27 10:00:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 5}]}
    )
    assert len(bars) == 1


def test_naive_timestamps_are_treated_as_eastern():
    bars = _parse_bars(
        [{"date": "2026-07-27 10:00:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 5}]
    )
    assert bars[0].ts.utcoffset().total_seconds() == -4 * 3600  # EDT in July


def test_unparseable_rows_are_skipped_not_fatal():
    bars = _parse_bars(
        [
            {"date": "nonsense", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 5},
            {"date": "2026-07-27 10:00:00", "open": 1, "high": 1, "low": 1, "close": 1},
            {"date": "2026-07-27 10:05:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 9},
        ]
    )
    assert len(bars) == 2  # the missing-volume row defaults to 0, the bad date is dropped


def test_bar_notional_uses_the_typical_price():
    bars = _parse_bars(
        [{"date": "2026-07-27 10:00:00", "open": 10, "high": 12, "low": 9, "close": 12, "volume": 100}]
    )
    assert bars[0].notional == pytest.approx((12 + 9 + 12) / 3 * 100)


# --------------------------------------------------------------------------
# Unusual Whales — dark pool prints
# --------------------------------------------------------------------------
def test_dark_pool_row_parses_with_the_documented_field_names():
    trade = _to_trade(
        {
            "tracking_id": "abc123",
            "ticker": "AAPL",
            "price": "182.35",
            "size": 40_000,
            "executed_at": "2026-07-27T14:30:00Z",
            "nbbo_bid": "182.30",
            "nbbo_ask": "182.40",
            "market_center": "L",
        },
        "AAPL",
    )
    assert trade is not None
    assert trade.size == 40_000
    assert trade.price == pytest.approx(182.35)
    assert trade.notional == pytest.approx(7_294_000)
    assert trade.is_off_exchange is True
    assert trade.raw_id == "abc123"


def test_dark_pool_row_survives_renamed_fields():
    """Alternative key spellings still yield a usable trade."""
    trade = _to_trade(
        {"id": "x", "symbol": "AAPL", "fill_price": 100.0, "quantity": 1_000, "timestamp": 1785000000},
        "AAPL",
    )
    assert trade is not None
    assert trade.price == 100.0
    assert trade.size == 1_000


def test_millisecond_timestamps_are_detected():
    trade = _to_trade({"price": 1, "size": 1, "timestamp": 1785000000000}, "AAPL")
    assert trade is not None
    assert trade.ts.year == 2026


def test_row_missing_essentials_is_dropped_not_guessed():
    assert _to_trade({"ticker": "AAPL"}, "AAPL") is None
    assert _to_trade({"price": 1, "size": 1}, "AAPL") is None  # no timestamp
    assert _to_trade("not a dict", "AAPL") is None


def test_ticker_falls_back_when_the_row_omits_it():
    trade = _to_trade({"price": 1, "size": 1, "executed_at": "2026-07-27T14:30:00Z"}, "msft")
    assert trade is not None and trade.ticker == "MSFT"


@pytest.mark.parametrize(
    "price,bid,ask,expected",
    [
        (100.09, 100.00, 100.10, Side.BUY),    # near the ask
        (100.01, 100.00, 100.10, Side.SELL),   # near the bid
        (100.05, 100.00, 100.10, Side.UNKNOWN),  # midpoint cross
        (100.05, None, None, Side.UNKNOWN),    # no quote context
        (100.05, 100.10, 100.00, Side.UNKNOWN),  # crossed/invalid quote
    ],
)
def test_side_inference_follows_the_quote_rule(price, bid, ask, expected):
    assert _infer_side(price, bid, ask) is expected


def test_explicit_side_beats_inference():
    trade = _to_trade(
        {
            "price": 100.09,
            "size": 1,
            "executed_at": "2026-07-27T14:30:00Z",
            "nbbo_bid": 100.00,
            "nbbo_ask": 100.10,
            "side": "sell",
        },
        "AAPL",
    )
    assert trade is not None and trade.side is Side.SELL


# --------------------------------------------------------------------------
# Unusual Whales — options flow
# --------------------------------------------------------------------------
def test_flow_row_parses():
    trade = _to_option_trade(
        {
            "id": "f1",
            "ticker": "AAPL",
            "type": "call",
            "strike": "190",
            "expiry": "2026-08-21",
            "total_premium": "425000",
            "volume": 4_200,
            "open_interest": 900,
            "has_sweep": True,
            "created_at": "2026-07-27T14:30:00Z",
            "underlying_price": "182.40",
            "total_size": 1_500,
        },
        "AAPL",
    )
    assert trade is not None
    assert trade.premium == pytest.approx(425_000)
    assert trade.option_type == "call"
    assert trade.trade_type == "sweep"
    assert trade.volume == 4_200
    assert trade.open_interest == 900
    assert trade.dte == 25


def test_premium_is_derived_when_absent():
    """price x size x 100 — the standard contract multiplier."""
    trade = _to_option_trade(
        {
            "ticker": "AAPL",
            "put_call": "P",
            "strike": 180,
            "expiry": "2026-08-21",
            "price": 3.50,
            "total_size": 500,
            "created_at": "2026-07-27T14:30:00Z",
        },
        "AAPL",
    )
    assert trade is not None
    assert trade.premium == pytest.approx(175_000)
    assert trade.option_type == "put"


def test_flow_row_without_a_recognisable_type_is_dropped():
    assert (
        _to_option_trade(
            {"ticker": "AAPL", "strike": 1, "expiry": "2026-08-21", "total_premium": 1,
             "created_at": "2026-07-27T14:30:00Z"},
            "AAPL",
        )
        is None
    )


def test_moneyness_sign_flips_for_puts():
    common = {
        "ticker": "AAPL",
        "expiry": "2026-08-21",
        "total_premium": 200_000,
        "created_at": "2026-07-27T14:30:00Z",
        "underlying_price": 100.0,
    }
    call = _to_option_trade({**common, "type": "call", "strike": 90.0}, "AAPL")
    put = _to_option_trade({**common, "type": "put", "strike": 90.0}, "AAPL")
    assert call is not None and put is not None
    assert call.moneyness_pct == pytest.approx(10.0)   # ITM call
    assert put.moneyness_pct == pytest.approx(-10.0)   # OTM put


def test_trade_type_falls_back_to_unknown_rather_than_lying():
    trade = _to_option_trade(
        {
            "ticker": "AAPL",
            "type": "call",
            "strike": 100,
            "expiry": "2026-08-21",
            "total_premium": 200_000,
            "created_at": "2026-07-27T14:30:00Z",
        },
        "AAPL",
    )
    assert trade is not None and trade.trade_type == "unknown"


# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------
def alert(**kwargs) -> Alert:
    base = dict(
        ticker="AAPL",
        detector="dark_pool",
        severity=Severity.HIGH,
        headline="Dark pool print — $5.00M",
        occurred_at=datetime(2026, 7, 27, 14, 30, tzinfo=timezone.utc),
        lines=["<b>50.0K</b> shares at $100.00"],
    )
    base.update(kwargs)
    return Alert(**base)


def test_format_includes_the_ticker_severity_and_body():
    text = format_alert(alert())
    assert "AAPL" in text
    assert "🔴" in text
    assert "Dark pool" in text
    assert "50.0K" in text


def test_format_links_a_filing_when_there_is_one():
    text = format_alert(alert(url="https://www.sec.gov/x"))
    assert '<a href="https://www.sec.gov/x">View filing</a>' in text


def test_dedup_id_is_stable_and_distinguishes_alerts():
    one = alert(dedup_parts=("print-1",))
    same = alert(dedup_parts=("print-1",))
    other = alert(dedup_parts=("print-2",))
    assert one.dedup_id == same.dedup_id
    assert one.dedup_id != other.dedup_id


def test_dedup_id_distinguishes_detectors_on_identical_events():
    from_block = alert(detector="block_trades", dedup_parts=("print-1",))
    from_dark = alert(detector="dark_pool", dedup_parts=("print-1",))
    assert from_block.dedup_id != from_dark.dedup_id


def test_long_messages_split_on_line_boundaries():
    body = "\n".join(f"line {i}" * 20 for i in range(200))
    parts = chunk(body, limit=1000)
    assert len(parts) > 1
    assert all(len(p) <= 1000 for p in parts)
    # Nothing is lost in the split.
    assert sum(p.count("line") for p in parts) == body.count("line")


def test_provider_text_is_escaped_before_it_reaches_the_message():
    """An insider named `Doe Jane <Q>` must not break Telegram's HTML parser."""
    from conftest import make_config, make_context, make_insider_txn
    from monitor.detectors import InsiderTradeDetector
    from monitor.state import State

    with State(":memory:") as state:
        txn = make_insider_txn(
            insider="Doe Jane <Q> & Co", title="CEO & Chair", shares=5_000, price=100.0
        )
        ctx = make_context(
            state,
            make_config(insider_trades={}).detector("insider_trades"),
            insider_transactions=[txn],
        )
        text = format_alert(InsiderTradeDetector().run(ctx)[0])

    assert "Doe Jane &lt;Q&gt; &amp; Co" in text
    assert "<Q>" not in text
    # Our own markup is untouched.
    assert "<b>" in text


def test_console_notifier_strips_markup_for_the_terminal(capsys):
    ConsoleNotifier().send(alert(lines=["<b>bold</b> &amp; escaped"]))
    out = capsys.readouterr().out
    assert "<b>" not in out
    assert "bold & escaped" in out
