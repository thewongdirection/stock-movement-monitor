"""End-to-end run behaviour, driven with stub providers instead of a network."""

from __future__ import annotations

from datetime import datetime, timedelta

from conftest import (
    NOW,
    build_bars,
    make_insider_txn,
    make_option_trade,
    make_trade,
    spike_last_bar,
)

from monitor import config as config_mod, engine
from monitor.market_calendar import ET
from monitor.models import Alert, Severity
from monitor.providers.base import ProviderError


class StubBars:
    def __init__(self, bars=None, error: Exception | None = None):
        self._bars = bars if bars is not None else build_bars()
        self._error = error

    def intraday_bars(self, symbol, interval, lookback_days=45):
        if self._error:
            raise self._error
        return self._bars

    def average_daily_volume(self, bars, sessions):
        return 10_000_000

    def close(self):
        pass


class StubUW:
    def __init__(self, prints=None, flow=None, error: Exception | None = None):
        self.prints = prints or []
        self.flow = flow or []
        self._error = error

    def dark_pool_prints(self, ticker, limit=200):
        if self._error:
            raise self._error
        return self.prints

    def flow_alerts(self, ticker, limit=200):
        return self.flow

    def close(self):
        pass


class StubSEC:
    def __init__(self, txns=None):
        self.txns = txns or []

    def cik_for(self, ticker):
        return "0000320193"

    def recent_form4_filings(self, ticker, since):
        return [{"accessionNumber": "a-1", "cik": "0000320193"}] if self.txns else []

    def fetch_transactions(self, ticker, filing):
        return self.txns

    def close(self):
        pass


class StubProviders:
    def __init__(self, bars=None, uw=None, sec=None):
        self.bars = bars or StubBars()
        self.uw = uw or StubUW()
        self.sec = sec or StubSEC()

    def close(self):
        pass


class RecordingNotifier:
    def __init__(self, fail: bool = False):
        self.alerts: list[Alert] = []
        self.summaries: list[str] = []
        self.fail = fail

    def send(self, alert):
        if self.fail:
            return False
        self.alerts.append(alert)
        return True

    def send_summary(self, text):
        self.summaries.append(text)
        return True


def config(**kwargs):
    payload = {"tickers": ["TEST"], "detectors": {}, **kwargs}
    return config_mod.from_dict(payload)


def only(detector: str, **settings):
    detectors = {
        name: {"enabled": name == detector} for name in config_mod.DETECTOR_SPECS
    }
    detectors[detector].update(settings)
    return config(detectors=detectors)


# --------------------------------------------------------------------------
def test_full_run_delivers_alerts_from_every_layer(state):
    cfg = config(
        detectors={
            "volume_anomaly": {"enabled": True},
            "block_trades": {"enabled": False},
            "dark_pool": {"enabled": True, "min_pct_of_adv": 0},
            "options_flow": {"enabled": True},
            "insider_trades": {"enabled": True},
        }
    )
    providers = StubProviders(
        bars=StubBars(spike_last_bar(build_bars(), 6.0)),
        uw=StubUW(
            prints=[make_trade(size=50_000, price=100.0)],
            flow=[make_option_trade()],
        ),
        sec=StubSEC([make_insider_txn(shares=5_000, price=100.0)]),
    )
    notifier = RecordingNotifier()

    result = engine.run(cfg, state, notifier, now=NOW, providers=providers)

    detectors = {a.detector for a in notifier.alerts}
    assert detectors == {"volume_anomaly", "dark_pool", "options_flow", "insider_trades"}
    assert result.ok
    assert len(result.delivered) == 4


def test_second_run_sends_nothing_new(state):
    cfg = only("dark_pool", min_pct_of_adv=0, cooldown_minutes=0)
    providers = StubProviders(uw=StubUW(prints=[make_trade(size=50_000, price=100.0)]))

    first = engine.run(cfg, state, RecordingNotifier(), now=NOW, providers=providers)
    assert len(first.delivered) == 1

    second = engine.run(cfg, state, RecordingNotifier(), now=NOW, providers=providers)
    assert second.delivered == []


