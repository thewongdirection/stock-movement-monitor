"""Run orchestration: fetch once per ticker, run every detector, deliver.

Design notes worth knowing when changing this:

* **Data is fetched per ticker, not per detector.** Bars feed both the L1
  detector and the average-daily-volume figure the L2 detectors threshold on,
  so fetching once keeps the request count at roughly one per provider per
  ticker per run.
* **Market-hours gating is per detector.** Price and flow detectors only make
  sense while the market is trading, but Form 4 filings arrive on EDGAR until
  roughly 22:00 ET, so the insider detector runs whenever the cron fires.
* **Every fetch passes through the health tracker.** Unresponsive, stale and
  corrupt are three different failures with three different checks — see
  `health.py`. Corrupt bars are dropped before a detector can alert on them.
* **One ticker's failure never ends the run.** Provider errors are collected
  and reported; the remaining tickers still get processed.
* **Progress is only recorded for alerts that were actually delivered** — see
  `State.flush_progress`.
"""

from __future__ import annotations

import html
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import market_calendar as cal
from .config import Config
from .detectors import (
    BlockTradeDetector,
    Context,
    DarkPoolDetector,
    Detector,
    InsiderTradeDetector,
    OptionsFlowDetector,
    OptionVolumeDetector,
    VolumeAnomalyDetector,
)
from .detectors.volume_anomaly import INTERVAL_MINUTES
from .health import HealthTracker, inspect_bars, inspect_events
from .models import Alert, OptionVolumeSnapshot, Severity
from .notify.base import Notifier
from .providers.base import ProviderError, SetupError
from .providers.fmp import FMPProvider
from .providers.ibkr import IBKRProvider
from .providers.sec_edgar import SECEdgarProvider, default_since
from .providers.snapshot import SnapshotBars
from .providers.unusual_whales import UnusualWhalesProvider
from .state import State

log = logging.getLogger(__name__)

#: Detectors that only make sense while the market is trading.
SESSION_BOUND = frozenset(
    {"volume_anomaly", "block_trades", "dark_pool", "options_flow", "option_volume"}
)

DETECTOR_CLASSES: dict[str, type[Detector]] = {
    d.name: d
    for d in (
        VolumeAnomalyDetector,
        BlockTradeDetector,
        DarkPoolDetector,
        OptionsFlowDetector,
        OptionVolumeDetector,
        InsiderTradeDetector,
    )
}


@dataclass
class RunResult:
    started_at: datetime
    delivered: list[Alert] = field(default_factory=list)
    duplicates: int = 0
    below_threshold: int = 0
    capped: int = 0
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    session_note: str = ""
    ran_session_detectors: bool = True
    health_summary: str = ""
    graded: list[str] = field(default_factory=list)
    #: Set when bars came from a file rather than a live feed. Never empty on a
    #: replay — a run that quietly looks live is the whole risk of the feature.
    replay_note: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, message: str) -> None:
        """Record an error once. Setup failures would otherwise repeat per ticker."""
        if message not in self.errors:
            self.errors.append(message)

    def note(self, message: str) -> None:
        """Record a note once — the same condition often recurs per alert."""
        if message not in self.notes:
            self.notes.append(message)

    def summary(self) -> str:
        bits = [f"{len(self.delivered)} alert(s) sent"]
        if self.duplicates:
            bits.append(f"{self.duplicates} already seen")
        if self.below_threshold:
            bits.append(f"{self.below_threshold} below min_severity")
        if self.capped:
            bits.append(f"{self.capped} capped")
        if self.graded:
            bits.append(f"{len(self.graded)} graded")
        if self.errors:
            bits.append(f"{len(self.errors)} error(s)")
        return ", ".join(bits)


