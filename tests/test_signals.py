"""The four detectors, and the baseline they share."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from monitor.clock import ET
from monitor.models import OptionChain, OptionContract, Severity, Side
from monitor.signals import (BlockSignal, Context, InsiderSignal, OpenInterestSignal,
                             VolumeSignal, build_baseline, registry)

from conftest import NOW, make_bars, make_chain, make_filing, make_trade, spike


def ctx(config, store, **kwargs):
    defaults = dict(ticker="NVDA", now=NOW, config=config, store=store)
    defaults.update(kwargs)
    return Context(**defaults)


class TestBaseline:
    def test_the_baseline_uses_the_same_slot_on_other_days(self, config):
        bars = make_bars(sessions=6, slot_volumes={"11:00": 1_000_000}, base_volume=200_000)
        target = next(b for b in bars if b.ts.hour == 11 and b.ts.minute == 0)
        baseline = build_baseline(bars, target, 30, min_samples=3)
        assert baseline.slot == "11:00"
        assert baseline.median == 1_000_000        # not the 200k of other slots

    def test_the_target_day_is_excluded_from_its_own_baseline(self, config):
        bars = make_bars(sessions=6)
        target = bars[-1]
        baseline = build_baseline(bars, target, 30, min_samples=3)
        same_day = [b for b in bars if b.ts.date() == target.ts.date()
                    and b.ts.hour == target.ts.hour]
        assert baseline.samples == len({b.ts.date() for b in bars}) - 1
        assert same_day

    def test_too_few_samples_returns_nothing_rather_than_a_guess(self):
        bars = make_bars(sessions=2)
        assert build_baseline(bars, bars[-1], 30, min_samples=5) is None

    def test_a_flat_history_gives_no_zscore_opinion(self):
        bars = make_bars(sessions=6, base_volume=1_000_000)
        baseline = build_baseline(bars, bars[-1], 30, min_samples=3)
        assert baseline.stdev == 0
        assert baseline.zscore(2_000_000) is None

    def test_rvol_is_none_when_the_median_is_zero(self):
        bars = make_bars(sessions=6, base_volume=0)
        baseline = build_baseline(bars, bars[-1], 30, min_samples=3)
        assert baseline.rvol(1000) is None


class TestVolumeSignal:
    def _bars(self, volume: int, close: float = 200.0):
        bars = make_bars(sessions=8, base_volume=1_000_000)
        target = max(b.ts for b in bars)
        return spike(bars, at=target, volume=volume, close=close), target

    def test_an_ordinary_bar_says_nothing(self, config, store):
        bars, _ = self._bars(1_000_000)
        assert VolumeSignal().evaluate(ctx(config, store, bars=bars)) == []

    def test_a_heavy_bar_with_a_move_fires(self, config, store):
        bars, target = self._bars(4_000_000, close=204.0)
        alerts = VolumeSignal().evaluate(ctx(config, store, bars=bars))
        assert len(alerts) == 1
        assert alerts[0].occurred_at == target
        assert alerts[0].signal == "volume"

    def test_heavy_volume_with_no_move_is_filtered_under_combine_all(self, config, store):
        bars, _ = self._bars(4_000_000, close=200.0)
        assert VolumeSignal().evaluate(ctx(config, store, bars=bars)) == []

    def test_combine_any_lets_a_single_criterion_through_at_low_severity(self, config, store):
        config.values["signals.volume.combine"] = "any"
        bars, _ = self._bars(1_000_000, close=204.0)     # move only, volume normal
        alerts = VolumeSignal().evaluate(ctx(config, store, bars=bars))
        assert alerts and alerts[0].severity is Severity.LOW

    def test_a_very_heavy_bar_escalates_to_high(self, config, store):
        bars, _ = self._bars(9_000_000, close=204.0)
        alerts = VolumeSignal().evaluate(ctx(config, store, bars=bars))
        assert alerts[0].severity is Severity.HIGH

    def test_a_bar_below_the_notional_floor_is_ignored(self, config, store):
        config.values["signals.volume.min_notional"] = 10_000_000_000
        bars, _ = self._bars(9_000_000, close=204.0)
        assert VolumeSignal().evaluate(ctx(config, store, bars=bars)) == []

    def test_a_still_forming_bar_is_not_evaluated(self, config, store):
        """Half a bar's volume would understate RVOL, then contradict itself later."""
        bars, target = self._bars(9_000_000, close=204.0)
        early = ctx(config, store, bars=bars, now=target + timedelta(minutes=10))
        assert VolumeSignal().evaluate(early) == []

    def test_an_old_bar_is_not_reported_as_news(self, config, store):
        bars, target = self._bars(9_000_000, close=204.0)
        late = ctx(config, store, bars=bars, now=target + timedelta(hours=6))
        assert VolumeSignal().evaluate(late) == []

    def test_a_ticker_override_changes_what_fires(self, config, store):
        bars, _ = self._bars(1_800_000, close=204.0)     # 1.8x — under the 2.0 default
        assert VolumeSignal().evaluate(ctx(config, store, bars=bars)) == []
        config.overrides["NVDA"] = {"signals.volume.rvol_threshold": 1.5}
        assert VolumeSignal().evaluate(ctx(config, store, bars=bars))

    def test_the_alert_carries_the_numbers_it_claims(self, config, store):
        bars, _ = self._bars(4_000_000, close=204.0)
        alert = VolumeSignal().evaluate(ctx(config, store, bars=bars))[0]
        assert any("4M" in fact for fact in alert.facts)
        assert any("Baseline from" in fact for fact in alert.facts)
        assert alert.read and alert.caveats

    def test_no_bars_is_silence_not_a_crash(self, config, store):
        assert VolumeSignal().evaluate(ctx(config, store, bars=[])) == []

    def test_a_short_history_produces_no_alert(self, config, store):
        bars = make_bars(sessions=2, base_volume=1_000_000)
        bars = spike(bars, at=max(b.ts for b in bars), volume=9_000_000, close=204.0)
        assert VolumeSignal().evaluate(ctx(config, store, bars=bars)) == []


