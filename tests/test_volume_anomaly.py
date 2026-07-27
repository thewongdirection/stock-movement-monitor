"""L1 detector tests — the noise-suppression behaviour is the point here."""

from __future__ import annotations

from datetime import timedelta

from conftest import (
    LAST_BAR_ANCHOR,
    NOW,
    build_bars,
    make_config,
    make_context,
    spike_last_bar,
)

from monitor.detectors import VolumeAnomalyDetector
from monitor.models import Bar, Severity


def settings(**overrides):
    return make_config(volume_anomaly=overrides).detector("volume_anomaly")


def test_quiet_market_produces_nothing(state):
    ctx = make_context(state, settings(), bars=build_bars())
    assert VolumeAnomalyDetector().run(ctx) == []


def test_volume_spike_fires(state):
    ctx = make_context(state, settings(), bars=spike_last_bar(build_bars(), 4.0))
    alerts = VolumeAnomalyDetector().run(ctx)
    assert len(alerts) == 1
    assert "Unusual volume" in alerts[0].headline
    assert any("RVOL" in line for line in alerts[0].lines)


def test_bigger_spike_gets_higher_severity(state):
    small = make_context(state, settings(), bars=spike_last_bar(build_bars(), 2.2))
    big = make_context(state, settings(), bars=spike_last_bar(build_bars(), 12.0))
    modest = VolumeAnomalyDetector().run(small)
    severe = VolumeAnomalyDetector().run(big)
    assert modest and severe
    assert severe[0].severity.rank > modest[0].severity.rank


def test_combine_all_needs_both_tests_to_trip(state):
    """A spike that clears RVOL but not the z-score is held back by combine=all.

    Engineered with a noisy baseline: a wide standard deviation keeps the
    z-score low even when the ratio to the mean is high.
    """
    bars = build_bars(base_volume=100_000, jitter=40_000)
    bars = spike_last_bar(bars, 2.1)

    strict = VolumeAnomalyDetector().run(
        make_context(state, settings(combine="all", zscore_threshold=9.0), bars=bars)
    )
    assert strict == []

    loose = VolumeAnomalyDetector().run(
        make_context(state, settings(combine="any", zscore_threshold=9.0), bars=bars)
    )
    assert len(loose) == 1


def test_time_of_day_normalisation_ignores_the_usual_shape(state):
    """The whole point of the detector: a busy slot that is *always* busy is normal.

    Every session's final bar carries 5x the volume of the rest. That is the
    slot's normal level, so it must not alert — a flat daily average would.
    """
    bars = build_bars()
    lifted = [
        Bar(
            ts=b.ts,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume * 5,
        )
        if b.ts.time() == LAST_BAR_ANCHOR.time().replace(minute=55)
        else b
        for b in bars
    ]
    ctx = make_context(state, settings(), bars=lifted)
    assert VolumeAnomalyDetector().run(ctx) == []


def test_warmup_skips_opening_bars(state):
    """A spike inside the warmup window is ignored; the same spike later is not."""
    anchor = NOW.replace(hour=9, minute=30)
    bars = spike_last_bar(build_bars(slots=2, last_day=anchor), 8.0)
    now = anchor + timedelta(minutes=11)

    muted = VolumeAnomalyDetector().run(
        make_context(state, settings(warmup_minutes=30), bars=bars, now=now)
    )
    assert muted == []

    audible = VolumeAnomalyDetector().run(
        make_context(state, settings(warmup_minutes=0), bars=bars, now=now)
    )
    assert len(audible) == 1


def test_thin_bars_are_filtered_by_notional(state):
    bars = spike_last_bar(build_bars(base_volume=100, price=1.0), 5.0)
    ctx = make_context(state, settings(min_bar_notional=1_000_000), bars=bars)
    assert VolumeAnomalyDetector().run(ctx) == []


def test_price_confirmation_can_be_required(state):
    """The synthetic spike moves price ~2.5%, so a 5% requirement blocks it."""
    bars = spike_last_bar(build_bars(), 5.0)
    assert (
        VolumeAnomalyDetector().run(
            make_context(state, settings(min_price_move_pct=5.0), bars=bars)
        )
        == []
    )
    assert (
        len(
            VolumeAnomalyDetector().run(
                make_context(state, settings(min_price_move_pct=1.0), bars=bars)
            )
        )
        == 1
    )


def test_incomplete_final_bar_is_not_judged(state):
    """A partially formed bar looks quiet and must be left alone."""
    bars = spike_last_bar(build_bars(), 6.0)
    # `now` lands one minute into the final 5-minute bar.
    ctx = make_context(state, settings(), bars=bars, now=bars[-1].ts + timedelta(minutes=1))
    assert VolumeAnomalyDetector().run(ctx) == []


def test_cooldown_suppresses_a_second_fire(state):
    bars = spike_last_bar(build_bars(), 6.0)
    first = make_context(state, settings(cooldown_minutes=60), bars=bars)
    assert len(VolumeAnomalyDetector().run(first)) == 1
    state.flush_progress()

    # A fresh spike five minutes later, still inside the cooldown.
    later_bars = bars + [
        Bar(
            ts=bars[-1].ts + timedelta(minutes=5),
            open=100.0,
            high=103.0,
            low=99.9,
            close=102.5,
            volume=bars[-1].volume,
        )
    ]
    second = make_context(
        state,
        settings(cooldown_minutes=60),
        bars=later_bars,
        now=NOW + timedelta(minutes=5),
    )
    assert VolumeAnomalyDetector().run(second) == []


def test_watermark_prevents_realerting_the_same_bar(state):
    bars = spike_last_bar(build_bars(), 6.0)
    assert len(VolumeAnomalyDetector().run(make_context(state, settings(cooldown_minutes=0), bars=bars))) == 1
    state.flush_progress()
    assert VolumeAnomalyDetector().run(make_context(state, settings(cooldown_minutes=0), bars=bars)) == []


def test_insufficient_baseline_is_skipped_not_guessed(state):
    ctx = make_context(state, settings(), bars=spike_last_bar(build_bars(sessions=3), 8.0))
    assert VolumeAnomalyDetector().run(ctx) == []


def test_no_bars_notes_the_gap(state):
    ctx = make_context(state, settings(), bars=[])
    assert VolumeAnomalyDetector().run(ctx) == []
    assert any("no bars" in note for note in ctx.notes)


def test_alert_states_that_direction_is_a_proxy(state):
    ctx = make_context(state, settings(), bars=spike_last_bar(build_bars(), 6.0))
    alerts = VolumeAnomalyDetector().run(ctx)
    joined = " ".join(alerts[0].lines)
    assert "proxy for side" in joined
    assert alerts[0].severity in {Severity.LOW, Severity.MEDIUM, Severity.HIGH}