class Providers:
    """Lazily constructed provider handles, built only if a detector needs them.

    A construction failure (missing key, placeholder SEC contact, no IBKR
    gateway) is cached and re-raised, so it costs one report per run instead of
    one per ticker.
    """

    def __init__(self, config: Config, now: datetime | None = None):
        self.config = config
        #: The run clock, needed only by the snapshot provider so a replay cannot
        #: see bars dated after the moment being replayed.
        self.now = now
        self._built: dict[str, object] = {}
        self._setup_errors: dict[str, SetupError] = {}

    def _get(self, role: str, factory):
        if role in self._setup_errors:
            raise self._setup_errors[role]
        if role not in self._built:
            try:
                self._built[role] = factory()
            except SetupError as exc:
                self._setup_errors[role] = exc
                raise
        return self._built[role]

    @property
    def bars(self):
        """Whichever bars provider is configured — FMP, an IBKR gateway, or a file."""
        if self.config.providers.bars == "ibkr":
            return self.ibkr
        if self.config.providers.bars == "snapshot":
            return self.snapshot
        return self._get(
            "bars",
            lambda: FMPProvider(
                api_key=os.environ.get("FMP_API_KEY", ""),
                base_url=self.config.providers.fmp_base_url,
                timeout=self.config.providers.request_timeout,
            ),
        )

    @property
    def ibkr(self):
        if self.config.providers.option_volume == "snapshot":
            return self.snapshot

        def build() -> IBKRProvider:
            provider = IBKRProvider(
                base_url=self.config.providers.ibkr_base_url,
                fields=self.config.providers.ibkr_fields,
                volume_multiplier=self.config.providers.ibkr_volume_multiplier,
                timeout=self.config.providers.request_timeout,
            )
            provider.check_auth()
            return provider

        return self._get("ibkr", build)

    @property
    def snapshot(self) -> SnapshotBars:
        return self._get(
            "snapshot",
            lambda: SnapshotBars(self.config.providers.snapshot_path, as_of=self.now),
        )

    @property
    def replaying(self) -> SnapshotBars | None:
        """The snapshot in use, if any — for the footer's replay warning."""
        providers = self.config.providers
        if "snapshot" not in {
            providers.bars, providers.trades, providers.flow, providers.option_volume
        }:
            return None
        try:
            return self.snapshot
        except SetupError:
            return None

    @property
    def uw(self):
        """Prints and flow — live, or replayed from the same snapshot as bars."""
        if self.config.providers.trades == "snapshot":
            return self.snapshot
        return self._get(
            "uw",
            lambda: UnusualWhalesProvider(
                api_key=os.environ.get("UW_API_KEY", ""),
                base_url=self.config.providers.uw_base_url,
                paths=self.config.providers.uw_paths,
                timeout=self.config.providers.request_timeout,
            ),
        )

    @property
    def sec(self) -> SECEdgarProvider:
        return self._get(
            "sec",
            lambda: SECEdgarProvider(
                user_agent=self.config.providers.sec_user_agent,
                timeout=self.config.providers.request_timeout,
            ),
        )

    def close(self) -> None:
        for handle in self._built.values():
            try:
                handle.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - teardown must not mask results
                pass


def run(
    config: Config,
    state: State,
    notifier: Notifier,
    *,
    now: datetime | None = None,
    force: bool = False,
    providers: Providers | None = None,
    canslim=None,
) -> RunResult:
    """One polling cycle.

    `providers` and `canslim` are injectable so tests can drive the whole
    pipeline without a network; production leaves them unset.
    """
    now = now or datetime.now(timezone.utc)
    result = RunResult(started_at=now)
    health = HealthTracker(state, unresponsive_after=int(config.run["unresponsive_after"]))

    open_now, why = cal.should_run(
        now.astimezone(cal.ET), extended_hours=bool(config.run["extended_hours"])
    )
    result.session_note = why
    result.ran_session_detectors = open_now or force
    if not result.ran_session_detectors:
        log.info("market closed (%s) — running insider detector only", why)

    enabled = config.enabled_detectors()
    active = [
        name
        for name in enabled
        if result.ran_session_detectors or name not in SESSION_BOUND
    ]
    if not active:
        result.notes.append(f"nothing to do: {why}")
        return result

    owned = providers is None
    providers = providers or Providers(config, now=now)
    candidates: list[Alert] = []
    # Newest event timestamp per feed across the whole watchlist. One thin ticker
    # going quiet proves nothing; all of them going quiet means a frozen feed.
    feed_newest: dict[str, datetime | None] = {}
    try:
        for ticker in config.tickers:
            candidates.extend(
                _process_ticker(
                    config, state, providers, health, ticker, active, now, result,
                    feed_newest,
                )
            )
        replaying = providers.replaying
        if replaying is not None:
            result.replay_note = replaying.provenance(now)
            log.warning("%s", result.replay_note)
    finally:
        if owned:
            providers.close()

    extended = bool(config.run["extended_hours"])
    for source, newest in feed_newest.items():
        health.record_feed_advance(
            source,
            newest,
            now,
            silence_minutes=int(config.run["max_feed_silence_minutes"]),
            extended_hours=extended,
        )

    result.health_summary = health.summary_message()
    _deliver(config, state, notifier, candidates, result, now, canslim)

    state.prune(
        now,
        int(config.run["state_retention_days"]),
        cluster_days=_widest_cluster_window(config),
    )
    state.record_run(now, len(result.delivered), result.summary())
    state.commit()
    return result


