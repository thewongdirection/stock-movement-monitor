"""Data source health — unresponsive, stale, or corrupt.

A monitor that silently stops seeing data is worse than no monitor, because
silence reads as "nothing is happening". Three distinct failure modes, each
needing a different check:

**Unresponsive** — the call raises or times out. Easy to spot, but one blip is
not an outage, so failures are counted and only escalate after
``unresponsive_after`` consecutive misses. Recovery is announced too, so you
know when to trust the feed again.

**Stale** — the call *succeeds* but returns old data. This is the dangerous
one: a frozen feed looks perfectly healthy from the outside. Bar timestamps are
compared against the session clock, so a provider still serving yesterday's
close at 11am gets caught.

**Corrupt** — the data is present and current but wrong: negative volume, zero
or negative prices, high below low, timestamps out of order, or a price jump so
large it implies an unadjusted split rather than a real move. These are checked
before the data reaches a detector, because a corrupt bar produces a confident,
completely false alert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from . import market_calendar as cal
from .models import Bar
from .state import State

log = logging.getLogger(__name__)


class HealthState(str, Enum):
    OK = "ok"
    UNRESPONSIVE = "unresponsive"
    STALE = "stale"
    CORRUPT = "corrupt"
    EMPTY = "empty"


@dataclass
class Finding:
    source: str
    ticker: str
    state: HealthState
    detail: str
    #: True when the problem has persisted long enough to be worth a message.
    escalated: bool = False


@dataclass
class HealthReport:
    findings: list[Finding] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)

    @property
    def escalated(self) -> list[Finding]:
        return [f for f in self.findings if f.escalated]

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)


# --------------------------------------------------------------------------
# Corruption checks on bar series
# --------------------------------------------------------------------------
#: A single-bar move beyond this is far more likely an unadjusted corporate
#: action than a real intraday print. Rare genuine moves this big exist, so the
#: bar is quarantined and reported rather than silently dropped.
IMPLAUSIBLE_MOVE_PCT = 50.0


def inspect_bars(
    ticker: str,
    bars: list[Bar],
    *,
    source: str = "bars",
    now: datetime,
    interval_minutes: int,
    max_stale_intervals: int = 4,
) -> tuple[list[Bar], list[Finding]]:
    """Validate a bar series, returning the usable bars plus any findings.

    Bad bars are dropped rather than allowed through, because a detector cannot
    tell a corrupt bar from a real one and will happily alert on it.
    """
    findings: list[Finding] = []
    if not bars:
        return [], [
            Finding(source, ticker, HealthState.EMPTY, "provider returned no bars")
        ]

    clean: list[Bar] = []
    problems: dict[str, int] = {}

    def flag(reason: str) -> None:
        problems[reason] = problems.get(reason, 0) + 1

    previous: Bar | None = None
    for bar in bars:
        if bar.volume < 0:
            flag("negative volume")
            continue
        if bar.open <= 0 or bar.high <= 0 or bar.low <= 0 or bar.close <= 0:
            flag("non-positive price")
            continue
        if bar.high < bar.low:
            flag("high below low")
            continue
        if not (bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high):
            flag("open/close outside high-low range")
            continue
        if previous is not None:
            if bar.ts < previous.ts:
                flag("timestamps out of order")
                continue
            if bar.ts == previous.ts:
                flag("duplicate timestamp")
                continue
            move = abs(bar.close - previous.close) / previous.close * 100
            # Only suspicious inside one session; an overnight gap is normal.
            same_session = bar.ts.date() == previous.ts.date()
            if same_session and move > IMPLAUSIBLE_MOVE_PCT:
                flag(f"implausible {move:.0f}% single-bar move")
                continue
        clean.append(bar)
        previous = bar

    if problems:
        detail = ", ".join(f"{count}x {reason}" for reason, count in sorted(problems.items()))
        findings.append(
            Finding(
                source,
                ticker,
                HealthState.CORRUPT,
                f"dropped {sum(problems.values())} of {len(bars)} bars — {detail}",
            )
        )

    if not clean:
        findings.append(
            Finding(source, ticker, HealthState.EMPTY, "no usable bars survived validation")
        )
        return [], findings

    stale = _staleness(clean[-1].ts, now, interval_minutes, max_stale_intervals)
    if stale:
        findings.append(Finding(source, ticker, HealthState.STALE, stale))

    return clean, findings


def _staleness(
    newest: datetime, now: datetime, interval_minutes: int, max_intervals: int
) -> str | None:
    """Describe how far behind the newest bar is, or None if it's current.

    Only meaningful during a session — outside one, the newest bar is *supposed*
    to be the last close.
    """
    now_et = now.astimezone(cal.ET)
    if not cal.is_regular_session(now_et):
        return None
    into = cal.minutes_into_session(now_et) or 0
    # Right after the open there is legitimately little data yet.
    if into < max(interval_minutes * 2, 10):
        return None

    lag = (now_et - newest.astimezone(cal.ET)).total_seconds() / 60.0
    budget = interval_minutes * max_intervals
    if lag <= budget:
        return None
    return (
        f"newest bar is {lag:.0f} min old ({newest.astimezone(cal.ET):%Y-%m-%d %H:%M %Z}); "
        f"expected within {budget:.0f} min of now — the feed looks frozen"
    )


def inspect_events(
    ticker: str,
    source: str,
    timestamps: list[datetime],
    *,
    now: datetime,
    max_future_minutes: int = 5,
) -> list[Finding]:
    """Sanity-check event timestamps from a print or flow feed.

    Timestamps meaningfully in the future mean a clock or timezone bug
    somewhere, which would let an event evade every watermark comparison and
    re-alert forever.
    """
    findings: list[Finding] = []
    if not timestamps:
        return findings
    horizon = now + timedelta(minutes=max_future_minutes)
    ahead = [t for t in timestamps if t > horizon]
    if ahead:
        worst = max(ahead)
        findings.append(
            Finding(
                source,
                ticker,
                HealthState.CORRUPT,
                f"{len(ahead)} event(s) timestamped in the future (worst: "
                f"{worst.isoformat()}) — likely a timezone or clock bug upstream",
            )
        )
    return findings


# --------------------------------------------------------------------------
# Consecutive-failure tracking
# --------------------------------------------------------------------------
class HealthTracker:
    """Turns individual call outcomes into escalations and recoveries.

    Backed by the state database so the count survives between cron runs — a
    single process only ever sees one attempt.
    """

    def __init__(self, state: State, unresponsive_after: int = 3):
        self.state = state
        self.unresponsive_after = max(1, unresponsive_after)
        self.report = HealthReport()

    def _key(self, source: str, ticker: str) -> str:
        return f"health:{source}:{ticker}"

    def record_failure(self, source: str, ticker: str, detail: str) -> None:
        key = self._key(source, ticker)
        count = self.state.bump_counter(key)
        escalated = count == self.unresponsive_after
        if count > self.unresponsive_after:
            # Already reported; stay quiet until it recovers.
            log.debug("%s still failing (%d consecutive)", key, count)
            return
        self.report.add(
            Finding(
                source,
                ticker,
                HealthState.UNRESPONSIVE,
                f"{detail} ({count} consecutive failure"
                f"{'s' if count != 1 else ''})",
                escalated=escalated,
            )
        )

    def record_success(self, source: str, ticker: str) -> None:
        key = self._key(source, ticker)
        previous = self.state.reset_counter(key)
        if previous >= self.unresponsive_after:
            self.report.recovered.append(
                f"{source} for {ticker} is responding again after "
                f"{previous} consecutive failures"
            )

    def record_findings(self, findings: list[Finding]) -> None:
        """Log data-quality findings, escalating stale/corrupt on persistence."""
        for finding in findings:
            key = f"{self._key(finding.source, finding.ticker)}:{finding.state.value}"
            count = self.state.bump_counter(key)
            if count == 1 or count == self.unresponsive_after:
                self.report.add(
                    Finding(
                        finding.source,
                        finding.ticker,
                        finding.state,
                        finding.detail,
                        # Corruption is escalated immediately — it produces
                        # false alerts, so waiting to report it is wrong.
                        escalated=(
                            finding.state is HealthState.CORRUPT
                            or count >= self.unresponsive_after
                        ),
                    )
                )

    def clear_findings(self, source: str, ticker: str) -> None:
        for state in (HealthState.STALE, HealthState.CORRUPT, HealthState.EMPTY):
            self.state.reset_counter(f"{self._key(source, ticker)}:{state.value}")

    def summary_message(self) -> str:
        """An HTML block for the run footer, or empty when everything is fine."""
        import html

        blocks: list[str] = []
        escalated = self.report.escalated
        if escalated:
            lines = []
            for f in escalated:
                icon = {
                    HealthState.UNRESPONSIVE: "🔌",
                    HealthState.STALE: "🧊",
                    HealthState.CORRUPT: "☣️",
                    HealthState.EMPTY: "␀",
                }.get(f.state, "•")
                lines.append(
                    f"{icon} <b>{html.escape(f.source)}</b> / "
                    f"{html.escape(f.ticker)} — {html.escape(f.state.value)}: "
                    f"{html.escape(f.detail)}"
                )
            blocks.append("🩺 <b>Data source problems</b>\n" + "\n".join(lines[:12]))
        if self.report.recovered:
            blocks.append(
                "✅ <b>Recovered</b>\n"
                + "\n".join(f"• {html.escape(r)}" for r in self.report.recovered[:12])
            )
        return "\n\n".join(blocks)
