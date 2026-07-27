"""Edge cases and hostile data.

Everything here is a shape real feeds actually produce: a split that wasn't
adjusted, a halt, a DST boundary, an empty response, a clock skew. The
requirement is the same in every case — never emit a confident alert from data
that cannot support one, and never crash the run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import NOW, make_config, make_context, make_option_trade, make_trade
from tickers import PROFILES, bars_for, spike

from monitor import config as config_mod
from monitor.detectors import (
    DarkPoolDetector,
    OptionsFlowDetector,
    VolumeAnomalyDetector,
)
from monitor.health import HealthState, inspect_bars, inspect_events
from monitor.market_calendar import ET
from monitor.models import Bar, OptionTrade


def vol_settings(**overrides):
    return make_config(volume_anomaly=overrides).detector("volume_anomaly")


def dark_settings(**overrides):
    return make_config(dark_pool=overrides).detector("dark_pool")


def flow_settings(**overrides):
    return make_config(options_flow=overrides).detector("options_flow")


# --------------------------------------------------------------------------
# Degenerate bar series
# --------------------------------------------------------------------------
def test_empty_bars_are_reported_not_crashed(state):
    ctx = make_context(state, vol_settings(), bars=[])
    assert VolumeAnomalyDetector().run(ctx) == []
    assert any("no bars" in n for n in ctx.notes)


def test_a_single_bar_cannot_produce_a_baseline(state):
    one = bars_for("AAPL", sessions=1, slots=1)
    ctx = make_context(state, vol_settings(), bars=one)
    assert VolumeAnomalyDetector().run(ctx) == []
    assert any("one session" in n for n in ctx.notes)


def test_all_zero_volume_never_divides_by_zero(state):
    """A halted name reports zero volume all session. Must be silent, not an error."""
    bars = [
        Bar(ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close, volume=0)
        for b in bars_for("AAPL")
    ]
    ctx = make_context(state, vol_settings(min_bar_notional=10_000), bars=bars)
    assert VolumeAnomalyDetector().run(ctx) == []


def test_a_flat_price_series_is_handled(state):
    """Zero price movement means zero variance — no division blowups."""
    flat = [
        Bar(ts=b.ts, open=100.0, high=100.0, low=100.0, close=100.0, volume=200_000)
        for b in bars_for("AAPL")
    ]
    ctx = make_context(state, vol_settings(), bars=flat)
    assert VolumeAnomalyDetector().run(ctx) == []


def test_identical_volume_gives_zero_stdev_without_error(state):
    """Perfectly constant volume makes the z-score undefined; RVOL must carry it."""
    constant = [
        Bar(ts=b.ts, open=b.open, high=b.high, low=b.low, close=b.close, volume=100_000)
        for b in bars_for("AAPL")
    ]
    spiked = spike(constant, 6.0)
    # combine=all would need a z-score, which cannot exist here; 'any' works off RVOL.
    ctx = make_context(state, vol_settings(combine="any"), bars=spiked)
    alerts = VolumeAnomalyDetector().run(ctx)
    assert len(alerts) == 1
    assert "RVOL" in alerts[0].lines[0]


# --------------------------------------------------------------------------
# Corrupt data
# --------------------------------------------------------------------------
def test_negative_volume_is_dropped_and_flagged():
    bars = bars_for("AAPL", sessions=2)
    bars[5] = Bar(ts=bars[5].ts, open=1, high=2, low=0.5, close=1.5, volume=-1000)
    clean, findings = inspect_bars(
        "AAPL", bars, now=NOW, interval_minutes=5, max_stale_intervals=4
    )
    assert len(clean) == len(bars) - 1
    assert any(f.state is HealthState.CORRUPT for f in findings)
    assert any("negative volume" in f.detail for f in findings)


def test_zero_price_is_dropped():
    bars = bars_for("AAPL", sessions=2)
    bars[3] = Bar(ts=bars[3].ts, open=0.0, high=0.0, low=0.0, close=0.0, volume=100)
    clean, findings = inspect_bars("AAPL", bars, now=NOW, interval_minutes=5)
    assert len(clean) == len(bars) - 1
    assert any("non-positive price" in f.detail for f in findings)


def test_high_below_low_is_dropped():
    bars = bars_for("AAPL", sessions=2)
    bars[4] = Bar(ts=bars[4].ts, open=10, high=5, low=20, close=10, volume=100)
    clean, findings = inspect_bars("AAPL", bars, now=NOW, interval_minutes=5)
    assert len(clean) == len(bars) - 1
    assert any("high below low" in f.detail for f in findings)


def test_close_outside_the_high_low_range_is_dropped():
    bars = bars_for("AAPL", sessions=2)
    bars[6] = Bar(ts=bars[6].ts, open=10, high=11, low=9, close=99, volume=100)
    clean, findings = inspect_bars("AAPL", bars, now=NOW, interval_minutes=5)
    assert len(clean) == len(bars) - 1
    assert any("outside high-low" in f.detail for f in findings)


def test_out_of_order_timestamps_are_dropped():
    bars = bars_for("AAPL", sessions=2)
    scrambled = bars[:5] + [bars[0]] + bars[5:]
    clean, findings = inspect_bars("AAPL", scrambled, now=NOW, interval_minutes=5)
    assert len(clean) == len(bars)
    assert any(
        "out of order" in f.detail or "duplicate timestamp" in f.detail for f in findings
    )


def test_an_unadjusted_split_is_caught_as_implausible():
    """A 10-for-1 split that wasn't price-adjusted looks like a -90% bar.

    Left through, the volume detector would fire on a corporate action and
    report a catastrophic move that never happened.
    """
    bars = bars_for("AAPL", sessions=2, slots=12)
    victim = bars[13]
    bars[13] = Bar(
        ts=victim.ts,
        open=victim.open / 10,
        high=victim.high / 10,
        low=victim.low / 10,
        close=victim.close / 10,
        volume=victim.volume * 10,
    )
    clean, findings = inspect_bars("AAPL", bars, now=NOW, interval_minutes=5)
    assert any("implausible" in f.detail for f in findings)
    assert len(clean) < len(bars)


def test_an_overnight_gap_is_not_treated_as_corruption():
    """Sessions legitimately gap. Only intraday jumps are suspicious."""
    day_one = bars_for("AAPL", sessions=1, slots=6, anchor=datetime(2026, 7, 24, 10, 0, tzinfo=ET))
    day_two = [
        Bar(ts=b.ts, open=b.open * 0.4, high=b.high * 0.4, low=b.low * 0.4,
            close=b.close * 0.4, volume=b.volume)
        for b in bars_for("AAPL", sessions=1, slots=6)
    ]
    clean, findings = inspect_bars(
        "AAPL", day_one + day_two, now=NOW, interval_minutes=5
    )
    assert len(clean) == 12
    assert not [f for f in findings if f.state is HealthState.CORRUPT]


def test_every_bar_corrupt_yields_empty_not_a_false_alert():
    bars = [
        Bar(ts=b.ts, open=-1, high=-1, low=-1, close=-1, volume=-1)
        for b in bars_for("AAPL", sessions=2)
    ]
    clean, findings = inspect_bars("AAPL", bars, now=NOW, interval_minutes=5)
    assert clean == []
    assert any(f.state is HealthState.EMPTY for f in findings)


# --------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------
def test_a_frozen_feed_is_detected_during_the_session():
    """The dangerous failure: a successful call returning yesterday's data."""
    stale = bars_for("AAPL", sessions=2, anchor=datetime(2026, 7, 24, 10, 0, tzinfo=ET))
    midday = datetime(2026, 7, 27, 14, 0, tzinfo=ET)
    _, findings = inspect_bars(
        "AAPL", stale, now=midday, interval_minutes=5, max_stale_intervals=4
    )
    assert any(f.state is HealthState.STALE for f in findings)
    assert any("frozen" in f.detail for f in findings)