class TestOpenInterestSignal:
    def test_a_first_snapshot_says_nothing(self, config, store):
        chain = make_chain(previous={})
        assert OpenInterestSignal().evaluate(ctx(config, store, chain=chain)) == []

    def test_a_large_build_fires(self, config, store):
        chain = make_chain(previous={"NVDA|2026-08-21|200|call": 34_051,
                                     "NVDA|2026-08-21|195|put": 16_000})
        alerts = OpenInterestSignal().evaluate(ctx(config, store, chain=chain))
        assert len(alerts) == 1
        assert "call" in alerts[0].headline and "opened" in alerts[0].headline

    def test_a_small_build_is_below_the_floor(self, config, store):
        chain = make_chain(contracts=[(200.0, "call", 34_400)],
                           previous={"NVDA|2026-08-21|200|call": 34_051})
        assert OpenInterestSignal().evaluate(ctx(config, store, chain=chain)) == []

    def test_a_build_that_is_large_but_proportionally_tiny_is_filtered(self, config, store):
        """+600 on 500,000 open contracts is noise, not positioning."""
        chain = make_chain(contracts=[(200.0, "call", 500_600)],
                           previous={"NVDA|2026-08-21|200|call": 500_000})
        assert OpenInterestSignal().evaluate(ctx(config, store, chain=chain)) == []

    def test_positions_being_closed_are_reported_too(self, config, store):
        chain = make_chain(contracts=[(200.0, "call", 10_000)],
                           previous={"NVDA|2026-08-21|200|call": 34_051})
        alerts = OpenInterestSignal().evaluate(ctx(config, store, chain=chain))
        assert alerts and "closed" in alerts[0].headline
        assert "unwound" in alerts[0].read or "closed out" in alerts[0].read

    def test_a_distant_expiry_is_excluded(self, config, store):
        far = OptionContract(ticker="NVDA", expiry="2028-01-21", strike=200.0,
                             right="call", open_interest=52_400, as_of="2026-07-24")
        chain = OptionChain("NVDA", "2026-07-24", [far],
                            {"NVDA|2028-01-21|200|call": 34_051})
        assert OpenInterestSignal().evaluate(ctx(config, store, chain=chain)) == []

    def test_the_net_call_and_put_change_is_reported(self, config, store):
        chain = make_chain(previous={"NVDA|2026-08-21|200|call": 34_051,
                                     "NVDA|2026-08-21|195|put": 20_000})
        alert = OpenInterestSignal().evaluate(ctx(config, store, chain=chain))[0]
        assert any("Net across the chain" in fact for fact in alert.facts)

    def test_only_the_top_n_contracts_are_listed(self, config, store):
        config.values["signals.open_interest.top_n"] = 2
        rows = [(190.0 + i, "call", 40_000) for i in range(6)]
        previous = {f"NVDA|2026-08-21|{190 + i:g}|call": 10_000 for i in range(6)}
        chain = make_chain(contracts=rows, previous=previous)
        alert = OpenInterestSignal().evaluate(ctx(config, store, chain=chain))[0]
        listed = [f for f in alert.facts if f.startswith("Opened")]
        assert len(listed) == 2
        assert any("further contracts" in f for f in alert.facts)

    def test_dedup_identity_changes_with_the_snapshot_date(self, config, store):
        previous = {"NVDA|2026-08-21|200|call": 34_051, "NVDA|2026-08-21|195|put": 16_000}
        one = OpenInterestSignal().evaluate(
            ctx(config, store, chain=make_chain(as_of="2026-07-24", previous=previous)))[0]
        two = OpenInterestSignal().evaluate(
            ctx(config, store, chain=make_chain(as_of="2026-07-25", previous=previous)))[0]
        assert one.dedup_key != two.dedup_key

    def test_severity_needs_real_scale_to_reach_high(self, config, store):
        # +800 on 3,000 existing contracts: past every floor, but ordinary.
        modest = make_chain(contracts=[(200.0, "call", 3_800)],
                            previous={"NVDA|2026-08-21|200|call": 3_000})
        assert OpenInterestSignal().evaluate(
            ctx(config, store, chain=modest))[0].severity is Severity.MEDIUM

        # +54,000 contracts overnight is not an ordinary day.
        huge = make_chain(contracts=[(200.0, "call", 60_000)],
                          previous={"NVDA|2026-08-21|200|call": 6_000})
        assert OpenInterestSignal().evaluate(
            ctx(config, store, chain=huge))[0].severity is Severity.HIGH