def _widest_cluster_window(config: Config) -> int:
    """The largest cluster window any ticker uses — per-ticker overrides included."""
    windows = [int(config.detector("insider_trades").get("cluster_window_days", 0))]
    windows.extend(
        int(config.detector("insider_trades", ticker).get("cluster_window_days", 0))
        for ticker in config.tickers
    )
    return max(windows, default=0)


def _process_ticker(
    config: Config,
    state: State,
    providers: Providers,
    health: HealthTracker,
    ticker: str,
    active: list[str],
    now: datetime,
    result: RunResult,
    feed_newest: dict[str, datetime | None] | None = None,
) -> list[Alert]:
    ctx = Context(
        ticker=ticker,
        now=now,
        settings={},
        state=state,
        cold_start_minutes=int(config.run["cold_start_lookback_minutes"]),
    )

    feeds = feed_newest if feed_newest is not None else {}

    def fetch(label: str, action):
        """Run a provider call, turning failure into a health record."""
        try:
            value = action()
        except SetupError as exc:
            # Not ticker-specific — report it once for the whole run.
            result.error(f"{label} unavailable: {exc}")
            return None
        except ProviderError as exc:
            result.error(f"{ticker} {label}: {exc}")
            health.record_failure(label, ticker, str(exc)[:160])
            return None
        health.record_success(label, ticker)
        return value

    def note_freshness(source: str, timestamps: list[datetime]) -> None:
        """Track the newest event this feed has shown us, across all tickers."""
        current = feeds.get(source)
        newest = max(timestamps, default=None)
        if newest is not None and (current is None or newest > current):
            feeds[source] = newest
        feeds.setdefault(source, current)

    needs_bars = "volume_anomaly" in active
    wants_adv = any(
        name in active
        and float(config.detector(name, ticker).get("min_pct_of_adv", 0)) > 0
        for name in ("block_trades", "dark_pool")
    )
    if needs_bars or wants_adv:
        vol_settings = config.detector("volume_anomaly", ticker)
        interval = str(vol_settings["bar_interval"])
        sessions = int(vol_settings["baseline_sessions"])
        bars = fetch("bars", lambda: providers.bars.intraday_bars(ticker, interval))
        if bars is not None:
            clean, findings = inspect_bars(
                ticker,
                bars,
                source="bars",
                now=now,
                interval_minutes=int(INTERVAL_MINUTES.get(interval, 5)),
                max_stale_intervals=int(config.run["max_stale_intervals"]),
                extended_hours=bool(config.run["extended_hours"]),
            )
            if findings:
                health.record_findings(findings)
                for finding in findings:
                    ctx.note(f"{finding.state.value} bars — {finding.detail}")
            else:
                health.clear_findings("bars", ticker)
            ctx.bars = clean
            ctx.adv = providers.bars.average_daily_volume(clean, sessions, now=now)

    if {"block_trades", "dark_pool"} & set(active):
        prints = fetch("prints", lambda: providers.uw.dark_pool_prints(ticker))
        if prints is not None:
            findings = inspect_events(
                ticker, "prints", [p.ts for p in prints], now=now
            )
            if findings:
                health.record_findings(findings)
                # Future-dated prints would evade every watermark, so drop them.
                horizon = now
                prints = [p for p in prints if p.ts <= horizon]
            else:
                health.clear_findings("prints", ticker)
            ctx.prints = prints
            note_freshness("prints", [p.ts for p in prints])

    if "options_flow" in active:
        flow = fetch("options flow", lambda: providers.uw.flow_alerts(ticker))
        if flow is not None:
            findings = inspect_events(ticker, "options flow", [f.ts for f in flow], now=now)
            if findings:
                health.record_findings(findings)
                flow = [f for f in flow if f.ts <= now]
            else:
                health.clear_findings("options flow", ticker)
            ctx.option_trades = flow
            note_freshness("options flow", [f.ts for f in flow])

    if "option_volume" in active:
        snapshot = fetch(
            "option volume", lambda: providers.ibkr.option_volume_ratio(ticker)
        )
        if snapshot is not None:
            today, average, _ = snapshot
            calls, puts = _call_put(providers, ticker)
            ctx.option_volume = OptionVolumeSnapshot(
                ticker=ticker,
                today_volume=today,
                average_volume=average,
                call_volume=calls,
                put_volume=puts,
            )

    if "insider_trades" in active:
        ctx.insider_transactions = (
            fetch(
                "insider",
                lambda: _fetch_insider(
                    providers, ticker, config.detector("insider_trades", ticker), state, now
                ),
            )
            or []
        )

    alerts: list[Alert] = []
    for name in active:
        ctx.settings = config.detector(name, ticker)
        detector = DETECTOR_CLASSES[name]()
        try:
            alerts.extend(detector.run(ctx))
        except Exception as exc:  # noqa: BLE001 - one detector must not sink the run
            log.exception("%s failed on %s", name, ticker)
            result.error(f"{ticker} {name}: {type(exc).__name__}: {exc}")

    result.notes.extend(ctx.notes)
    return alerts


