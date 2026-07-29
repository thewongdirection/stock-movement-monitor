"""Orchestration: fetch cadence, failure isolation, dedup and capping."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from monitor.clock import ET
from monitor.engine import Engine
from monitor.models import Severity
from monitor.sources import SourceSet
from monitor.sources.base import EmptyResponse, Unreachable

from conftest import NOW, make_bars, make_chain, make_filing, spike


class FakeBars:
    name = "fake-bars"

    def __init__(self, bars=None, error=None):
        self._bars = bars if bars is not None else []
        self._error = error
        self.calls = 0

    def bars(self, ticker, minutes, sessions):
        self.calls += 1
        if self._error:
            raise self._error
        return self._bars


class FakeOptions:
    name = "fake-options"

    def __init__(self, chain=None, error=None):
        self._chain = chain
        self._error = error
        self.calls = 0

    def chain(self, ticker, max_days):
        self.calls += 1
        if self._error:
            raise self._error
        return self._chain


class FakeInsider:
    name = "fake-insider"

    def __init__(self, filings=None, error=None):
        self._filings = filings or []
        self._error = error

    def filings(self, ticker, since):
        if self._error:
            raise self._error
        return self._filings


def hot_bars():
    bars = make_bars(sessions=8, base_volume=1_000_000)
    return spike(bars, at=max(b.ts for b in bars), volume=6_000_000, close=204.0)


def engine(config, store, **sources):
    config.values["watchlist"] = ["NVDA"]
    config.values["canslim.enabled"] = False
    return Engine(config, store, SourceSet(**sources), now=NOW)


class TestBasicRun:
    def test_a_quiet_market_produces_nothing_and_stays_healthy(self, config, store):
        result = engine(config, store, bars=FakeBars(make_bars(sessions=8))).run()
        assert result.alerts == [] and result.ok and result.scanned == 1

    def test_a_volume_anomaly_reaches_the_result(self, config, store):
        result = engine(config, store, bars=FakeBars(hot_bars())).run()
        assert len(result.alerts) == 1
        assert result.alerts[0].signal == "volume"

    def test_every_watched_ticker_is_scanned(self, config, store):
        config.values["watchlist"] = ["NVDA", "MSFT", "AAPL"]
        result = Engine(config, store, SourceSet(bars=FakeBars(make_bars(sessions=8))),
                        now=NOW).run()
        assert result.scanned == 3

    def test_an_explicit_ticker_list_overrides_the_watchlist(self, config, store):
        result = engine(config, store, bars=FakeBars(hot_bars())).run(["TSLA"])
        assert result.scanned == 1
        assert result.alerts[0].ticker == "TSLA"


class TestFailureIsolation:
    def test_an_unreachable_source_becomes_a_visible_issue(self, config, store):
        source = FakeBars(error=Unreachable("fake-bars", "connection refused", "NVDA"))
        result = engine(config, store, bars=source).run()
        assert not result.ok
        assert result.health.issues[0].kind == "unreachable"

    def test_one_broken_source_does_not_stop_the_others(self, config, store):
        result = engine(
            config, store,
            bars=FakeBars(error=Unreachable("fake-bars", "down", "NVDA")),
            insider=FakeInsider([make_filing()]),
        ).run()
        assert any(a.signal == "insider" for a in result.alerts)
        assert not result.ok

    def test_construction_issues_are_carried_into_the_result(self, config, store):
        from monitor.models import SourceIssue
        sources = SourceSet(bars=FakeBars(make_bars(sessions=8)))
        sources.issues.append(SourceIssue("sec", "*", "unconfigured", "no user agent"))
        result = Engine(config, store, sources, now=NOW).run()
        assert any(i.source == "sec" for i in result.health.issues)

    def test_a_signal_that_raises_is_contained(self, config, store, monkeypatch):
        from monitor.signals import volume as volume_module

        def explode(self, ctx):
            raise RuntimeError("boom")

        monkeypatch.setattr(volume_module.VolumeSignal, "evaluate", explode)
        result = engine(config, store,
                        bars=FakeBars(hot_bars()),
                        insider=FakeInsider([make_filing()])).run()
        assert any(a.signal == "insider" for a in result.alerts)
        assert any("volume" in note for note in result.health.notes)


class TestOpenInterestCadence:
    def _chain(self):
        return make_chain(previous={"NVDA|2026-08-21|200|call": 34_051,
                                    "NVDA|2026-08-21|195|put": 16_000})

    def test_the_chain_is_fetched_once_a_day(self, config, store):
        options = FakeOptions(self._chain())
        made = engine(config, store, bars=FakeBars(make_bars(sessions=8)), options=options)
        made.run()
        made.run()
        assert options.calls == 1

    def test_a_new_day_fetches_again(self, config, store):
        options = FakeOptions(self._chain())
        config.values["watchlist"] = ["NVDA"]
        config.values["canslim.enabled"] = False
        sources = SourceSet(bars=FakeBars(make_bars(sessions=8)), options=options)
        Engine(config, store, sources, now=NOW).run()
        Engine(config, store, sources, now=NOW + timedelta(days=1)).run()
        assert options.calls == 2

    def test_a_first_snapshot_is_stored_and_noted_rather_than_alerted(self, config, store):
        chain = make_chain(previous={})
        result = engine(config, store, bars=FakeBars(make_bars(sessions=8)),
                        options=FakeOptions(chain)).run()
        assert not any(a.signal == "open_interest" for a in result.alerts)
        assert any("first option snapshot" in n for n in result.health.notes)
        assert store.oi_snapshot_dates("NVDA") == [chain.as_of]

    def test_the_store_supplies_the_baseline_for_a_live_source(self, config, store):
        store.save_oi_snapshot("NVDA", "2026-07-23", make_chain(
            contracts=[(200.0, "call", 34_051), (195.0, "put", 16_000)]).contracts)
        fresh = make_chain(previous={})
        result = engine(config, store, bars=FakeBars(make_bars(sessions=8)),
                        options=FakeOptions(fresh)).run()
        assert any(a.signal == "open_interest" for a in result.alerts)

    def test_a_stale_baseline_is_flagged_as_cumulative(self, config, store):
        store.save_oi_snapshot("NVDA", "2026-07-01", make_chain(
            contracts=[(200.0, "call", 34_051), (195.0, "put", 16_000)]).contracts)
        result = engine(config, store, bars=FakeBars(make_bars(sessions=8)),
                        options=FakeOptions(make_chain(previous={}))).run()
        assert any("cumulative" in note for note in result.health.notes)

    def test_a_failed_chain_fetch_does_not_burn_the_daily_marker(self, config, store):
        options = FakeOptions(error=EmptyResponse("fake-options", "no contracts", "NVDA"))
        made = engine(config, store, bars=FakeBars(make_bars(sessions=8)), options=options)
        made.run()
        made.run()
        assert options.calls == 2, "a failure must be retried, not treated as done"


class TestFiltering:
    def test_an_already_sent_alert_is_dropped(self, config, store):
        made = engine(config, store, bars=FakeBars(hot_bars()))
        first = made.run()
        for alert in first.alerts:
            store.mark_sent(alert)
        second = made.run()
        assert second.alerts == [] and second.duplicates == 1

    def test_alerts_below_the_minimum_severity_are_dropped(self, config, store):
        config.values["notify.min_severity"] = "high"
        config.values["signals.volume.combine"] = "any"
        bars = spike(make_bars(sessions=8, base_volume=1_000_000),
                     at=max(b.ts for b in make_bars(sessions=8)), volume=1_000_000, close=204.0)
        result = engine(config, store, bars=FakeBars(bars)).run()
        assert all(a.severity is Severity.HIGH for a in result.alerts)

    def test_the_cap_keeps_the_most_severe_and_reports_the_rest(self, config, store):
        config.values["notify.max_per_run"] = 2
        config.values["watchlist"] = ["NVDA", "MSFT", "AAPL", "TSLA"]
        config.values["canslim.enabled"] = False
        sources = SourceSet(bars=FakeBars(hot_bars()))
        result = Engine(config, store, sources, now=NOW).run()
        assert len(result.alerts) == 2
        assert result.capped == 2
        assert result.generated == 4

    def test_nothing_is_marked_sent_by_the_engine(self, config, store):
        """Marking is the CLI's job, after delivery succeeds."""
        result = engine(config, store, bars=FakeBars(hot_bars())).run()
        assert result.alerts
        assert not store.already_sent(result.alerts[0].dedup_key)