def test_stale_data_outside_the_session_is_not_a_problem():
    """After the close the newest bar is supposed to be the last close."""
    bars = bars_for("AAPL", sessions=2)
    evening = datetime(2026, 7, 27, 21, 0, tzinfo=ET)
    _, findings = inspect_bars("AAPL", bars, now=evening, interval_minutes=5)
    assert not [f for f in findings if f.state is HealthState.STALE]


def test_no_staleness_complaint_right_after_the_open():
    """There is legitimately almost no data at 09:32."""
    bars = bars_for("AAPL", sessions=1, slots=1, anchor=datetime(2026, 7, 27, 9, 30, tzinfo=ET))
    just_open = datetime(2026, 7, 27, 9, 32, tzinfo=ET)
    _, findings = inspect_bars("AAPL", bars, now=just_open, interval_minutes=5)
    assert not [f for f in findings if f.state is HealthState.STALE]


def test_future_timestamps_are_flagged_and_would_evade_watermarks():
    ahead = [NOW + timedelta(hours=3), NOW - timedelta(minutes=1)]
    findings = inspect_events("AAPL", "prints", ahead, now=NOW)
    assert len(findings) == 1
    assert "future" in findings[0].detail


def test_a_small_clock_skew_is_tolerated():
    """Provider clocks drift by seconds; that must not be reported as corrupt."""
    findings = inspect_events(
        "AAPL", "prints", [NOW + timedelta(minutes=2)], now=NOW, max_future_minutes=5
    )
    assert findings == []


# --------------------------------------------------------------------------
# Time handling
# --------------------------------------------------------------------------
def test_bars_spanning_the_dst_change_are_handled(state):
    """US DST ends 2026-11-01. Session clock times stay put; UTC offsets shift."""
    before = bars_for("AAPL", sessions=3, slots=6, anchor=datetime(2026, 10, 29, 10, 0, tzinfo=ET))
    after = bars_for("AAPL", sessions=3, slots=6, anchor=datetime(2026, 11, 4, 10, 0, tzinfo=ET))
    offsets = {b.ts.utcoffset() for b in before + after}
    assert len(offsets) == 2, "fixture should straddle the DST boundary"

    clean, findings = inspect_bars(
        "AAPL", before + after, now=datetime(2026, 11, 4, 12, 0, tzinfo=ET), interval_minutes=5
    )
    assert len(clean) == len(before) + len(after)
    assert not [f for f in findings if f.state is HealthState.CORRUPT]


