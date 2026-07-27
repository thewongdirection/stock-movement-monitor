"""Health escalation, recovery, and the bot-writable runtime overlay."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from conftest import NOW, make_trade
from tickers import bars_for

from monitor import config as config_mod, engine
from monitor.health import Finding, HealthState, HealthTracker
from monitor.market_calendar import ET
from monitor.models import Bar
from monitor.providers.base import ProviderError
from monitor.runtime import Overlay, OverlayError
from monitor.state import State


# --------------------------------------------------------------------------
# Escalation and recovery
# --------------------------------------------------------------------------
def test_one_failure_does_not_escalate(state):
    tracker = HealthTracker(state, unresponsive_after=3)
    tracker.record_failure("bars", "AAPL", "HTTP 503")
    assert tracker.report.findings
    assert tracker.report.escalated == []


def test_the_third_consecutive_failure_escalates(state):
    tracker = HealthTracker(state, unresponsive_after=3)
    for _ in range(3):
        tracker = HealthTracker(state, unresponsive_after=3)
        tracker.record_failure("bars", "AAPL", "HTTP 503")
    assert len(tracker.report.escalated) == 1
    assert "3 consecutive failures" in tracker.report.escalated[0].detail


def test_a_persistent_outage_stops_repeating_itself(state):
    """Report once, then stay quiet — nobody wants it every five minutes."""
    for _ in range(10):
        tracker = HealthTracker(state, unresponsive_after=3)
        tracker.record_failure("bars", "AAPL", "HTTP 503")
    assert tracker.report.findings == []


def test_recovery_is_announced_only_after_a_real_outage(state):
    for _ in range(4):
        HealthTracker(state, unresponsive_after=3).record_failure("bars", "AAPL", "down")
    tracker = HealthTracker(state, unresponsive_after=3)
    tracker.record_success("bars", "AAPL")
    assert tracker.report.recovered
    assert "responding again" in tracker.report.recovered[0]


def test_success_after_a_single_blip_is_not_announced(state):
    HealthTracker(state, unresponsive_after=3).record_failure("bars", "AAPL", "blip")
    tracker = HealthTracker(state, unresponsive_after=3)
    tracker.record_success("bars", "AAPL")
    assert tracker.report.recovered == []


def test_failures_are_tracked_per_source_and_ticker(state):
    tracker = HealthTracker(state, unresponsive_after=2)
    tracker.record_failure("bars", "AAPL", "x")
    tracker.record_failure("bars", "MSFT", "x")
    tracker.record_failure("prints", "AAPL", "x")
    assert state.counter("health:bars:AAPL") == 1
    assert state.counter("health:bars:MSFT") == 1
    assert state.counter("health:prints:AAPL") == 1


def test_corruption_escalates_immediately(state):
    """Corrupt data produces false alerts, so waiting to report it is wrong."""
    tracker = HealthTracker(state, unresponsive_after=5)
    tracker.record_findings(
        [Finding("bars", "AAPL", HealthState.CORRUPT, "dropped 3 of 40 bars")]
    )
    assert len(tracker.report.escalated) == 1


def test_staleness_escalates_only_once_it_persists(state):
    finding = Finding("bars", "AAPL", HealthState.STALE, "newest bar is 90 min old")
    first = HealthTracker(state, unresponsive_after=3)
    first.record_findings([finding])
    assert first.report.escalated == []

    for _ in range(2):
        later = HealthTracker(state, unresponsive_after=3)
        later.record_findings([finding])
    assert later.report.escalated


def test_summary_message_escapes_provider_text(state):
    tracker = HealthTracker(state, unresponsive_after=1)
    tracker.record_failure("bars", "AAPL", "<script>alert(1)</script>")
    message = tracker.summary_message()
    assert "<script>" not in message
    assert "&lt;script&gt;" in message


def test_summary_is_empty_when_healthy(state):
    tracker = HealthTracker(state, unresponsive_after=3)
    tracker.record_success("bars", "AAPL")
    assert tracker.summary_message() == ""


# --------------------------------------------------------------------------
# Health inside a real run
# --------------------------------------------------------------------------
class FlakyBars:
    def __init__(self, bars=None, fail: bool = False):
        self._bars = bars if bars is not None else bars_for("AAPL")
        self.fail = fail

    def intraday_bars(self, symbol, interval, lookback_days=45):
        if self.fail:
            raise ProviderError("HTTP 503: upstream unavailable")
        return self._bars

    def average_daily_volume(self, bars, sessions, now=None):
        return 55_000_000

    def close(self):
        pass


class Providers:
    def __init__(self, bars):
        self.bars = bars

    def close(self):
        pass


class Recorder:
    def __init__(self):
        self.alerts, self.summaries = [], []

    def send(self, alert, files=None):
        self.alerts.append(alert)
        return True

    def send_summary(self, text):
        self.summaries.append(text)
        return True


def only_volume(**settings):
    detectors = {n: {"enabled": n == "volume_anomaly"} for n in config_mod.DETECTOR_SPECS}
    detectors["volume_anomaly"].update(settings)
    return config_mod.from_dict({"tickers": ["AAPL"], "detectors": detectors})


def test_repeated_outage_reaches_the_run_footer(state):
    cfg = only_volume()
    cfg.run["unresponsive_after"] = 2
    providers = Providers(FlakyBars(fail=True))
    recorder = Recorder()

    engine.run(cfg, state, recorder, now=NOW, providers=providers)
    result = engine.run(cfg, state, recorder, now=NOW, providers=providers)

    assert "Data source problems" in result.health_summary
    assert "unresponsive" in result.health_summary
    assert any("Data source problems" in s for s in recorder.summaries)


def test_corrupt_bars_are_dropped_before_a_detector_sees_them(state):
    """The whole point: a corrupt bar must not become a confident alert."""
    bars = bars_for("AAPL", sessions=21)
    # A bar with a colossal fake volume that would trivially trip the detector.
    poisoned = list(bars)
    victim = poisoned[-1]
    poisoned[-1] = Bar(
        ts=victim.ts, open=victim.open, high=victim.high, low=victim.low,
        close=victim.close, volume=-999_999_999,
    )
    cfg = only_volume()
    recorder = Recorder()
    result = engine.run(
        cfg, state, recorder, now=NOW, providers=Providers(FlakyBars(poisoned))
    )
    assert result.delivered == []
    assert "Data source problems" in result.health_summary
    assert "corrupt" in result.health_summary


def test_a_stale_feed_is_surfaced_in_the_run(state):
    stale = bars_for("AAPL", sessions=21, anchor=datetime(2026, 7, 24, 10, 0, tzinfo=ET))
    cfg = only_volume()
    midday = datetime(2026, 7, 27, 14, 0, tzinfo=ET)
    result = engine.run(
        cfg, state, Recorder(), now=midday, providers=Providers(FlakyBars(stale))
    )
    assert "stale" in result.health_summary or any("stale" in n for n in result.notes)


# --------------------------------------------------------------------------
# Runtime overlay
# --------------------------------------------------------------------------
@pytest.fixture
def overlay(tmp_path) -> Overlay:
    return Overlay(path=tmp_path / "runtime.json")


def test_add_and_remove_round_trip(overlay):
    overlay.add_ticker("pltr")
    cfg = config_mod.from_dict({"tickers": ["AAPL"]}, overlay=overlay)
    assert cfg.tickers == ["AAPL", "PLTR"]

    overlay.remove_ticker("AAPL", cfg.baseline_tickers)
    cfg = config_mod.from_dict({"tickers": ["AAPL"]}, overlay=overlay)
    assert cfg.tickers == ["PLTR"]


def test_re_adding_a_removed_baseline_ticker_restores_it(overlay):
    overlay.remove_ticker("AAPL", ["AAPL"])
    overlay.add_ticker("AAPL")
    cfg = config_mod.from_dict({"tickers": ["AAPL"]}, overlay=overlay)
    assert cfg.tickers == ["AAPL"]


def test_removing_an_unwatched_ticker_is_rejected(overlay):
    with pytest.raises(OverlayError, match="not on the watchlist"):
        overlay.remove_ticker("ZZZZ", ["AAPL"])


def test_invalid_ticker_is_rejected_with_a_useful_message(overlay):
    with pytest.raises(OverlayError, match="does not look like a ticker"):
        overlay.add_ticker("not a ticker!")


@pytest.mark.parametrize(
    "text,expected",
    [("2.5M", 2_500_000), ("500k", 500_000), ("1B", 1_000_000_000),
     ("1,250,000", 1_250_000), ("$750000", 750_000)],
)
def test_human_shorthand_for_large_numbers(overlay, text, expected):
    overlay.set_detector("dark_pool", "min_notional", text)
    assert overlay.detectors["dark_pool"]["min_notional"] == expected


@pytest.mark.parametrize("text", ["on", "true", "yes", "1", "enable"])
def test_boolean_synonyms_accepted(overlay, text):
    overlay.set_detector("options_flow", "require_volume_gt_oi", text)
    assert overlay.detectors["options_flow"]["require_volume_gt_oi"] is True


@pytest.mark.parametrize("text", ["off", "false", "no", "0", "disable"])
def test_boolean_negatives_accepted(overlay, text):
    overlay.set_detector("options_flow", "require_volume_gt_oi", text)
    assert overlay.detectors["options_flow"]["require_volume_gt_oi"] is False


def test_out_of_range_is_rejected_not_clamped(overlay):
    """A bot user asked for a number; silently substituting another is worse."""
    with pytest.raises(OverlayError, match="above the supported range"):
        overlay.set_detector("volume_anomaly", "rvol_threshold", "500")


def test_below_range_is_rejected(overlay):
    with pytest.raises(OverlayError, match="below the supported range"):
        overlay.set_detector("volume_anomaly", "rvol_threshold", "0.1")


def test_unknown_setting_names_the_valid_ones(overlay):
    with pytest.raises(OverlayError, match="rvol_threshold"):
        overlay.set_detector("volume_anomaly", "rvol_treshold", "3")


def test_unknown_detector_names_the_valid_ones(overlay):
    with pytest.raises(OverlayError, match="volume_anomaly"):
        overlay.set_detector("volume_anomally", "rvol_threshold", "3")


def test_non_numeric_value_is_rejected(overlay):
    with pytest.raises(OverlayError, match="expected a number"):
        overlay.set_detector("volume_anomaly", "rvol_threshold", "high")


def test_choice_setting_is_validated(overlay):
    with pytest.raises(OverlayError):
        overlay.set_detector("volume_anomaly", "combine", "either")


def test_per_ticker_overlay_only_affects_that_ticker(overlay):
    overlay.set_detector("volume_anomaly", "rvol_threshold", "4", ticker="TSLA")
    cfg = config_mod.from_dict({"tickers": ["TSLA", "AAPL"]}, overlay=overlay)
    assert cfg.detector("volume_anomaly", "TSLA")["rvol_threshold"] == 4.0
    assert cfg.detector("volume_anomaly", "AAPL")["rvol_threshold"] == 2.0


def test_reset_one_detector_leaves_the_others(overlay):
    overlay.set_detector("volume_anomaly", "rvol_threshold", "4")
    overlay.set_detector("dark_pool", "min_notional", "2M")
    overlay.reset("volume_anomaly")
    assert "volume_anomaly" not in overlay.detectors
    assert overlay.detectors["dark_pool"]["min_notional"] == 2_000_000


def test_reset_all_keeps_the_watchlist(overlay):
    overlay.add_ticker("PLTR")
    overlay.set_detector("volume_anomaly", "rvol_threshold", "4")
    overlay.reset()
    assert overlay.detectors == {}
    assert overlay.added_tickers == ["PLTR"]


def test_persistence_round_trip(tmp_path):
    path = tmp_path / "runtime.json"
    first = Overlay(path=path)
    first.add_ticker("PLTR")
    first.set_detector("dark_pool", "min_notional", "3M")
    first.set_enabled("options_flow", False)
    first.save()

    second = Overlay.load(path)
    assert second.added_tickers == ["PLTR"]
    assert second.detectors["dark_pool"]["min_notional"] == 3_000_000
    assert second.detectors["options_flow"]["enabled"] is False


def test_a_corrupt_overlay_file_falls_back_to_the_committed_config(tmp_path):
    """A truncated JSON write must not take the monitor down."""
    path = tmp_path / "runtime.json"
    path.write_text('{"added_tickers": ["PLTR"')
    overlay = Overlay.load(path)
    assert overlay.is_empty()
    assert any("corrupt" in change.what for change in overlay.log)

    cfg = config_mod.from_dict({"tickers": ["AAPL"]}, overlay=overlay)
    assert cfg.tickers == ["AAPL"]


def test_overlay_edits_are_validated_by_the_same_code_as_the_yaml(overlay):
    """The overlay path must not be a way to smuggle in an invalid threshold."""
    overlay.detectors["volume_anomaly"] = {"rvol_threshold": 9999}
    cfg = config_mod.from_dict({"tickers": ["AAPL"]}, overlay=overlay)
    assert cfg.detector("volume_anomaly")["rvol_threshold"] == 20.0
    assert any("above the supported range" in i.message for i in cfg.issues)


def test_describe_lists_every_change(overlay):
    overlay.add_ticker("PLTR")
    overlay.set_detector("dark_pool", "min_notional", "2M")
    overlay.set_detector("volume_anomaly", "rvol_threshold", "3", ticker="PLTR")
    overlay.set_run("min_severity", "high")
    described = "\n".join(overlay.describe())
    assert "PLTR" in described
    assert "dark_pool.min_notional" in described
    assert "run.min_severity" in described


# --------------------------------------------------------------------------
# Narrator settings in the overlay
# --------------------------------------------------------------------------
def test_narrator_can_be_switched_on_from_the_overlay(overlay):
    assert overlay.set_canslim("narrator", "llm") == "canslim.narrator set to llm."
    cfg = config_mod.from_dict({"tickers": ["AAPL"]}, overlay=overlay)
    assert cfg.canslim["narrator"] == "llm"


def test_an_unknown_narrator_backend_is_rejected_not_clamped(overlay):
    """Bot edits are rejected rather than silently corrected, same as thresholds."""
    with pytest.raises(OverlayError) as exc:
        overlay.set_canslim("narrator", "gpt")
    assert "not allowed" in str(exc.value)
    assert overlay.canslim == {}


def test_an_out_of_range_token_budget_is_rejected(overlay):
    with pytest.raises(OverlayError) as exc:
        overlay.set_canslim("narrator_max_tokens", "999999")
    assert "2,000 to 64,000" in str(exc.value)


def test_an_unknown_narrator_setting_names_the_valid_ones(overlay):
    with pytest.raises(OverlayError) as exc:
        overlay.set_canslim("temperature", "0.7")
    assert "narrator_effort" in str(exc.value)


def test_narrator_research_takes_the_boolean_synonyms(overlay):
    overlay.set_canslim("narrator_research", "off")
    assert overlay.canslim["narrator_research"] is False


def test_narrator_settings_survive_a_save_and_reload(tmp_path):
    path = tmp_path / "runtime.json"
    first = Overlay(path=path)
    first.set_canslim("narrator", "llm")
    first.set_canslim("narrator_effort", "high")
    first.save()

    second = Overlay.load(path)
    assert second.canslim == {"narrator": "llm", "narrator_effort": "high"}
    assert not second.is_empty()


def test_reset_all_also_drops_the_narrator_change(overlay):
    overlay.set_canslim("narrator", "llm")
    overlay.reset()
    assert overlay.canslim == {}


def test_describe_reports_the_narrator_change(overlay):
    overlay.set_canslim("narrator", "llm")
    assert "canslim.narrator = llm" in overlay.describe()


# --------------------------------------------------------------------------
# Freshness: never serve old data quietly
# --------------------------------------------------------------------------
def test_every_provider_request_forbids_a_cached_response():
    """A cached quote is not a cheap win — it is a wrong answer that looks right."""
    from monitor.providers.base import RateLimitedSession

    session = RateLimitedSession()
    try:
        assert "no-store" in session.session.headers["Cache-Control"]
        assert session.session.headers["Pragma"] == "no-cache"
    finally:
        session.close()


def test_provider_headers_do_not_clobber_the_no_cache_headers():
    from monitor.providers.base import RateLimitedSession

    session = RateLimitedSession(headers={"User-Agent": "monitor test"})
    try:
        assert session.session.headers["User-Agent"] == "monitor test"
        assert "no-cache" in session.session.headers["Cache-Control"]
    finally:
        session.close()


def test_a_frozen_feed_during_extended_hours_is_still_caught():
    """This used to be skipped entirely — extended hours had no staleness check."""
    from monitor.health import inspect_bars

    premarket = datetime(2026, 7, 27, 8, 30, tzinfo=ET)
    old = premarket - timedelta(hours=3)
    bars = [Bar(ts=old, open=100, high=100.5, low=99.5, close=100, volume=1_000)]

    _, findings = inspect_bars(
        "TEST", bars, now=premarket, interval_minutes=5, extended_hours=True
    )
    assert [f.state for f in findings] == [HealthState.STALE]
    assert "extended-hours" in findings[0].detail


def test_extended_hours_tolerates_ordinary_sparseness():
    """Pre-market bars are legitimately thin; the budget widens rather than vanishing."""
    from monitor.health import inspect_bars

    premarket = datetime(2026, 7, 27, 8, 30, tzinfo=ET)
    recent = premarket - timedelta(minutes=20)
    bars = [Bar(ts=recent, open=100, high=100.5, low=99.5, close=100, volume=1_000)]

    _, findings = inspect_bars(
        "TEST", bars, now=premarket, interval_minutes=5, extended_hours=True
    )
    assert findings == []


def test_extended_hours_staleness_is_not_checked_when_not_polling_then():
    """With extended_hours off the monitor isn't looking, so silence is expected."""
    from monitor.health import inspect_bars

    premarket = datetime(2026, 7, 27, 8, 30, tzinfo=ET)
    old = premarket - timedelta(hours=5)
    bars = [Bar(ts=old, open=100, high=100.5, low=99.5, close=100, volume=1_000)]

    _, findings = inspect_bars(
        "TEST", bars, now=premarket, interval_minutes=5, extended_hours=False
    )
    assert findings == []