def _call_put(providers: Providers, ticker: str) -> tuple[float | None, float | None]:
    """Best-effort call/put split — the skew, when the gateway exposes it."""
    try:
        snap = providers.ibkr.snapshot(ticker)
    except (ProviderError, SetupError):
        return None, None
    return snap.get("option_call_volume"), snap.get("option_put_volume")


def _fetch_insider(
    providers: Providers,
    ticker: str,
    settings: dict,
    state: State,
    now: datetime,
) -> list:
    """Pull Form 4 lines for filings we have not already read."""
    lookback = int(settings["lookback_days"])
    mark = state.watermark("insider_trades", ticker)
    since = default_since(lookback)
    if mark is not None:
        # Re-read from the day of the last filing seen; dedup handles overlap.
        since = max(since, mark.date())

    out: list = []
    for filing in providers.sec.recent_form4_filings(ticker, since):
        try:
            out.extend(providers.sec.fetch_transactions(ticker, filing))
        except ProviderError as exc:
            log.warning(
                "could not read Form 4 %s for %s: %s",
                filing.get("accessionNumber"),
                ticker,
                exc,
            )
    return out


def _deliver(
    config: Config,
    state: State,
    notifier: Notifier,
    candidates: list[Alert],
    result: RunResult,
    now: datetime,
    canslim=None,
) -> None:
    floor = Severity(str(config.run["min_severity"])).rank
    global_cap = int(config.run["max_alerts_per_run"])

    fresh: list[Alert] = []
    for alert in candidates:
        if alert.severity.rank < floor:
            result.below_threshold += 1
            continue
        if not state.is_new(alert.dedup_id):
            result.duplicates += 1
            continue
        fresh.append(alert)

    # Most severe and most recent first, so the cap drops the least useful.
    fresh.sort(key=lambda a: (-a.severity.rank, -a.occurred_at.timestamp()))
    if len(fresh) > global_cap:
        result.capped = len(fresh) - global_cap
        result.note(
            f"{result.capped} alert(s) dropped by run.max_alerts_per_run={global_cap}"
        )
        fresh = fresh[:global_cap]

    grade_floor = Severity(str(config.run["canslim_min_severity"])).rank
    want_grades = bool(config.run["attach_canslim"]) and canslim is not None

    max_age = int(config.run["canslim_max_age_minutes"])

    failed_pairs: set[tuple[str, str]] = set()
    for alert in fresh:
        files: list[Path] = []
        if want_grades and alert.severity.rank >= grade_floor:
            files = _grade_files(canslim, alert, result, now, max_age)

        if notifier.send(alert, files=files):
            state.mark_seen(alert.dedup_id, alert.detector, alert.ticker, now)
            state.record_signal(
                dedup_id=alert.dedup_id,
                ticker=alert.ticker,
                detector=alert.detector,
                severity=alert.severity.value,
                headline=alert.headline,
                detail=_plain(alert.lines[0]) if alert.lines else "",
                occurred_at=alert.occurred_at,
                url=alert.url,
            )
            result.delivered.append(alert)
        else:
            failed_pairs.add((alert.detector, alert.ticker))
            result.error(
                f"delivery failed for {alert.ticker} {alert.detector}; will retry next run"
            )

    state.flush_progress(skip=failed_pairs)

    footer = _footer(config, result)
    if footer:
        notifier.send_summary(footer)


