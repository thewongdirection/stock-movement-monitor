"""The market calendar, and the time-of-day bucketing the baselines depend on.

Everything in this project is timestamped in US/Eastern. That is a deliberate
choice, not a convenience: the volume baseline compares a bar against the *same
clock slot* on previous sessions, and the market opens at 09:30 Eastern all year
round while its UTC offset moves twice a year. Bucketing in UTC would compare
the open against mid-morning for half the year and call the difference a signal.

The calendar is computed from rules rather than shipped as a hard-coded list of
dates, so it does not quietly expire at the end of whatever year the list was
written in. Exchange-declared closures that no rule predicts — a national day of
mourning, a hurricane — are the known gap; `AD_HOC_CLOSURES` is where those go.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
HALF_DAY_CLOSE = time(13, 0)
PREMARKET_OPEN = time(4, 0)
AFTERHOURS_CLOSE = time(20, 0)

#: Unscheduled full-day closures the rules below cannot derive. Add as they happen.
AD_HOC_CLOSURES: frozenset[date] = frozenset({
    date(2025, 1, 9),    # National day of mourning, President Carter
})


# --------------------------------------------------------------------------- #
# now / conversion
# --------------------------------------------------------------------------- #

def now_et() -> datetime:
    """Current wall-clock time in the market's timezone."""
    return datetime.now(ET)