# --------------------------------------------------------------------------
# Frozen event feeds — HTTP 200 with yesterday's data
# --------------------------------------------------------------------------
SESSION = datetime(2026, 7, 27, 14, 30, tzinfo=ET)


def test_a_feed_that_stops_advancing_is_reported(state):
    """The dangerous case: the call succeeds and returns the same prints forever."""
    tracker = HealthTracker(state, unresponsive_after=1)
    frozen_at = SESSION - timedelta(minutes=200)

    tracker.record_feed_advance(
        "prints", frozen_at, SESSION - timedelta(minutes=10), silence_minutes=60
    )
    tracker.record_feed_advance("prints", frozen_at, SESSION, silence_minutes=60)

    stale = [f for f in tracker.report.findings if f.state is HealthState.STALE]
    assert stale, "a feed stuck for 200 minutes should be reported"
    assert "frozen, not the market" in stale[0].detail
    assert "🧊" in tracker.summary_message()


def test_a_feed_that_keeps_advancing_is_quiet(state):
    tracker = HealthTracker(state, unresponsive_after=1)
    tracker.record_feed_advance(
        "prints", SESSION - timedelta(minutes=40), SESSION - timedelta(minutes=30),
        silence_minutes=60,
    )
    tracker.record_feed_advance(
        "prints", SESSION - timedelta(minutes=2), SESSION, silence_minutes=60
    )
    assert tracker.summary_message() == ""