class TestInsiderSignal:
    def test_a_purchase_fires(self, config, store):
        alerts = InsiderSignal().evaluate(ctx(config, store, filings=[make_filing()]))
        assert len(alerts) == 1
        assert alerts[0].severity is Severity.HIGH        # CFO, so senior

    def test_a_small_trade_is_below_the_floor(self, config, store):
        tiny = make_filing(shares=10, price=205.44)
        assert InsiderSignal().evaluate(ctx(config, store, filings=[tiny])) == []

    def test_a_planned_sale_is_suppressed_by_default(self, config, store):
        sale = make_filing(code="S", planned_10b5_1=True, title="Director",
                           is_officer=False, is_director=True)
        assert InsiderSignal().evaluate(ctx(config, store, filings=[sale])) == []

    def test_a_planned_sale_can_be_included_on_request(self, config, store):
        config.values["signals.insider.include_planned_sales"] = True
        sale = make_filing(code="S", planned_10b5_1=True)
        alerts = InsiderSignal().evaluate(ctx(config, store, filings=[sale]))
        assert alerts and alerts[0].severity is Severity.LOW
        assert "no action implied" in alerts[0].read

    def test_a_discretionary_sale_is_reported(self, config, store):
        sale = make_filing(code="S", planned_10b5_1=False)
        alerts = InsiderSignal().evaluate(ctx(config, store, filings=[sale]))
        assert alerts and "sold" in alerts[0].headline

    def test_two_buyers_make_a_cluster(self, config, store):
        one = make_filing(insider="DOE JANE", traded_on="2026-07-21")
        two = make_filing(insider="SMITH ALAN", title="Director", shares=4_200,
                          price=203.10, traded_on="2026-07-22",
                          accession="acc-2", is_officer=False, is_director=True)
        alerts = InsiderSignal().evaluate(ctx(config, store, filings=[one, two]))
        clusters = [a for a in alerts if "cluster" in a.headline]
        assert len(clusters) == 1
        assert clusters[0].severity is Severity.HIGH
        assert clusters[0] is alerts[0], "the cluster should lead"

    def test_one_buyer_is_not_a_cluster(self, config, store):
        alerts = InsiderSignal().evaluate(ctx(config, store, filings=[make_filing()]))
        assert not any("cluster" in a.headline for a in alerts)

    def test_a_cluster_spanning_polls_is_still_detected(self, config, store):
        """Neither poll can see both buys; the store is what joins them."""
        first = ctx(config, store, filings=[make_filing(insider="DOE JANE")])
        InsiderSignal().evaluate(first)

        later = make_filing(insider="SMITH ALAN", accession="acc-2",
                            traded_on="2026-07-22", is_officer=False, is_director=True)
        second = ctx(config, store, filings=[later])
        alerts = InsiderSignal().evaluate(second)
        assert any("cluster" in a.headline for a in alerts)

    def test_the_cluster_identity_grows_with_the_buyer_set(self, config, store):
        two = [make_filing(insider="A", accession="a"),
               make_filing(insider="B", accession="b")]
        first = [a for a in InsiderSignal().evaluate(ctx(config, store, filings=two))
                 if "cluster" in a.headline][0]
        three = two + [make_filing(insider="C", accession="c")]
        second = [a for a in InsiderSignal().evaluate(ctx(config, store, filings=three))
                  if "cluster" in a.headline][0]
        assert first.dedup_key != second.dedup_key

    def test_the_purchase_share_is_measured_against_the_new_position(self, config, store):
        alert = InsiderSignal().evaluate(ctx(config, store, filings=[make_filing()]))[0]
        assert any("this trade was 8% of it" in fact for fact in alert.facts)

    def test_a_sale_is_measured_against_the_prior_position(self, config, store):
        sale = make_filing(code="S", shares=8_000, shares_after=42_000, price=393.47)
        alert = InsiderSignal().evaluate(ctx(config, store, filings=[sale]))[0]
        assert any("sold 16% of the prior position" in fact for fact in alert.facts)


