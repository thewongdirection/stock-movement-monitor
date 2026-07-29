"""Knowing the difference between a quiet market and a broken feed.

This is the failure this project most needs to avoid. A monitor that goes silent
because nothing is happening and a monitor that goes silent because its data
source died look identical from the outside — and the second one is worst
exactly when it matters, since feeds tend to break under the load of a volatile
day.

So silence is never accepted on its own. Every run asserts something positive:
data arrived, it was recent enough to act on, and it advanced since last time.
Anything that fails those assertions becomes a visible `SourceIssue` delivered
alongside the alerts, not a line in a log file nobody reads.

Every check here is market-aware. Bars are *supposed* to be four hours old at
8pm and three days old on a Sunday; flagging that would train you to ignore the
warnings, which is the same as not having them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .clock import market_phase, minutes_into_session, session_for, to_et
from .models import Bar, SourceIssue, ago
from .store import Store


@dataclass
class Health:
    """Accumulates everything wrong with this run's inputs."""

    issues: list[SourceIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, issue: SourceIssue) -> None:
        self.issues.append(issue)

    def note(self, text: str) -> None:
        """Something worth saying that is not a fault — a bootstrap, a skip."""
        self.notes.append(text)

    @property
    def ok(self) -> bool:
        return not self.issues

    def by_kind(self, kind: str) -> list[SourceIssue]:
        return [issue for issue in self.issues if issue.kind == kind]

    def summary(self) -> str:
        if self.ok:
            return "all sources healthy"
        counts: dict[str, int] = {}
        for issue in self.issues:
            counts[issue.kind] = counts.get(issue.kind, 0) + 1
        return ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))


def check_bar_freshness(source: str, ticker: str, bars: list[Bar], now: datetime,
                        max_age_minutes: int) -> SourceIssue | None:
    """Is the newest bar recent enough to act on?

    Only asked while the market is open and far enough into the session that a
    bar should exist. At 09:31 the first 30-minute bar has not closed yet, and
    complaining about its absence would fire on every single trading day.
    """
    now = to_et(now)
    if not bars:
        return SourceIssue(source, ticker, "empty", "no bars returned at all")

    newest = max(bar.ts for bar in bars)
    age = now - newest
    if market_phase(now) != "open":
        return None
    elapsed = minutes_into_session(now) or 0
    if elapsed < max_age_minutes:
        return None
    if age > timedelta(minutes=max_age_minutes):
        return SourceIssue(
            source, ticker, "stale",
            f"newest bar is {newest:%Y-%m-%d %H:%M} ET, {ago(age)} old "
            f"(limit {max_age_minutes} min) while the market is open",
        )
    return None


def check_feed_advanced(store: Store, source: str, tickers: list[str],
                        now: datetime, max_silence_minutes: int) -> SourceIssue | None:
    """Did *anything* on the watchlist move since the last run?

    A single stale ticker is ordinary — thin names print nothing for an hour.
    The whole watchlist standing still during market hours is not a market
    condition, it is a frozen feed, and it is the one failure a per-ticker check
    cannot see.
    """
    now = to_et(now)
    if market_phase(now) != "open":
        return None
    if (minutes_into_session(now) or 0) < max_silence_minutes:
        return None

    state = store.feed_state()
    watched = [t.upper() for t in tickers]
    known = [state[t] for t in watched if t in state]
    if len(known) < 2 or len(known) < len(watched):
        # Not enough history to distinguish a frozen feed from a first run.
        return None

    newest = max(datetime.fromisoformat(last_bar) for last_bar, _ in known)
    silence = now - to_et(newest)
    if silence > timedelta(minutes=max_silence_minutes):
        return SourceIssue(
            source, "*", "stale",
            f"no bar advanced on any of {len(watched)} tickers for {ago(silence)} "
            f"during market hours — the feed looks frozen, not the market",
        )
    return None


def check_session(now: datetime) -> str | None:
    """A human-readable reason the market is not open, or None if it is."""
    now = to_et(now)
    phase = market_phase(now)
    if phase == "open":
        return None
    session = session_for(now.date())
    if session is None:
        return f"market closed — {now:%A %d %b} is not a trading day"
    if session.half_day and phase == "afterhours":
        return f"market closed — early close at {session.close_at:%H:%M} ET today"
    return f"market {phase} — regular session runs {session.open_at:%H:%M}–{session.close_at:%H:%M} ET"