def test_a_quiet_but_recent_feed_is_within_budget(state):
    """A thin watchlist really can have no prints for a few minutes."""
    tracker = HealthTracker(state, unresponsive_after=1)
    newest = SESSION - timedelta(minutes=20)
    tracker.record_feed_advance("prints", newest, SESSION - timedelta(minutes=5),
                                silence_minutes=60)
    tracker.record_feed_advance("prints", newest, SESSION, silence_minutes=60)
    assert tracker.summary_message() == ""


def test_a_feed_is_not_judged_outside_a_session(state):
    """Nothing advances overnight; a count accumulated then would fire at the open."""
    tracker = HealthTracker(state, unresponsive_after=1)
    overnight = datetime(2026, 7, 27, 3, 0, tzinfo=ET)
    tracker.record_feed_advance(
        "prints", overnight - timedelta(hours=12), overnight, silence_minutes=60
    )
    assert tracker.summary_message() == ""


def test_a_feed_never_seen_is_not_called_frozen(state):
    """A brand-new watchlist has no history — that is not evidence of a fault."""
    tracker = HealthTracker(state, unresponsive_after=1)
    tracker.record_feed_advance("prints", None, SESSION, silence_minutes=60)
    assert tracker.summary_message() == ""


def test_feed_freshness_survives_between_runs(state):
    """The comparison is across cron ticks, so it has to be persisted."""
    tracker = HealthTracker(state, unresponsive_after=1)
    newest = SESSION - timedelta(minutes=5)
    tracker.record_feed_advance("prints", newest, SESSION, silence_minutes=60)

    later = HealthTracker(state, unresponsive_after=1)
    assert later.state.feed_seen("feed:prints") == newest
