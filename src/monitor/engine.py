"""One pass over the watchlist: fetch, evaluate, dedup, hand back.

The engine does not send anything. It returns alerts and the CLI delivers them,
so that `store.mark_sent` happens *after* a successful send — a delivery failure
leaves the alert unmarked and it goes out on the next run. Marking first would
be simpler and would silently drop alerts every time Telegram had a bad minute.

Two fetch cadences live here. Bars and Form 4s are pulled on every run, because
both change intraday. Option chains are pulled once per calendar day, because
open interest is settled overnight by the OCC and re-pulling a few hundred
contracts hourly would spend the entire rate budget re-reading a number that
cannot have moved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .canslim import CanSlim
from .clock import now_et, to_et
from .config import Config
from .health import Health, check_bar_freshness, check_feed_advanced, check_session
from .models import Alert, Severity
from .signals import Context, registry
from .sources import SourceSet
from .sources.base import SourceError
from .store import Store

log = logging.getLogger("monitor.engine")


@dataclass
class RunResult:
    alerts: list[Alert] = field(default_factory=list)
    health: Health = field(default_factory=Health)
    scanned: int = 0
    generated: int = 0
    duplicates: int = 0
    capped: int = 0
    canslim: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.health.ok


class Engine:
    def __init__(self, config: Config, store: Store, sources: SourceSet,
                 canslim: CanSlim | None = None, now: datetime | None = None,
                 dry_run: bool = False):
        self.config = config
        self.store = store
        self.sources = sources
        self.canslim = canslim
        self.now = to_et(now) if now else now_et()
        #: A preview must not consume anything a real run needs. See _load_chain.
        self.dry_run = dry_run

    # -- the pass -----------------------------------------------------------
    def run(self, tickers: list[str] | None = None) -> RunResult:
        result = RunResult()
        result.health.issues.extend(self.sources.issues)

        watchlist = [t.upper() for t in (tickers or self.config.watchlist)]
        closed = check_session(self.now)
        if closed:
            result.health.note(closed)

        signals = registry(self.config)
        if not signals:
            result.health.note("every signal is disabled — nothing was evaluated")
            return result

        raw: list[Alert] = []
        for ticker in watchlist:
            result.scanned += 1
            ctx = self._context(ticker, result.health)
            for signal in signals:
                try:
                    raw.extend(signal.evaluate(ctx))
                except Exception:                   # noqa: BLE001
                    # One broken signal must not cost the other three, or the
                    # rest of the watchlist.
                    log.exception("signal %s failed on %s", signal.name, ticker)
                    result.health.note(
                        f"signal '{signal.name}' raised on {ticker} — see the log"
                    )

        frozen = check_feed_advanced(
            self.store, self.config.get("sources.bars"), watchlist, self.now,
            self.config.get("health.max_feed_silence_minutes"),
        )
        if frozen:
            result.health.add(frozen)

        result.generated = len(raw)
        result.alerts = self._filter(raw, result)
        self._attach_canslim(result)
        return result

    # -- per-ticker fetch ---------------------------------------------------
    def _context(self, ticker: str, health: Health) -> Context:
        ctx = Context(ticker=ticker, now=self.now, config=self.config, store=self.store)

        if self.sources.bars is not None:
            try:
                ctx.bars = self.sources.bars.bars(
                    ticker,
                    self.config.get("poll.bar_minutes"),
                    self.config.get("poll.baseline_sessions"),
                )
            except SourceError as exc:
                health.add(exc.issue())
            else:
                stale = check_bar_freshness(
                    self.sources.bars.name, ticker, ctx.bars, self.now,
                    self.config.get("poll.max_bar_age_minutes"),
                )
                if stale:
                    health.add(stale)
                if ctx.bars:
                    self.store.note_feed(ticker, max(bar.ts for bar in ctx.bars))

        if self.sources.options is not None and self.config.get("signals.open_interest.enabled"):
            self._load_chain(ctx, health)

        if self.sources.insider is not None and self.config.get("signals.insider.enabled"):
            since = self.now.date() - timedelta(
                days=self.config.get("signals.insider.lookback_days")
            )
            try:
                ctx.filings = self.sources.insider.filings(ticker, since)
            except SourceError as exc:
                health.add(exc.issue())

        if self.sources.trades is not None and self.config.get("signals.blocks.enabled"):
            since = self.now - timedelta(
                minutes=self.config.get("poll.max_bar_age_minutes")
            )
            try:
                ctx.trades = self.sources.trades.trades(ticker, since)
            except SourceError as exc:
                health.add(exc.issue())

        return ctx

    def _load_chain(self, ctx: Context, health: Health) -> None:
        """Fetch the chain at most once a day, and pair it with yesterday's."""
        marker = f"oi_fetched:{ctx.ticker}"
        today = self.now.date().isoformat()
        if self.store.watermark(marker) == today:
            log.debug("%s: option chain already pulled today", ctx.ticker)
            return

        try:
            chain = self.sources.options.chain(
                ctx.ticker, self.config.get("signals.open_interest.max_days_to_expiry")
            )
        except SourceError as exc:
            health.add(exc.issue())
            return

        # A replay capture already carries its own prior snapshot; a live source
        # does not, and gets it from the store.
        if not chain.previous:
            previous, from_date = self.store.previous_oi(ctx.ticker, chain.as_of)
            chain.previous = previous
            if from_date and _gap_days(from_date, chain.as_of) > 4:
                health.note(
                    f"{ctx.ticker}: open interest compared against {from_date}, "
                    f"not the previous session — treat the change as cumulative"
                )

        if not chain.has_baseline:
            health.note(
                f"{ctx.ticker}: first option snapshot stored ({len(chain.contracts)} "
                f"contracts). Open-interest changes become available on the next daily pass."
            )

        ctx.chain = chain

        # The snapshot is real observed data and re-saving it is an upsert, so it
        # is stored either way — tomorrow's diff needs it.
        self.store.save_oi_snapshot(ctx.ticker, chain.as_of, chain.contracts)

        # The daily marker is different: it is a budget, and a preview must not
        # spend it. Setting it here on a --dry-run made the real scheduled run
        # later the same day skip the chain entirely and never alert on open
        # interest — losing the headline signal for the whole day, on precisely
        # the path the setup guide walks you through first.
        if not self.dry_run:
            self.store.set_watermark(marker, today)

    # -- filtering ----------------------------------------------------------
    def _filter(self, alerts: list[Alert], result: RunResult) -> list[Alert]:
        """Drop what was already sent, then cap, keeping the most severe.

        The cap sorts by severity and recency before truncating, so a chaotic
        hour loses the least interesting alerts rather than an arbitrary tail.
        `capped` is reported, because a silent truncation reads as "that was
        everything".
        """
        minimum = self.config.get("notify.min_severity")
        floor = Severity(minimum).rank

        fresh: list[Alert] = []
        for alert in alerts:
            if alert.severity.rank < floor:
                continue
            if self.store.already_sent(alert.dedup_key):
                result.duplicates += 1
                continue
            fresh.append(alert)

        fresh.sort(key=lambda a: (-a.severity.rank, -a.occurred_at.timestamp()))
        cap = self.config.get("notify.max_per_run")
        if len(fresh) > cap:
            result.capped = len(fresh) - cap
            fresh = fresh[:cap]
        return fresh

    def _attach_canslim(self, result: RunResult) -> None:
        if self.canslim is None or not self.canslim.enabled:
            return
        for alert in result.alerts:
            if alert.ticker in result.canslim:
                continue
            if not self.canslim.should_attach(alert.severity.rank):
                continue
            try:
                line = self.canslim.line(alert.ticker)
            except Exception:                       # noqa: BLE001
                log.exception("CAN SLIM grading failed for %s", alert.ticker)
                continue
            if line:
                result.canslim[alert.ticker] = line


def _gap_days(earlier: str, later: str) -> int:
    try:
        return (date.fromisoformat(later) - date.fromisoformat(earlier)).days
    except ValueError:
        return 0