class TestSessionAwareness:
    def test_a_closed_market_is_noted_not_treated_as_a_fault(self, config, store):
        config.values["watchlist"] = ["NVDA"]
        config.values["canslim.enabled"] = False
        sunday = datetime(2026, 7, 26, 12, 0, tzinfo=ET)
        result = Engine(config, store, SourceSet(bars=FakeBars(make_bars(sessions=8))),
                        now=sunday).run()
        assert result.ok
        assert any("not a trading day" in note for note in result.health.notes)

    def test_all_signals_disabled_produces_a_note(self, config, store):
        for name in ("volume", "blocks", "open_interest", "insider"):
            config.values[f"signals.{name}.enabled"] = False
        result = engine(config, store, bars=FakeBars(hot_bars())).run()
        assert result.alerts == []
        assert any("disabled" in note for note in result.health.notes)


class TestCanSlimAttachment:
    class FakeCanSlim:
        enabled = True

        def __init__(self, line="CAN SLIM B+ (72%) — WATCH"):
            self._line = line
            self.calls = []

        def should_attach(self, rank):
            return rank >= Severity.MEDIUM.rank

        def line(self, ticker, fresh=False):
            self.calls.append(ticker)
            return self._line

    def test_a_grade_is_attached_once_per_ticker(self, config, store):
        config.values["watchlist"] = ["NVDA"]
        grader = self.FakeCanSlim()
        made = Engine(config, store, SourceSet(bars=FakeBars(hot_bars())),
                      canslim=grader, now=NOW)
        result = made.run()
        assert result.canslim["NVDA"].startswith("CAN SLIM")
        assert grader.calls == ["NVDA"]

    def test_a_grading_failure_does_not_lose_the_alert(self, config, store):
        class Broken(self.FakeCanSlim):
            def line(self, ticker, fresh=False):
                raise RuntimeError("fundamentals exploded")

        config.values["watchlist"] = ["NVDA"]
        result = Engine(config, store, SourceSet(bars=FakeBars(hot_bars())),
                        canslim=Broken(), now=NOW).run()
        assert result.alerts and result.canslim == {}