class TestBlockSignal:
    def test_a_large_print_fires(self, config, store):
        alerts = BlockSignal().evaluate(ctx(config, store, trades=[make_trade()]))
        assert len(alerts) == 1
        assert alerts[0].severity is Severity.MEDIUM       # institutional size

    def test_a_small_print_is_ignored(self, config, store):
        small = make_trade(size=100, price=205.0)
        assert BlockSignal().evaluate(ctx(config, store, trades=[small])) == []

    def test_a_small_share_count_at_a_high_price_still_qualifies(self, config, store):
        """10,000 shares of a $4 stock and 500 of a $900 stock are both blocks."""
        pricey = make_trade(size=500, price=900.0)
        assert BlockSignal().evaluate(ctx(config, store, trades=[pricey]))

    def test_a_mega_block_escalates(self, config, store):
        mega = make_trade(size=150_000, price=205.0)
        assert BlockSignal().evaluate(
            ctx(config, store, trades=[mega]))[0].severity is Severity.HIGH

    def test_off_exchange_only_filters_lit_prints(self, config, store):
        config.values["signals.blocks.off_exchange_only"] = True
        assert BlockSignal().evaluate(ctx(config, store, trades=[make_trade()])) == []
        dark = make_trade(off_exchange=True)
        assert BlockSignal().evaluate(ctx(config, store, trades=[dark]))

    def test_unknown_side_is_stated_rather_than_guessed(self, config, store):
        alert = BlockSignal().evaluate(ctx(config, store, trades=[make_trade()]))[0]
        assert any("no aggressor flag" in fact for fact in alert.facts)
        assert "does not say which side" in alert.read

    def test_an_inferred_side_is_labelled_as_an_inference(self, config, store):
        buy = make_trade(side=Side.BUY)
        alert = BlockSignal().evaluate(ctx(config, store, trades=[buy]))[0]
        assert "inference" in alert.read

    def test_the_report_is_capped_and_takes_the_largest(self, config, store):
        config.values["signals.blocks.max_per_ticker"] = 2
        trades = [make_trade(size=10_000 + n * 5_000, ref=f"t{n}") for n in range(6)]
        alerts = BlockSignal().evaluate(ctx(config, store, trades=trades))
        assert len(alerts) == 2
        assert alerts[0].dedup_key != alerts[1].dedup_key

    def test_an_old_print_is_not_reported(self, config, store):
        stale = make_trade(ts=NOW - timedelta(hours=8))
        assert BlockSignal().evaluate(ctx(config, store, trades=[stale])) == []


class TestRegistry:
    def test_only_enabled_signals_are_returned(self, config):
        config.values["signals.blocks.enabled"] = False
        config.values["signals.volume.enabled"] = False
        names = [signal.name for signal in registry(config)]
        assert names == ["open_interest", "insider"]

    def test_open_interest_leads_because_it_proves_the_most(self, config):
        config.values["signals.blocks.enabled"] = True
        assert [s.name for s in registry(config)][0] == "open_interest"
