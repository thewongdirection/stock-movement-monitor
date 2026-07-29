"""Telling a quiet market apart from a broken feed."""

from __future__ import annotations

from datetime import datetime, timedelta

from monitor.clock import ET
from monitor.health import (Health, check_bar_freshness, check_feed_advanced,
                            check_session)
from monitor.models import SourceIssue

from conftest import make_bars

MIDDAY = datetime(2026, 7, 24, 12, 0, tzinfo=ET)


class TestHealthAccumulator:
    def test_a_clean_run_is_ok(self):
        assert Health().ok

    def test_issues_are_summarised_by_kind(self):
        health = Health()
        health.add(SourceIssue("fmp", "NVDA", "stale", "old"))
        health.add(SourceIssue("sec", "*", "unreachable", "down"))
        health.add(SourceIssue("fmp", "MSFT", "stale", "old"))
        assert not health.ok
        assert health.summary() == "2 stale, 1 unreachable"
        assert len(health.by_kind("stale")) == 2

    def test_notes_are_not_faults(self):
        health = Health()
        health.note("market closed")
        assert health.ok and health.notes


class TestBarFreshness:
    def test_recent_bars_are_fine(self):
        bars = make_bars(sessions=3, end=MIDDAY)
        assert check_bar_freshness("fmp", "NVDA", bars, MIDDAY, 120) is None

    def test_no_bars_at_all_is_an_empty_source(self):
        issue = check_bar_freshness("fmp", "NVDA", [], MIDDAY, 120)
        assert issue.kind == "empty"

    def test_an_old_bar_during_market_hours_is_stale(self):
        bars = make_bars(sessions=3, end=MIDDAY - timedelta(hours=4))
        late = datetime(2026, 7, 24, 15, 30, tzinfo=ET)
        issue = check_bar_freshness("fmp", "NVDA", bars, late, 60)
        assert issue and issue.kind == "stale"
        assert "while the market is open" in issue.detail

    def test_an_old_bar_outside_market_hours_is_not_a_fault(self):
        """Bars are supposed to be hours old at 8pm and days old on a Sunday."""
        bars = make_bars(sessions=3, end=MIDDAY)
        evening = datetime(2026, 7, 24, 20, 30, tzinfo=ET)
        assert check_bar_freshness("fmp", "NVDA", bars, evening, 60) is None
        sunday = datetime(2026, 7, 26, 12, 0, tzinfo=ET)
        assert check_bar_freshness("fmp", "NVDA", bars, sunday, 60) is None

    def test_the_first_minutes_of_a_session_are_not_stale(self):
        """At 09:31 the first 30-minute bar has not closed yet."""
        bars = make_bars(sessions=3, end=MIDDAY - timedelta(days=1))
        just_open = datetime(2026, 7, 24, 9, 35, tzinfo=ET)
        assert check_bar_freshness("fmp", "NVDA", bars, just_open, 120) is None


class TestFrozenFeed:
    def _seed(self, store, tickers, at):
        for ticker in tickers:
            store.note_feed(ticker, at)

    def test_a_watchlist_wide_standstill_is_reported(self, store):
        self._seed(store, ["NVDA", "MSFT"], datetime(2026, 7, 24, 9, 30, tzinfo=ET))
        late = datetime(2026, 7, 24, 15, 0, tzinfo=ET)
        issue = check_feed_advanced(store, "fmp", ["NVDA", "MSFT"], late, 180)
        assert issue and issue.kind == "stale"
        assert "frozen" in issue.detail

    def test_a_feed_that_is_advancing_is_fine(self, store):
        self._seed(store, ["NVDA", "MSFT"], datetime(2026, 7, 24, 14, 30, tzinfo=ET))
        late = datetime(2026, 7, 24, 15, 0, tzinfo=ET)
        assert check_feed_advanced(store, "fmp", ["NVDA", "MSFT"], late, 180) is None

    def test_a_first_run_is_not_a_frozen_feed(self, store):
        late = datetime(2026, 7, 24, 15, 0, tzinfo=ET)
        assert check_feed_advanced(store, "fmp", ["NVDA", "MSFT"], late, 180) is None

    def test_partial_history_does_not_trigger_it(self, store):
        self._seed(store, ["NVDA"], datetime(2026, 7, 24, 9, 30, tzinfo=ET))
        late = datetime(2026, 7, 24, 15, 0, tzinfo=ET)
        assert check_feed_advanced(store, "fmp", ["NVDA", "MSFT"], late, 180) is None

    def test_it_never_fires_outside_market_hours(self, store):
        self._seed(store, ["NVDA", "MSFT"], datetime(2026, 7, 20, 9, 30, tzinfo=ET))
        sunday = datetime(2026, 7, 26, 12, 0, tzinfo=ET)
        assert check_feed_advanced(store, "fmp", ["NVDA", "MSFT"], sunday, 180) is None

    def test_it_does_not_fire_early_in_the_session(self, store):
        self._seed(store, ["NVDA", "MSFT"], datetime(2026, 7, 23, 15, 30, tzinfo=ET))
        just_open = datetime(2026, 7, 24, 10, 0, tzinfo=ET)
        assert check_feed_advanced(store, "fmp", ["NVDA", "MSFT"], just_open, 180) is None


class TestSessionReason:
    def test_an_open_market_has_no_reason(self):
        assert check_session(MIDDAY) is None

    def test_a_weekend_is_named(self):
        reason = check_session(datetime(2026, 7, 25, 12, 0, tzinfo=ET))
        assert "not a trading day" in reason

    def test_a_holiday_is_named(self):
        assert "not a trading day" in check_session(datetime(2026, 12, 25, 12, 0, tzinfo=ET))

    def test_premarket_states_the_session_hours(self):
        reason = check_session(datetime(2026, 7, 24, 8, 0, tzinfo=ET))
        assert "premarket" in reason and "09:30" in reason

    def test_a_half_day_early_close_is_explained(self):
        reason = check_session(datetime(2026, 12, 24, 14, 0, tzinfo=ET))
        assert "early close" in reason and "13:00" in reason
