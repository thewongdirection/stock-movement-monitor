"""Run orchestration: fetch once per ticker, run every detector, deliver.

Design notes worth knowing when changing this:

* **Data is fetched per ticker, not per detector.** Bars feed both the L1
  detector and the average-daily-volume figure the L2 detectors threshold on,
  so fetching once keeps the request count at roughly one per provider per
  ticker per run.
* **Market-hours gating is per detector.** Price and flow detectors only make
  sense while the market is trading, but Form 4 filings arrive on EDGAR until
  roughly 22:00 ET, so the insider detector runs whenever the cron fires.
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

from . import market_calendar as cal
from .config import Config
from .detectors import (
    BlockTradeDetector,
    Context,
    DarkPoolDetector,
    Detector,
    InsiderTradeDetector,
    OptionsFlowDetector,
    VolumeAnomalyDetector,
)
from .models import Alert, Severity
from .notify.base import Notifier
from .providers.base import ProviderError, SetupError
from .providers.fmp import FMPProvider
from .providers.sec_edgar import SECEdgarProvider, default_since
from .providers.unusual_whales import UnusualWhalesProvider
from .state import State

log = logging.getLogger(__name__)

#: Detectors that only make sense while the market is trading.
SESSION_BOUND = frozenset({"volume_anomaly", "block_trades", "dark_pool", "options_flow"})

DETECTOR_CLASSES: dict[str, type[Detector]] = {
    d.name: d
    for d in (
        VolumeAnomalyDetector,
        BlockTradeDetector,
        DarkPoolDetector,
        OptionsFlowDetector,
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

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, message: str) -> None:
        """Record an error once. Setup failures would otherwise repeat per ticker."""
        if message not in self.errors:
            self.errors.append(message)

    def summary(self) -> str:
        bits = [f"{len(self.delivered)} alert(s) sent"]
        if self.duplicates:
            bits.append(f"{self.duplicates} already seen")
        if self.below_threshold:
            bits.append(f"{self.below_threshold} below min_severity")
        if self.capped:
            bits.append(f"{self.capped} capped")
        if self.errors:
            bits.append(f"{len(self.errors)} error(s)")
        return ", ".join(bits)


class Providers:
    """Lazily constructed provider handles, built only if a detector needs them.

    A construction failure (missing key, placeholder SEC contact) is cached and
    re-raised, so it costs one report per run instead of one per ticker.
    """

    def __init__(self, config: Config):
        self.config = config
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
    def bars(self) -> FMPProvider:
        return self._get(
            "bars",
            lambda: FMPProvider(
                api_key=os.environ.get("FMP_API_KEY", ""),
                base_url=self.config.providers.fmp_base_url,
                timeout=self.config.providers.request_timeout,
            ),
        )

    @property
    def uw(self) -> UnusualWhalesProvider:
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
) -> RunResult:
    """One polling cycle.

    `providers` is injectable so tests can drive the whole pipeline without a
    network; production leaves it unset and gets lazily built real ones.
    """
    now = now or datetime.now(timezone.utc)
    result = RunResult(started_at=now)

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
    providers = providers or Providers(config)
    candidates: list[Alert] = []
    try:
        for ticker in config.tickers:
            candidates.extend(
                _process_ticker(config, state, providers, ticker, active, now, result)
            )
    finally:
        if owned:
            providers.close()

    _deliver(config, state, notifier, candidates, result, now)

    state.prune(now, int(config.run["state_retention_days"]))
    state.record_run(now, len(result.delivered), result.summary())
    state.commit()
    return result


def _process_ticker(
    config: Config,
    state: State,
    providers: Providers,
    ticker: str,
    active: list[str],
    now: datetime,
    result: RunResult,
) -> list[Alert]:
    ctx = Context(
        ticker=ticker,
        now=now,
        settings={},
        state=state,
        cold_start_minutes=int(config.run["cold_start_lookback_minutes"]),
    )

    needs_bars = "volume_anomaly" in active
    wants_adv = any(
        name in active
        and float(config.detector(name, ticker).get("min_pct_of_adv", 0)) > 0
        for name in ("block_trades", "dark_pool")
    )
    def fetch(label: str, action):
        """Run a provider call, turning failure into a report rather than a stop."""
        try:
            return action()
        except SetupError as exc:
            # Not ticker-specific — report it once for the whole run.
            result.error(f"{label} unavailable: {exc}")
        except ProviderError as exc:
            result.error(f"{ticker} {label}: {exc}")
        return None

    if needs_bars or wants_adv:
        interval = str(config.detector("volume_anomaly", ticker)["bar_interval"])
        sessions = int(config.detector("volume_anomaly", ticker)["baseline_sessions"])
        bars = fetch("bars", lambda: providers.bars.intraday_bars(ticker, interval))
        if bars is not None:
            ctx.bars = bars
            ctx.adv = providers.bars.average_daily_volume(bars, sessions)

    if {"block_trades", "dark_pool"} & set(active):
        ctx.prints = fetch("prints", lambda: providers.uw.dark_pool_prints(ticker)) or []

    if "options_flow" in active:
        ctx.option_trades = fetch("options flow", lambda: providers.uw.flow_alerts(ticker)) or []

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
        result.notes.append(
            f"{result.capped} alert(s) dropped by run.max_alerts_per_run={global_cap}"
        )
        fresh = fresh[:global_cap]

    failed_pairs: set[tuple[str, str]] = set()
    for alert in fresh:
        if notifier.send(alert):
            state.mark_seen(alert.dedup_id, alert.detector, alert.ticker, now)
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


def _footer(config: Config, result: RunResult) -> str:
    """A quiet digest message, sent only when there is something to say.

    Error strings can contain raw provider response bodies, so everything
    interpolated here is escaped.
    """
    esc = html.escape
    blocks: list[str] = []
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
    notable = [n for n in result.notes if "suppressed" in n or "unavailable" in n]
    if notable:
        blocks.append(
            "ℹ️ <b>Notes</b>\n" + "\n".join(f"• {esc(n)}" for n in notable[:10])
        )
    return "\n\n".join(blocks)