def to_et(ts: datetime) -> datetime:
    """Normalise any timestamp to Eastern.

    A naive timestamp is *assumed* to already be Eastern rather than UTC. Every
    source in this project converts on parse, so a naive value reaching here is
    either a test fixture or hand-written config — both of which mean local
    market time.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=ET)
    return ts.astimezone(ET)


def to_utc(ts: datetime) -> datetime:
    return to_et(ts).astimezone(UTC)


# --------------------------------------------------------------------------- #
# holiday rules
# --------------------------------------------------------------------------- #

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Mon=0) of a month. n=1 is the first."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm. Needed only to find Good Friday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(holiday: date) -> date | None:
    """Shift a weekend holiday to the day the exchange actually closes.

    Saturday moves back to Friday, Sunday forward to Monday — except a Saturday
    New Year's Day, where the preceding Friday is the last trading day of the
    old year and the exchange stays open for it.
    """
    if holiday.weekday() == 5:
        if holiday.month == 1 and holiday.day == 1:
            return None
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def holidays(year: int) -> frozenset[date]:
    """Full-day NYSE/Nasdaq closures for a calendar year."""
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    fixed = [
        date(year, 1, 1),                       # New Year's Day
        date(year, 6, 19),                      # Juneteenth (from 2022)
        date(year, 7, 4),                       # Independence Day
        date(year, 12, 25),                     # Christmas
    ]
    if year < 2022:
        fixed.remove(date(year, 6, 19))

    days = {d for d in (_observed(f) for f in fixed) if d is not None}
    days |= {
        _nth_weekday(year, 1, 0, 3),            # MLK Day
        _nth_weekday(year, 2, 0, 3),            # Presidents' Day
        _easter(year) - timedelta(days=2),      # Good Friday
        _last_weekday(year, 5, 0),              # Memorial Day
        _nth_weekday(year, 9, 0, 1),            # Labor Day
        thanksgiving,
    }
    return frozenset(days)


def half_days(year: int) -> frozenset[date]:
    """Sessions that close at 13:00 ET.

    Only counted when the short session is a weekday that is not itself a
    holiday — a Christmas Eve that falls on a Sunday is not a half day, it is
    nothing at all.
    """
    full = holidays(year)
    candidates = {
        date(year, 7, 3),                                     # day before July 4
        _nth_weekday(year, 11, 3, 4) + timedelta(days=1),     # day after Thanksgiving
        date(year, 12, 24),                                   # Christmas Eve
    }
    return frozenset(
        d for d in candidates if d.weekday() < 5 and d not in full
    )


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Session:
    day: date
    open_at: datetime
    close_at: datetime
    half_day: bool

    def contains(self, ts: datetime) -> bool:
        return self.open_at <= to_et(ts) < self.close_at

    @property
    def length_minutes(self) -> int:
        return int((self.close_at - self.open_at).total_seconds() // 60)


def is_trading_day(day: date) -> bool:
    return (
        day.weekday() < 5
        and day not in holidays(day.year)
        and day not in AD_HOC_CLOSURES
    )


def session_for(day: date) -> Session | None:
    """The regular session on `day`, or None if the market was closed."""
    if not is_trading_day(day):
        return None
    short = day in half_days(day.year)
    return Session(
        day=day,
        open_at=datetime.combine(day, REGULAR_OPEN, ET),
        close_at=datetime.combine(day, HALF_DAY_CLOSE if short else REGULAR_CLOSE, ET),
        half_day=short,
    )


def previous_sessions(day: date, count: int) -> list[date]:
    """The `count` trading days strictly before `day`, most recent first.

    Used to build the same-slot baseline. Bounded at 10x the request so a
    pathological calendar can't spin.
    """
    out: list[date] = []
    cursor = day
    for _ in range(count * 10 + 10):
        if len(out) >= count:
            break
        cursor -= timedelta(days=1)
        if is_trading_day(cursor):
            out.append(cursor)
    return out


def last_session_on_or_before(day: date) -> date | None:
    for _ in range(15):
        if is_trading_day(day):
            return day
        day -= timedelta(days=1)
    return None


# --------------------------------------------------------------------------- #
# phase
# --------------------------------------------------------------------------- #

def market_phase(ts: datetime | None = None) -> str:
    """One of: closed | premarket | open | afterhours.

    The monitor still runs when the market is closed — Form 4s land until
    roughly 22:00 ET and the overnight open-interest file is the whole point of
    the daily pass. The phase decides which signals are *meaningful*, not
    whether to bother waking up.
    """
    ts = to_et(ts or now_et())
    session = session_for(ts.date())
    if session is None:
        return "closed"
    if session.contains(ts):
        return "open"
    clock = ts.time()
    if PREMARKET_OPEN <= clock < session.open_at.time():
        return "premarket"
    if session.close_at.time() <= clock < AFTERHOURS_CLOSE:
        return "afterhours"
    return "closed"


def minutes_into_session(ts: datetime | None = None) -> int | None:
    """How far into the regular session `ts` is, or None if outside it."""
    ts = to_et(ts or now_et())
    session = session_for(ts.date())
    if session is None or not session.contains(ts):
        return None
    return int((ts - session.open_at).total_seconds() // 60)


# --------------------------------------------------------------------------- #
# slots
# --------------------------------------------------------------------------- #

def slot_of(ts: datetime, minutes: int = 30) -> str:
    """The time-of-day bucket a timestamp belongs to, e.g. '14:30'.

    Buckets are anchored to midnight, not to the open, so a 30-minute grid gives
    09:30/10:00/... and a 60-minute grid gives 09:00/10:00/.... The anchor only
    has to be *stable* — the baseline compares like slot against like slot.
    """
    if minutes <= 0:
        raise ValueError("slot size must be positive")
    ts = to_et(ts)
    total = ts.hour * 60 + ts.minute
    bucket = (total // minutes) * minutes
    return f"{bucket // 60:02d}:{bucket % 60:02d}"


def session_progress(ts: datetime | None = None) -> float:
    """0.0 at the open, 1.0 at the close. Clamped outside the session.

    Volume is not uniform across a session — the first and last half hours carry
    a large share of the day. Anything that needs to scale by "how much of the
    day has happened" uses this rather than assuming a straight line.
    """
    ts = to_et(ts or now_et())
    session = session_for(ts.date())
    if session is None:
        return 1.0
    if ts <= session.open_at:
        return 0.0
    if ts >= session.close_at:
        return 1.0
    span = (session.close_at - session.open_at).total_seconds()
    return (ts - session.open_at).total_seconds() / span
