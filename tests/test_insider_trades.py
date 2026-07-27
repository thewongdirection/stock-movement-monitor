"""Form 4 parsing and the insider detector's editorial judgements."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import NOW, make_config, make_context, make_insider_txn

from monitor.detectors import InsiderTradeDetector
from monitor.models import Severity
from monitor.providers.sec_edgar import (
    category_for,
    is_placeholder_user_agent,
    parse_form4,
)

FIXTURES = Path(__file__).parent / "fixtures"
FILED_AT = datetime(2026, 7, 27, 13, 5, tzinfo=timezone.utc)


def settings(**overrides):
    return make_config(insider_trades=overrides).detector("insider_trades")


def load(name: str):
    return parse_form4(
        (FIXTURES / name).read_text(),
        fallback_ticker="TEST",
        accession="0001234567-26-000001",
        filed_at=FILED_AT,
        url="https://www.sec.gov/example",
    )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def test_parses_a_purchase_and_its_derivative_line():
    txns = load("form4_purchase.xml")
    assert len(txns) == 2

    buy = txns[0]
    assert buy.ticker == "TEST"
    assert buy.transaction_code == "P"
    assert buy.shares == 12_500
    assert buy.price_per_share == pytest.approx(184.25)
    assert buy.notional == pytest.approx(2_303_125)
    assert buy.transaction_date == "2026-07-24"
    assert buy.shares_owned_after == 212_500
    assert buy.is_derivative is False
    assert buy.value_known is True

    exercise = txns[1]
    assert exercise.is_derivative is True
    assert exercise.transaction_code == "M"
    assert exercise.value_known is False  # price stated as 0


def test_xml_entities_survive_parsing():
    """Issuer and insider names carry &amp; and &lt;&gt; in real filings."""
    buy = load("form4_purchase.xml")[0]
    assert buy.issuer_name == "Test Corp & Sons"
    assert buy.insider_name == "Doe Jane <Q>"
    assert buy.insider_title == "Chief Executive Officer"
    assert buy.is_officer is True
    assert buy.is_director is False
    assert "Chief Executive Officer" in buy.role


def test_detects_the_10b5_1_checkbox_and_the_footnote():
    sale = load("form4_planned_sale.xml")[0]
    assert sale.transaction_code == "S"
    assert sale.is_10b5_1 is True
    assert sale.is_director and sale.is_officer
    assert "Chief Financial Officer" in sale.role
    assert "Director" in sale.role


def test_malformed_xml_raises_rather_than_returning_nothing():
    from monitor.providers.base import ProviderError

    with pytest.raises(ProviderError, match="malformed"):
        parse_form4(
            "<ownershipDocument><unclosed>",
            fallback_ticker="TEST",
            accession="x",
            filed_at=FILED_AT,
        )


@pytest.mark.parametrize(
    "code,expected",
    [("P", "purchase"), ("S", "sale"), ("A", "grant"), ("M", "exercise"),
     ("X", "exercise"), ("F", "tax"), ("G", "gift"), ("Z", "other")],
)
def test_transaction_codes_map_to_analyst_categories(code, expected):
    assert category_for(code) == expected


def test_example_user_agent_is_rejected():
    assert is_placeholder_user_agent("stock-movement-monitor you@example.com")
    assert not is_placeholder_user_agent("stock-movement-monitor real.person@gmail.com")


# --------------------------------------------------------------------------
# Detector behaviour
# --------------------------------------------------------------------------
def test_purchase_above_the_floor_fires(state):
    ctx = make_context(
        state, settings(), insider_transactions=[make_insider_txn(shares=5_000, price=100.0)]
    )
    alerts = InsiderTradeDetector().run(ctx)
    assert len(alerts) == 1
    assert "Insider purchase" in alerts[0].headline
    assert alerts[0].url == "https://www.sec.gov/example"


def test_small_purchase_is_below_the_floor(state):
    ctx = make_context(
        state, settings(), insider_transactions=[make_insider_txn(shares=100, price=100.0)]
    )
    assert InsiderTradeDetector().run(ctx) == []


def test_sales_face_a_higher_bar_than_purchases(state):
    """$300k: over the purchase floor, under the sale floor. Same size, one alert."""
    buy = make_insider_txn(code="P", shares=3_000, price=100.0, accession="a-1")
    sell = make_insider_txn(code="S", shares=3_000, price=100.0, accession="a-2")
    ctx = make_context(state, settings(), insider_transactions=[buy, sell])
    alerts = InsiderTradeDetector().run(ctx)
    assert len(alerts) == 1
    assert "purchase" in alerts[0].headline


def test_planned_10b5_1_sale_is_excluded_by_default(state):
    sale = make_insider_txn(code="S", shares=10_000, price=100.0, is_10b5_1=True)
    assert (
        InsiderTradeDetector().run(
            make_context(state, settings(), insider_transactions=[sale])
        )
        == []
    )
    assert (
        len(
            InsiderTradeDetector().run(
                make_context(
                    state,
                    settings(exclude_10b5_1_sales=False),
                    insider_transactions=[sale],
                )
            )
        )
        == 1
    )


def test_10b5_1_purchases_are_kept_but_annotated(state):
    """The exclusion is about sales; a planned purchase still gets through."""
    buy = make_insider_txn(code="P", shares=10_000, price=100.0, is_10b5_1=True)
    alerts = InsiderTradeDetector().run(
        make_context(state, settings(), insider_transactions=[buy])
    )
    assert len(alerts) == 1
    assert any("10b5-1" in line for line in alerts[0].lines)


def test_derivative_lines_are_off_by_default(state):
    exercise = make_insider_txn(code="M", shares=10_000, price=100.0, is_derivative=True)
    assert (
        InsiderTradeDetector().run(
            make_context(
                state, settings(alert_on=["exercise"]), insider_transactions=[exercise]
            )
        )
        == []
    )
    assert (
        len(
            InsiderTradeDetector().run(
                make_context(
                    state,
                    settings(alert_on=["exercise"], include_derivative=True),
                    insider_transactions=[exercise],
                )
            )
        )
        == 1
    )


def test_grants_are_not_reported_unless_asked_for(state):
    grant = make_insider_txn(code="A", shares=50_000, price=100.0)
    assert (
        InsiderTradeDetector().run(
            make_context(state, settings(), insider_transactions=[grant])
        )
        == []
    )
    assert (
        len(
            InsiderTradeDetector().run(
                make_context(
                    state, settings(alert_on=["grant"]), insider_transactions=[grant]
                )
            )
        )
        == 1
    )


def test_cluster_buy_escalates_to_high(state):
    """Two different insiders buying inside the window is the strongest signal."""
    first = make_insider_txn(insider="Jane Doe", shares=2_000, price=100.0, accession="a-1")
    second = make_insider_txn(insider="Richard Roe", shares=2_000, price=100.0, accession="a-2")
    ctx = make_context(state, settings(), insider_transactions=[first, second])
    alerts = InsiderTradeDetector().run(ctx)

    assert len(alerts) == 2
    # Both filings arrived in the same run, so both must carry the cluster flag.
    assert all(a.severity is Severity.HIGH for a in alerts)
    assert all(any("Cluster buy" in line for line in a.lines) for a in alerts)


def test_a_single_insider_filing_twice_is_not_a_cluster(state):
    """Cluster detection counts distinct people, not filings."""
    one = make_insider_txn(insider="Jane Doe", shares=2_000, price=100.0, accession="a-1")
    two = make_insider_txn(insider="Jane Doe", shares=3_000, price=100.0, accession="a-2")
    alerts = InsiderTradeDetector().run(
        make_context(state, settings(), insider_transactions=[one, two])
    )
    assert len(alerts) == 2
    assert not any("Cluster buy" in line for a in alerts for line in a.lines)


def test_a_filing_with_no_stated_price_is_still_reported(state):
    """Weighted-average fills state no price; dropping them would lose real buys."""
    txn = make_insider_txn(shares=8_000, price=0.0)
    alerts = InsiderTradeDetector().run(
        make_context(state, settings(), insider_transactions=[txn])
    )
    assert len(alerts) == 1
    assert "value not stated" in alerts[0].headline
    assert any("states no price" in line for line in alerts[0].lines)


def test_alert_shows_the_trade_to_filing_lag(state):
    txn = make_insider_txn(shares=5_000, price=100.0, traded_days_ago=2)
    alerts = InsiderTradeDetector().run(
        make_context(state, settings(), insider_transactions=[txn])
    )
    timing = [line for line in alerts[0].lines if "Traded" in line][0]
    assert "filed" in timing
    assert "2 days later" in timing


def test_large_purchase_by_a_ceo_reaches_high(state):
    txn = make_insider_txn(shares=20_000, price=100.0, title="Chief Executive Officer")
    alerts = InsiderTradeDetector().run(
        make_context(state, settings(), insider_transactions=[txn])
    )
    assert alerts[0].severity is Severity.HIGH


def test_modest_sale_stays_low_severity(state):
    txn = make_insider_txn(code="S", shares=8_000, price=100.0, title="VP Sales")
    alerts = InsiderTradeDetector().run(
        make_context(state, settings(), insider_transactions=[txn])
    )
    assert alerts[0].severity is Severity.LOW


def test_dedup_id_separates_two_lines_of_one_filing(state):
    """One Form 4 can carry several transactions; each needs its own identity."""
    txns = load("form4_purchase.xml")
    ctx = make_context(
        state,
        settings(alert_on=["purchase", "exercise"], include_derivative=True),
        insider_transactions=txns,
    )
    alerts = InsiderTradeDetector().run(ctx)
    assert len(alerts) == 2
    assert alerts[0].dedup_id != alerts[1].dedup_id


# --------------------------------------------------------------------------
# Cluster bookkeeping
# --------------------------------------------------------------------------
def test_a_repeat_buyer_refreshes_their_date(state):
    """The bug this covers: the row was INSERT OR IGNORE, so the date never moved.

    An insider who bought a year ago and buys again today would keep the year-old
    timestamp and fall outside every cluster window — silently removing the
    strongest insider signal there is.
    """
    long_ago = NOW - timedelta(days=200)
    state.record_insider_buyer("TEST", "Jane Doe", long_ago)
    assert state.recent_insider_buyers("TEST", NOW, days=30) == 0

    state.record_insider_buyer("TEST", "Jane Doe", NOW)
    assert state.recent_insider_buyers("TEST", NOW, days=30) == 1


def test_the_same_buyer_is_never_counted_twice(state):
    """One person buying three times is one insider, not a cluster of three."""
    for _ in range(3):
        state.record_insider_buyer("TEST", "Jane Doe", NOW)
    assert state.recent_insider_buyers("TEST", NOW, days=30) == 1


def test_buyer_names_are_matched_case_insensitively(state):
    state.record_insider_buyer("TEST", "Jane Doe", NOW)
    state.record_insider_buyer("TEST", "JANE DOE", NOW)
    assert state.recent_insider_buyers("TEST", NOW, days=30) == 1


def test_distinct_buyers_accumulate_into_a_cluster(state):
    state.record_insider_buyer("TEST", "Jane Doe", NOW)
    state.record_insider_buyer("TEST", "John Roe", NOW - timedelta(days=5))
    assert state.recent_insider_buyers("TEST", NOW, days=30) == 2


def test_buyers_in_another_ticker_are_not_counted(state):
    state.record_insider_buyer("TEST", "Jane Doe", NOW)
    state.record_insider_buyer("OTHER", "John Roe", NOW)
    assert state.recent_insider_buyers("TEST", NOW, days=30) == 1


def test_pruning_keeps_buyers_the_cluster_window_still_needs(state):
    """`seen` is pruned on state_retention_days, but the cluster window can be longer.

    With retention shorter than the window, buyer rows were deleted while the
    detector was still supposed to be counting them.
    """
    recent = NOW - timedelta(days=20)
    state.record_insider_buyer("TEST", "Jane Doe", recent)
    state.prune(NOW, retention_days=7, cluster_days=30)
    assert state.recent_insider_buyers("TEST", NOW, days=30) == 1


def test_pruning_still_drops_buyers_past_every_window(state):
    ancient = NOW - timedelta(days=120)
    state.record_insider_buyer("TEST", "Jane Doe", ancient)
    state.prune(NOW, retention_days=7, cluster_days=30)
    assert state.recent_insider_buyers("TEST", NOW, days=180) == 0


def test_pruning_still_clears_ordinary_dedup_keys(state):
    """The carve-out is only for buyer rows; normal dedup keys expire as before."""
    state.mark_seen("old-key", "dark_pool", "TEST", NOW - timedelta(days=40))
    state.record_insider_buyer("TEST", "Jane Doe", NOW - timedelta(days=40))
    state.prune(NOW, retention_days=7, cluster_days=90)
    assert state.is_new("old-key")
    assert state.recent_insider_buyers("TEST", NOW, days=90) == 1