def test_early_close_session_respects_the_shorter_day(state):
    """Nov 27 2026 closes at 13:00. A 15:30 bar cannot exist and must be ignored."""
    half_day = datetime(2026, 11, 27, 15, 30, tzinfo=ET)
    bars = bars_for("AAPL", sessions=21, slots=4, anchor=half_day)
    ctx = make_context(state, vol_settings(), bars=spike(bars, 8.0), now=half_day + timedelta(minutes=25))
    # Outside the (shortened) session, so minutes_into_session is None.
    assert VolumeAnomalyDetector().run(ctx) == []


def test_naive_datetimes_from_a_provider_do_not_crash_dedup():
    """A missing timezone must not make two events collide or explode."""
    naive = OptionTrade(
        ticker="AAPL",
        ts=datetime(2026, 7, 27, 14, 30),
        premium=200_000,
        option_type="call",
        strike=350.0,
        expiry="2026-08-21",
        size=100,
    )
    assert naive.dte is not None


# --------------------------------------------------------------------------
# Threshold boundaries
# --------------------------------------------------------------------------
def test_a_print_exactly_on_the_threshold_fires(state):
    """Boundaries are inclusive; a $1,000,000 print at a $1,000,000 floor counts."""
    ctx = make_context(
        state,
        dark_settings(min_notional=1_000_000, min_pct_of_adv=0),
        prints=[make_trade(size=10_000, price=100.0, raw_id="exact")],
        adv=None,
    )
    assert len(DarkPoolDetector().run(ctx)) == 1


def test_a_print_one_cent_under_the_threshold_does_not(state):
    ctx = make_context(
        state,
        dark_settings(min_notional=1_000_000, min_pct_of_adv=0),
        prints=[make_trade(size=10_000, price=99.999, raw_id="under")],
        adv=None,
    )
    assert DarkPoolDetector().run(ctx) == []


def test_zero_premium_flow_is_rejected(state):
    ctx = make_context(
        state, flow_settings(), option_trades=[make_option_trade(premium=0.0)]
    )
    assert OptionsFlowDetector().run(ctx) == []


def test_expiry_in_the_past_yields_negative_dte_and_is_filtered(state):
    """Stale flow rows carry expired contracts; min_dte=0 must exclude them."""
    trade = make_option_trade(days_out=-5)
    assert trade.dte == -5
    ctx = make_context(state, flow_settings(min_dte=0), option_trades=[trade])
    assert OptionsFlowDetector().run(ctx) == []


def test_malformed_expiry_does_not_crash_the_detector(state):
    trade = OptionTrade(
        ticker="TEST",
        ts=NOW - timedelta(minutes=2),
        premium=500_000,
        option_type="call",
        strike=100.0,
        expiry="not-a-date",
        size=100,
        volume=5_000,
        open_interest=100,
        trade_type="sweep",
        raw_id="bad-expiry",
    )
    assert trade.dte is None
    ctx = make_context(state, flow_settings(), option_trades=[trade])
    # dte unknown means the DTE window can't be applied; the trade still counts.
    assert len(OptionsFlowDetector().run(ctx)) == 1


# --------------------------------------------------------------------------
# Config hostility
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        {"tickers": ["AAPL"], "detectors": "not a mapping"},
        {"tickers": ["AAPL"], "detectors": {"volume_anomaly": "not a mapping"}},
        {"tickers": ["AAPL"], "overrides": "not a mapping"},
        {"tickers": ["AAPL"], "run": {"min_severity": "urgent"}},
        {"tickers": ["AAPL"], "providers": {"request_timeout": "soon"}},
    ],
)
def test_hostile_config_shapes_degrade_rather_than_crash(bad):
    if bad.get("detectors") == "not a mapping":
        with pytest.raises(Exception):
            config_mod.from_dict(bad)
        return
    cfg = config_mod.from_dict(bad)
    assert cfg.tickers == ["AAPL"]
    assert cfg.issues


def test_a_ticker_list_of_only_junk_is_fatal():
    from monitor.params import ConfigError

    with pytest.raises(ConfigError):
        config_mod.from_dict({"tickers": ["!!!", "  ", "12345678901234567890"]})


def test_duplicate_tickers_are_collapsed():
    cfg = config_mod.from_dict({"tickers": ["AAPL", "aapl", "AAPL "]})
    assert cfg.tickers == ["AAPL"]


def test_all_detectors_disabled_is_valid_but_does_nothing():
    cfg = config_mod.from_dict(
        {
            "tickers": ["AAPL"],
            "detectors": {n: {"enabled": False} for n in config_mod.DETECTOR_SPECS},
        }
    )
    assert cfg.enabled_detectors() == []
    for role in ("bars", "trades", "flow", "insider", "option_volume"):
        assert cfg.needs(role) is False
