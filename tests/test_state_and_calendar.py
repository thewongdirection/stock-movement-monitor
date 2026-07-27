from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from monitor import market_calendar as cal
from monitor.state import State


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
NOW = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)


def test_dedup_recognises_a_repeat(state):
    assert state.is_new("abc") is True
    state.mark_seen("abc", "dark_pool", "TEST", NOW)
    assert state.is_new("abc") is False


def test_watermarks_are_staged_until_flushed(state):
    state.set_watermark("dark_pool", "TEST", NOW)
    assert state.watermark("dark_pool", "TEST") is None
    state.flush_progress()
    assert state.watermark("dark_pool", "TEST") == NOW


def test_flush_skips_the_pairs_that_failed_delivery(state):
    state.set_watermark("dark_pool", "TEST", NOW)
    state.set_watermark("options_flow", "TEST", NOW)
    state.flush_progress(skip={("dark_pool", "TEST")})
    assert state.watermark("dark_pool", "TEST") is None
    assert state.watermark("options_flow", "TEST") == NOW


def test_watermarks_never_move_backwards(state):
    state.set_watermark("dark_pool", "TEST", NOW)
    state.flush_progress()
    state.set_watermark("dark_pool", "TEST", NOW - timedelta(hours=1))
    state.flush_progress()
    assert state.watermark("dark_pool", "TEST") == NOW


def test_cold_start_returns_a_bounded_window(state):
    since, cold = state.since("dark_pool", "TEST", NOW, cold_start_minutes=30)
    assert cold is True
    assert since == NOW - timedelta(minutes=30)


def test_known_watermark_is_used_instead_of_the_cold_window(state):
    mark = NOW - timedelta(minutes=6)
    state.set_watermark("dark_pool", "TEST", mark)
    state.flush_progress()
    since, cold = state.since("dark_pool", "TEST", NOW, cold_start_minutes=30)
    assert cold is False
    assert since == mark


def test_a_very_stale_watermark_is_floored_at_a_week(state):
    """After a long outage, don't try to replay a month of prints."""
    state.set_watermark("dark_pool", "TEST", NOW - timedelta(days=90))
    state.flush_progress()
    since, _ = state.since("dark_pool", "TEST", NOW, cold_start_minutes=30)
    assert since == NOW - timedelta(days=7)


def test_cooldown_blocks_then_expires(state):
    state.start_cooldown("dark_pool", "TEST", NOW, minutes=15)
    state.flush_progress()
    assert state.in_cooldown("dark_pool", "TEST", NOW) is True
    assert state.in_cooldown("dark_pool", "TEST", NOW + timedelta(minutes=16)) is False


def test_zero_cooldown_is_a_no_op(state):
    state.start_cooldown("dark_pool", "TEST", NOW, minutes=0)
    state.flush_progress()
    assert state.in_cooldown("dark_pool", "TEST", NOW) is False


def test_insider_buyer_recording_counts_distinct_people(state):
    state.record_insider_buyer("TEST", "Jane Doe", NOW)
    state.record_insider_buyer("TEST", "jane doe", NOW)  # same person, different case
    state.record_insider_buyer("TEST", "Richard Roe", NOW)
    assert state.recent_insider_buyers("TEST", NOW, days=30) == 2


def test_insider_buyers_fall_out_of_the_window(state):
    state.record_insider_buyer("TEST", "Jane Doe", NOW - timedelta(days=60))
    assert state.recent_insider_buyers("TEST", NOW, days=30) == 0


def test_prune_clears_old_rows(state):
    state.mark_seen("old", "dark_pool", "TEST", NOW - timedelta(days=60))
    state.mark_seen("new", "dark_pool", "TEST", NOW)
    state.prune(NOW, retention_days=30)
    assert state.is_new("old") is True
    assert state.is_new("new") is False


def test_state_survives_reopening(tmp_path):
    path = tmp_path / "persist.db"
    with State(path) as first:
        first.mark_seen("abc", "dark_pool", "TEST", NOW)
        first.set_watermark("dark_pool", "TEST", NOW)
        first.flush_progress()
    with State(path) as second:
        assert second.is_new("abc") is False
        assert second.watermark("dark_pool", "TEST") == NOW


# --------------------------------------------------------------------------
# Market calendar
# --------------------------------------------------------------------------
def test_weekday_is_a_trading_day():
    assert cal.is_trading_day(date(2026, 7, 27)) is True  # Monday


@pytest.mark.parametrize(
    "day",
    [
        date(2026, 7, 25),  # Saturday
        date(2026, 7, 26),  # Sunday
        date(2026, 1, 1),   # New Year's Day
        date(2026, 4, 3),   # Good Friday
        date(2026, 7, 3),   # Independence Day observed (4th is a Saturday)
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
        date(2027, 6, 18),  # Juneteenth observed (19th is a Saturday)
        date(2027, 12, 24),  # Christmas observed (25th is a Saturday)
    ],
)
def test_closures(day):
    assert cal.is_trading_day(day) is False


def test_early_closes_are_recognised():
    assert cal.session_close(date(2026, 11, 27)) == cal.EARLY_CLOSE
    assert cal.session_close(date(2026, 7, 27)) == cal.REGULAR_CLOSE


def test_regular_session_boundaries():
    day = date(2026, 7, 27)
    assert cal.is_regular_session(_et(day, 9, 29)) is False
    assert cal.is_regular_session(_et(day, 9, 30)) is True
    assert cal.is_regular_session(_et(day, 15, 59)) is True
    assert cal.is_regular_session(_et(day, 16, 0)) is False


def test_half_day_closes_early():
    half = date(2026, 11, 27)
    assert cal.is_regular_session(_et(half, 12, 59)) is True
    assert cal.is_regular_session(_et(half, 13, 1)) is False


def test_should_run_respects_the_extended_hours_switch():
    premarket = _et(date(2026, 7, 27), 7, 0)
    assert cal.should_run(premarket, extended_hours=False)[0] is False
    assert cal.should_run(premarket, extended_hours=True)[0] is True


def test_should_run_explains_itself():
    ran, why = cal.should_run(_et(date(2026, 12, 25), 11, 0))
    assert ran is False
    assert "holiday" in why


def test_uncovered_year_fails_open_on_weekdays():
    """Past the holiday table, a weekday runs with a warning rather than stopping."""
    future_weekday = _et(date(2035, 3, 14), 11, 0)  # a Wednesday
    ran, why = cal.should_run(future_weekday)
    assert ran is True
    assert "holiday table" in why

    ran_weekend, why_weekend = cal.should_run(_et(date(2035, 3, 17), 11, 0))  # Sunday
    assert ran_weekend is False
    assert "weekend" in why_weekend


def test_minutes_into_session():
    assert cal.minutes_into_session(_et(date(2026, 7, 27), 9, 30)) == 0
    assert cal.minutes_into_session(_et(date(2026, 7, 27), 10, 30)) == 60
    assert cal.minutes_into_session(_et(date(2026, 7, 27), 20, 0)) is None


def test_minutes_until_close_uses_the_early_close():
    assert cal.minutes_until_close(_et(date(2026, 11, 27), 12, 0)) == 60
    assert cal.minutes_until_close(_et(date(2026, 7, 27), 15, 0)) == 60


def _et(day: date, hour: int, minute: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=cal.ET)