def test_failed_delivery_is_retried_on_the_next_run(state):
    """A Telegram outage must not consume the alert."""
    cfg = only("dark_pool", min_pct_of_adv=0, cooldown_minutes=0)
    providers = StubProviders(uw=StubUW(prints=[make_trade(size=50_000, price=100.0)]))

    broken = RecordingNotifier(fail=True)
    first = engine.run(cfg, state, broken, now=NOW, providers=providers)
    assert first.delivered == []
    assert any("delivery failed" in e for e in first.errors)

    working = RecordingNotifier()
    second = engine.run(cfg, state, working, now=NOW, providers=providers)
    assert len(second.delivered) == 1


def test_market_closed_still_checks_insider_filings(state):
    """Form 4s land on EDGAR long after the closing bell."""
    cfg = config(
        detectors={
            "volume_anomaly": {"enabled": True},
            "dark_pool": {"enabled": True},
            "insider_trades": {"enabled": True},
        }
    )
    providers = StubProviders(
        bars=StubBars(spike_last_bar(build_bars(), 6.0)),
        uw=StubUW(prints=[make_trade(size=50_000, price=100.0)]),
        sec=StubSEC([make_insider_txn(shares=5_000, price=100.0)]),
    )
    notifier = RecordingNotifier()
    saturday = datetime(2026, 7, 25, 12, 0, tzinfo=ET)

    result = engine.run(cfg, state, notifier, now=saturday, providers=providers)

    assert result.ran_session_detectors is False
    assert {a.detector for a in notifier.alerts} == {"insider_trades"}


def test_force_overrides_the_session_gate(state):
    cfg = only("dark_pool", min_pct_of_adv=0)
    saturday = datetime(2026, 7, 25, 12, 0, tzinfo=ET)
    providers = StubProviders(
        uw=StubUW(prints=[make_trade(size=50_000, price=100.0, now=saturday)])
    )
    result = engine.run(
        cfg, state, RecordingNotifier(), now=saturday, providers=providers, force=True
    )
    assert len(result.delivered) == 1


def test_one_provider_failure_does_not_stop_the_others(state):
    cfg = config(
        detectors={
            "volume_anomaly": {"enabled": True},
            "insider_trades": {"enabled": True},
            "dark_pool": {"enabled": False},
            "options_flow": {"enabled": False},
        }
    )
    providers = StubProviders(
        bars=StubBars(error=ProviderError("HTTP 401: authentication rejected")),
        sec=StubSEC([make_insider_txn(shares=5_000, price=100.0)]),
    )
    notifier = RecordingNotifier()

    result = engine.run(cfg, state, notifier, now=NOW, providers=providers)

    assert {a.detector for a in notifier.alerts} == {"insider_trades"}
    assert any("401" in e for e in result.errors)
    assert result.ok is False


def test_setup_failure_is_reported_once_not_once_per_ticker(state):
    """A missing key is not a per-symbol problem and shouldn't read like one."""
    from monitor.providers.base import SetupError

    cfg = config_mod.from_dict(
        {
            "tickers": ["AAA", "BBB", "CCC"],
            "detectors": {
                name: {"enabled": name == "volume_anomaly"}
                for name in config_mod.DETECTOR_SPECS
            },
        }
    )
    providers = StubProviders(bars=StubBars(error=SetupError("FMP_API_KEY is not set")))
    result = engine.run(cfg, state, RecordingNotifier(), now=NOW, providers=providers)

    assert len(result.errors) == 1
    assert "unavailable" in result.errors[0]


def test_per_ticker_fetch_failure_is_reported_per_ticker(state):
    """A genuine per-symbol failure should still name each symbol."""
    cfg = config_mod.from_dict(
        {
            "tickers": ["AAA", "BBB"],
            "detectors": {
                name: {"enabled": name == "volume_anomaly"}
                for name in config_mod.DETECTOR_SPECS
            },
        }
    )
    providers = StubProviders(bars=StubBars(error=ProviderError("HTTP 404: not found")))
    result = engine.run(cfg, state, RecordingNotifier(), now=NOW, providers=providers)

    assert len(result.errors) == 2
    assert any("AAA" in e for e in result.errors)
    assert any("BBB" in e for e in result.errors)


