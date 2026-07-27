"""A dependency-light US equity session calendar.

Deliberately hardcoded rather than pulling in pandas: the monitor runs on a
five-minute cron and a heavyweight calendar dependency is not worth the cold
start. Coverage is checked at runtime — past the last covered year the module
degrades to plain weekday logic and says so, rather than silently deciding the
market is shut.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
PREMARKET_OPEN = time(4, 0)
AFTERHOURS_CLOSE = time(20, 0)

# NYSE full closures.
HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2026
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
        # 2027
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
        date(2027, 11, 25), date(2027, 12, 24),
        # 2028
        date(2028, 1, 17), date(2028, 2, 21), date(2028, 4, 14), date(2028, 5, 29),
        date(2028, 6, 19), date(2028, 7, 4), date(2028, 9, 4), date(2028, 11, 23),
        date(2028, 12, 25),
    }
)

# Sessions that close at 13:00 ET.
EARLY_CLOSES: frozenset[date] = frozenset(
    {
        date(2026, 11, 27), date(2026, 12, 24),
        date(2027, 11, 26),
        date(2028, 7, 3), date(2028, 11, 24),
    }
)

COVERED_YEARS = range(2026, 2029)


def calendar_covers(d: date) -> bool:
    """False once we're past the hardcoded holiday table."""
    return d.year in COVERED_YEARS


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in HOLIDAYS


def session_close(d: date) -> time:
    return EARLY_CLOSE if d in EARLY_CLOSES else REGULAR_CLOSE


def now_et() -> datetime:
    return datetime.now(ET)


def is_regular_session(moment: datetime | None = None) -> bool:
    moment = (moment or now_et()).astimezone(ET)
    if not is_trading_day(moment.date()):
        return False
    return REGULAR_OPEN <= moment.time() < session_close(moment.date())


def is_extended_session(moment: datetime | None = None) -> bool:
    """Pre-market or after-hours on a trading day (excluding the regular session)."""
    moment = (moment or now_et()).astimezone(ET)
    if not is_trading_day(moment.date()):
        return False
    t = moment.time()
    if PREMARKET_OPEN <= t < REGULAR_OPEN:
        return True
    return session_close(moment.date()) <= t < AFTERHOURS_CLOSE


def should_run(moment: datetime | None = None, extended_hours: bool = False) -> tuple[bool, str]:
    """Whether price-driven detectors should run now, plus a reason for the log."""
    moment = (moment or now_et()).astimezone(ET)
    if not calendar_covers(moment.date()):
        # Fail open: better a redundant run than a monitor that quietly stops.
        if moment.weekday() >= 5:
            return False, f"weekend ({moment:%a}); holiday table ends {COVERED_YEARS[-1]}"
        return True, (
            f"holiday table only covers through {COVERED_YEARS[-1]} — running on "
            "weekday logic alone; update market_calendar.HOLIDAYS"
        )
    if is_regular_session(moment):
        return True, f"regular session ({moment:%H:%M %Z})"
    if extended_hours and is_extended_session(moment):
        return True, f"extended hours ({moment:%H:%M %Z})"
    if not is_trading_day(moment.date()):
        why = "holiday" if moment.date() in HOLIDAYS else "weekend"
        return False, f"{why} ({moment:%Y-%m-%d})"
    return False, f"outside session hours ({moment:%H:%M %Z})"


def minutes_into_session(moment: datetime) -> int | None:
    """Minutes since the open, or None outside the regular session."""
    moment = moment.astimezone(ET)
    if not is_trading_day(moment.date()):
        return None
    open_at = datetime.combine(moment.date(), REGULAR_OPEN, tzinfo=ET)
    close_at = datetime.combine(moment.date(), session_close(moment.date()), tzinfo=ET)
    if not (open_at <= moment < close_at):
        return None
    return int((moment - open_at).total_seconds() // 60)


def minutes_until_close(moment: datetime) -> int | None:
    moment = moment.astimezone(ET)
    if not is_trading_day(moment.date()):
        return None
    close_at = datetime.combine(moment.date(), session_close(moment.date()), tzinfo=ET)
    if moment >= close_at:
        return None
    return int((close_at - moment).total_seconds() // 60)