def _grade_files(
    canslim, alert: Alert, result: RunResult, now: datetime, max_age_minutes: int
) -> list[Path]:
    """Grade the ticker and return the attachment, or nothing if it can't be done.

    A missing report must never cost the alert it was going to be attached to,
    so every failure here is recorded as a note and the alert goes out bare.
    """
    try:
        outcome = canslim.grade(alert.ticker, now, max_age_minutes=max_age_minutes)
    except Exception as exc:  # noqa: BLE001 - grading is a nicety, not the point
        log.warning("CAN SLIM grading failed for %s: %s", alert.ticker, exc)
        result.note(f"{alert.ticker}: CAN SLIM grading failed ({exc})")
        return []

    if not outcome.ok or outcome.report is None:
        result.note(f"{alert.ticker}: no CAN SLIM report ({outcome.skipped})")
        return []

    report = outcome.report
    # Say how old the figures are rather than implying they are current. A grade
    # reused from earlier in the session quotes the price it was computed at.
    age = report.age_minutes(now)
    stamp = ""
    if report.from_cache and age is not None and age >= 1:
        stamp = f" <i>(figures as of {age:.0f} min ago)</i>"

    if alert.ticker not in result.graded:
        result.graded.append(alert.ticker)
        alert.lines.append(
            f"\n📄 <b>CAN SLIM: {html.escape(report.grade.verdict)}</b> "
            f"({html.escape(report.grade.score_text)}) — "
            f"{html.escape(report.grade.summary[:180])}{stamp}"
        )
    else:
        alert.lines.append(
            f"\n📄 CAN SLIM: {html.escape(report.grade.one_line())}"
            + (stamp or " <i>(graded earlier this run)</i>")
        )

    if report.has_pdf and report.pdf_path:
        return [report.pdf_path]
    if report.pdf_error:
        result.note(
            f"{alert.ticker}: PDF export unavailable ({report.pdf_error[:100]}) — "
            "sending the HTML report instead"
        )
    return [report.html_path]


def _plain(text: str) -> str:
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _footer(config: Config, result: RunResult) -> str:
    """A quiet digest message, sent only when there is something to say.

    Error strings can contain raw provider response bodies, so everything
    interpolated here is escaped.
    """
    esc = html.escape
    blocks: list[str] = []
    if result.replay_note:
        blocks.append("⏪ <b>Replay run</b>\n" + esc(result.replay_note))
    if result.health_summary:
        blocks.append(result.health_summary)
    if config.issues:
        blocks.append(
            "⚙️ <b>Config adjusted at runtime</b>\n"
            + "\n".join(f"• {esc(i.path)}: {esc(i.message)}" for i in config.issues[:10])
        )
    if result.errors:
        blocks.append(
            "⚠️ <b>Errors this run</b>\n"
            + "\n".join(f"• {esc(e)}" for e in result.errors[:10])
        )
    notable = [
        n
        for n in result.notes
        if any(word in n for word in ("suppressed", "unavailable", "stale", "corrupt", "failed"))
    ]
    if notable:
        blocks.append(
            "ℹ️ <b>Notes</b>\n" + "\n".join(f"• {esc(n)}" for n in notable[:10])
        )
    return "\n\n".join(blocks)