def test_min_severity_filters_quiet_alerts(state):
    cfg = only("insider_trades")
    cfg.run["min_severity"] = "high"
    providers = StubProviders(
        sec=StubSEC([make_insider_txn(code="S", shares=8_000, price=100.0, title="VP")])
    )
    result = engine.run(cfg, state, RecordingNotifier(), now=NOW, providers=providers)
    assert result.delivered == []
    assert result.below_threshold == 1


def test_global_cap_limits_a_noisy_run(state):
    cfg = only("dark_pool", min_pct_of_adv=0, max_alerts_per_run=50)
    cfg.run["max_alerts_per_run"] = 3
    prints = [
        make_trade(size=30_000 + i * 5_000, price=100.0, raw_id=f"p{i}")
        for i in range(10)
    ]
    providers = StubProviders(uw=StubUW(prints=prints))
    result = engine.run(cfg, state, RecordingNotifier(), now=NOW, providers=providers)
    assert len(result.delivered) == 3
    assert result.capped == 7


def test_config_warnings_are_surfaced_in_the_footer(state):
    cfg = config_mod.from_dict(
        {
            "tickers": ["TEST"],
            "detectors": {"dark_pool": {"enabled": True, "min_notional": 1}},
        }
    )
    assert cfg.issues  # clamped below the allowed minimum
    notifier = RecordingNotifier()
    engine.run(cfg, state, notifier, now=NOW, providers=StubProviders())
    assert notifier.summaries
    assert "Config adjusted" in notifier.summaries[0]


def test_no_footer_when_the_run_is_clean(state):
    cfg = only("dark_pool", min_pct_of_adv=0)
    providers = StubProviders(uw=StubUW(prints=[make_trade(size=50_000, price=100.0)]))
    notifier = RecordingNotifier()
    engine.run(cfg, state, notifier, now=NOW, providers=providers)
    assert notifier.summaries == []


def test_per_ticker_override_reaches_the_detector(state):
    detectors = {name: {"enabled": False} for name in config_mod.DETECTOR_SPECS}
    detectors["dark_pool"] = {"enabled": True, "min_pct_of_adv": 0}
    cfg = config_mod.from_dict(
        {
            "tickers": ["TEST"],
            "detectors": detectors,
            "overrides": {"TEST": {"dark_pool": {"min_notional": 100_000_000}}},
        }
    )
    providers = StubProviders(uw=StubUW(prints=[make_trade(size=50_000, price=100.0)]))
    result = engine.run(cfg, state, RecordingNotifier(), now=NOW, providers=providers)
    assert result.delivered == []


def test_disabled_detectors_are_never_run(state):
    cfg = config(
        detectors={name: {"enabled": False} for name in config_mod.DETECTOR_SPECS}
    )
    result = engine.run(cfg, state, RecordingNotifier(), now=NOW, providers=StubProviders())
    assert result.delivered == []
    assert any("nothing to do" in n for n in result.notes)


def test_delivered_alerts_are_ordered_most_severe_first(state):
    cfg = only("insider_trades")
    providers = StubProviders(
        sec=StubSEC(
            [
                make_insider_txn(code="S", shares=8_000, price=100.0, title="VP", accession="a-1"),
                make_insider_txn(code="P", shares=30_000, price=100.0, accession="a-2"),
            ]
        )
    )
    result = engine.run(cfg, state, RecordingNotifier(), now=NOW, providers=providers)
    assert [a.severity for a in result.delivered] == [Severity.HIGH, Severity.LOW]


def test_state_is_pruned_after_a_run(state):
    cfg = only("dark_pool", min_pct_of_adv=0)
    providers = StubProviders(uw=StubUW(prints=[make_trade(size=50_000, price=100.0)]))
    engine.run(cfg, state, RecordingNotifier(), now=NOW, providers=providers)

    stats = state.stats()
    assert stats["seen"] == 1
    assert stats["runs"] == 1

    # A run far in the future clears the retention window.
    later = NOW + timedelta(days=400)
    engine.run(cfg, state, RecordingNotifier(), now=later, providers=providers, force=True)
    assert state.stats()["seen"] <= 1
