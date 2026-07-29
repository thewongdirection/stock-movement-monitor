"""The market calendar, and the timezone assumption everything else rests on."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from monitor import clock
from monitor.clock import ET


class TestHolidays:
    def test_the_fixed_holidays_are_all_present(self):
        found = clock.holidays(2026)
        assert date(2026, 1, 1) in found            # New Year's Day
        assert date(2026, 12, 25) in found          # Christmas
        assert date(2026, 6, 19) in found           # Juneteenth

    def test_the_floating_holidays_land_on_the_right_mondays(self):
        found = clock.holidays(2026)
        assert date(2026, 1, 19) in found           # 3rd Monday, MLK
        assert date(2026, 2, 16) in found           # 3rd Monday, Presidents'
        assert date(2026, 5, 25) in found           # last Monday, Memorial
        assert date(2026, 9, 7) in found            # 1st Monday, Labor

    def test_good_friday_is_derived_from_easter(self):
        # Easter 2026 is 5 April, so Good Friday is the 3rd.
        assert clock._easter(2026) == date(2026, 4, 5)
        assert date(2026, 4, 3) in clock.holidays(2026)

    def test_juneteenth_did_not_exist_before_2022(self):
        assert date(2021, 6, 19) not in clock.holidays(2021)
        assert date(2022, 6, 20) in clock.holidays(2022)   # observed, 19th is a Sunday

    def test_a_saturday_holiday_is_observed_on_the_friday(self):
        # 4 July 2026 is a Saturday.
        assert date(2026, 7, 3) in clock.holidays(2026)
        assert not clock.is_trading_day(date(2026, 7, 3))

    def test_a_sunday_holiday_is_observed_on_the_monday(self):
        # 4 July 2027 is a Sunday.
        assert date(2027, 7, 5) in clock.holidays(2027)

    def test_a_saturday_new_year_does_not_close_the_previous_friday(self):
        """The one exception: 31 December stays open as the year's last session."""
        # 1 January 2028 is a Saturday.
        assert date(2027, 12, 31) not in clock.holidays(2027)
        assert clock.is_trading_day(date(2027, 12, 31))

    def test_an_ad_hoc_closure_is_honoured(self):
        assert not clock.is_trading_day(date(2025, 1, 9))


class TestHalfDays:
    def test_black_friday_and_christmas_eve_are_short(self):
        found = clock.half_days(2026)
        assert date(2026, 11, 27) in found          # day after Thanksgiving
        assert date(2026, 12, 24) in found

    def test_a_half_day_that_is_actually_a_holiday_is_not_counted(self):
        # 3 July 2026 is the observed Independence Day, a full closure.
        assert date(2026, 7, 3) not in clock.half_days(2026)

    def test_a_half_day_session_closes_at_one(self):
        session = clock.session_for(date(2026, 12, 24))
        assert session.half_day
        assert session.close_at.hour == 13
        assert session.length_minutes == 210


class TestSessions:
    def test_a_weekend_has_no_session(self):
        assert clock.session_for(date(2026, 7, 25)) is None      # Saturday
        assert clock.session_for(date(2026, 7, 26)) is None      # Sunday

    def test_previous_sessions_skip_the_weekend(self):
        got = clock.previous_sessions(date(2026, 7, 27), 3)      # a Monday
        assert got == [date(2026, 7, 24), date(2026, 7, 23), date(2026, 7, 22)]

    def test_previous_sessions_skip_a_holiday(self):
        got = clock.previous_sessions(date(2026, 7, 6), 2)
        assert date(2026, 7, 3) not in got                       # observed July 4th

    def test_last_session_on_or_before_walks_back(self):
        assert clock.last_session_on_or_before(date(2026, 7, 26)) == date(2026, 7, 24)


class TestPhase:
    @pytest.mark.parametrize("hour,minute,expected", [
        (5, 0, "premarket"), (9, 29, "premarket"), (9, 30, "open"),
        (12, 0, "open"), (15, 59, "open"), (16, 0, "afterhours"),
        (19, 59, "afterhours"), (21, 0, "closed"), (3, 0, "closed"),
    ])
    def test_the_phases_line_up_with_the_session(self, hour, minute, expected):
        assert clock.market_phase(datetime(2026, 7, 24, hour, minute, tzinfo=ET)) == expected

    def test_a_holiday_is_closed_all_day(self):
        assert clock.market_phase(datetime(2026, 12, 25, 12, 0, tzinfo=ET)) == "closed"

    def test_minutes_into_session_is_none_outside_it(self):
        assert clock.minutes_into_session(datetime(2026, 7, 24, 8, 0, tzinfo=ET)) is None
        assert clock.minutes_into_session(datetime(2026, 7, 24, 10, 30, tzinfo=ET)) == 60

    def test_progress_is_clamped_at_the_edges(self):
        assert clock.session_progress(datetime(2026, 7, 24, 8, 0, tzinfo=ET)) == 0.0
        assert clock.session_progress(datetime(2026, 7, 24, 18, 0, tzinfo=ET)) == 1.0
        assert clock.session_progress(datetime(2026, 7, 24, 12, 45, tzinfo=ET)) == pytest.approx(0.5)


class TestSlots:
    def test_slots_are_anchored_to_midnight(self):
        assert clock.slot_of(datetime(2026, 7, 24, 14, 47, tzinfo=ET), 30) == "14:30"
        assert clock.slot_of(datetime(2026, 7, 24, 9, 31, tzinfo=ET), 30) == "09:30"
        assert clock.slot_of(datetime(2026, 7, 24, 9, 59, tzinfo=ET), 60) == "09:00"

    def test_a_nonsense_slot_size_is_refused(self):
        with pytest.raises(ValueError):
            clock.slot_of(datetime(2026, 7, 24, 12, 0, tzinfo=ET), 0)

    def test_the_open_lands_in_the_same_slot_across_a_dst_change(self):
        """The reason every timestamp in this project is Eastern.

        In UTC the opening bar is 13:30 in summer and 14:30 in winter, so a
        UTC-bucketed baseline would compare the January open against the July
        mid-morning and call the difference a signal.
        """
        summer = datetime(2026, 7, 24, 9, 30, tzinfo=ET)
        winter = datetime(2026, 1, 23, 9, 30, tzinfo=ET)
        assert clock.slot_of(summer, 30) == clock.slot_of(winter, 30) == "09:30"

        summer_utc = summer.astimezone(timezone.utc)
        winter_utc = winter.astimezone(timezone.utc)
        assert summer_utc.hour != winter_utc.hour     # the trap this avoids


class TestConversion:
    def test_a_naive_timestamp_is_read_as_eastern(self):
        got = clock.to_et(datetime(2026, 7, 24, 12, 0))
        assert got.hour == 12 and got.tzinfo is ET

    def test_a_utc_timestamp_is_converted_not_relabelled(self):
        got = clock.to_et(datetime(2026, 7, 24, 16, 0, tzinfo=timezone.utc))
        assert got.hour == 12

    def test_round_tripping_through_utc_is_lossless(self):
        original = datetime(2026, 7, 24, 12, 0, tzinfo=ET)
        assert clock.to_et(clock.to_utc(original)) == original
