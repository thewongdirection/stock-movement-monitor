"""The same thresholds across ten very different names.

Detection logic tuned on one synthetic mega-cap will misbehave on a $3 stock or
an illiquid micro-cap. These tests assert the behaviour that should hold
*regardless* of price scale and liquidity, and pin the places where it
deliberately differs.
"""

from __future__ import annotations

import pytest
from conftest import NOW, make_config, make_context, make_trade
from tickers import PROFILES, bars_for, spike

from monitor.detectors import (
    BlockTradeDetector,
    DarkPoolDetector,
    VolumeAnomalyDetector,
)

LIQUID = ["AAPL", "MSFT", "TSLA", "NVDA", "CRWD", "PLTR", "SMCI", "PENNY"]
ALL = list(PROFILES)


def vol_settings(**overrides):
    return make_config(volume_anomaly=overrides).detector("volume_anomaly")


def dark_settings(**overrides):
    return make_config(dark_pool=overrides).detector("dark_pool")


def block_settings(**overrides):
    return make_config(block_trades=overrides).detector("block_trades")


def ctx_for(state, symbol, settings, bars=None, **kwargs):
    ctx = make_context(state, settings, **kwargs)
    object.__setattr__(ctx, "ticker", symbol)
    ctx.bars = bars if bars is not None else bars_for(symbol)
    return ctx


# --------------------------------------------------------------------------
# Volume anomaly across the board
# --------------------------------------------------------------------------
@pytest.mark.parametrize("symbol", ALL)
def test_a_quiet_session_is_silent_in_every_name(state, symbol):
    """No spike, no alert — whatever the price or liquidity."""
    ctx = ctx_for(state, symbol, vol_settings(), bars_for(symbol))
    assert VolumeAnomalyDetector().run(ctx) == []


@pytest.mark.parametrize("symbol", LIQUID)
def test_a_big_spike_fires_in_every_liquid_name(state, symbol):
    """A 10x spike must be caught regardless of the name's normal variance.

    TSLA and NVDA have wide natural volume swings, so `combine: all` (RVOL and
    z-score together) is the real test here — a noisy baseline suppresses the
    z-score leg.
    """
    ctx = ctx_for(state, symbol, vol_settings(), spike(bars_for(symbol), 10.0))
    alerts = VolumeAnomalyDetector().run(ctx)
    assert len(alerts) == 1, f"{symbol} missed a 10x volume spike"
    assert alerts[0].ticker == symbol


def test_the_illiquid_name_stays_quiet_even_on_a_spike(state):
    """THIN trades ~$10k a bar. A 10x spike is still below min_bar_notional.

    This is intentional: a $100k bar in a micro-cap is not information worth a
    push notification, and letting it through would make the monitor unusable.
    """
    ctx = ctx_for(state, "THIN", vol_settings(), spike(bars_for("THIN"), 10.0))
    assert VolumeAnomalyDetector().run(ctx) == []


def test_lowering_the_notional_floor_lets_the_illiquid_name_through(state):
    """The suppression above must be the threshold, not a bug in the detector."""
    ctx = ctx_for(
        state, "THIN", vol_settings(min_bar_notional=10_000), spike(bars_for("THIN"), 10.0)
    )
    assert len(VolumeAnomalyDetector().run(ctx)) == 1


@pytest.mark.parametrize("symbol", ["TSLA", "NVDA", "SMCI", "PENNY"])
def test_erratic_names_need_a_bigger_spike_than_steady_ones(state, symbol):
    """A name whose volume naturally swings should not fire on a mild bump.

    This is the z-score leg doing its job: with a wide baseline standard
    deviation, a 2x bar is unremarkable.
    """
    ctx = ctx_for(state, symbol, vol_settings(), spike(bars_for(symbol), 1.8))
    assert VolumeAnomalyDetector().run(ctx) == []


def test_per_ticker_override_raises_the_bar_for_one_name_only(state):
    """TSLA is habitually busy, so its threshold should be liftable alone."""
    from monitor import config as config_mod

    cfg = config_mod.from_dict(
        {
            "tickers": ["TSLA", "MSFT"],
            "detectors": {"volume_anomaly": {"enabled": True}},
            "overrides": {"TSLA": {"volume_anomaly": {"rvol_threshold": 15.0}}},
        }
    )
    tsla_bars = spike(bars_for("TSLA"), 6.0)
    msft_bars = spike(bars_for("MSFT"), 6.0)

    tsla = ctx_for(state, "TSLA", cfg.detector("volume_anomaly", "TSLA"), tsla_bars)
    msft = ctx_for(state, "MSFT", cfg.detector("volume_anomaly", "MSFT"), msft_bars)

    assert VolumeAnomalyDetector().run(tsla) == []
    assert len(VolumeAnomalyDetector().run(msft)) == 1


# --------------------------------------------------------------------------
# Where share counts and dollars disagree
# --------------------------------------------------------------------------
def test_low_priced_stock_exposes_the_share_count_trap(state):
    """50,000 shares of a $3.70 stock is $185k — a block by shares, not by value.

    The classic 10,000-share block definition was written when share prices were
    different. This is exactly why `combine` exists.
    """
    print_ = make_trade(size=50_000, price=PROFILES["PENNY"].price, raw_id="p1")

    any_mode = BlockTradeDetector().run(
        ctx_for(
            state,
            "PENNY",
            block_settings(preset="classic", combine="any", min_pct_of_adv=0),
            prints=[print_],
            adv=PROFILES["PENNY"].adv,
        )
    )
    assert len(any_mode) == 1  # clears the 10k share floor

    all_mode = BlockTradeDetector().run(
        ctx_for(
            state,
            "PENNY",
            block_settings(preset="institutional", combine="all", min_pct_of_adv=0),
            prints=[make_trade(size=50_000, price=PROFILES["PENNY"].price, raw_id="p2")],
            adv=PROFILES["PENNY"].adv,
        )
    )
    assert all_mode == []  # $185k fails the $1M dollar floor


def test_ultra_high_priced_stock_exposes_the_reverse_trap(state):
    """Three shares of a $742,000 stock is $2.2M — a block by value, not shares."""
    print_ = make_trade(size=3, price=PROFILES["BRK.A"].price, raw_id="brk")
    alerts = DarkPoolDetector().run(
        ctx_for(
            state,
            "BRK.A",
            dark_settings(min_notional=1_000_000, min_pct_of_adv=0),
            prints=[print_],
            adv=PROFILES["BRK.A"].adv,
        )
    )
    assert len(alerts) == 1
    assert "$2.23M" in alerts[0].headline


@pytest.mark.parametrize("symbol", ["AAPL", "NVDA", "PLTR", "SMCI", "PENNY"])
def test_pct_of_adv_scales_correctly_across_liquidity(state, symbol):
    """A print worth 1% of ADV should fire in every name at the same setting.

    Absolute size cannot do this — 1% of NVDA's ADV is 1.9M shares while 1% of
    SMCI's is 95,000. Relative sizing is what makes one threshold portable.
    """
    profile = PROFILES[symbol]
    size = int(profile.adv * 0.01)
    alerts = DarkPoolDetector().run(
        ctx_for(
            state,
            symbol,
            dark_settings(min_notional=1, min_pct_of_adv=0.5, combine="all"),
            prints=[make_trade(size=size, price=profile.price, raw_id=f"{symbol}-1")],
            adv=profile.adv,
        )
    )
    assert len(alerts) == 1, f"{symbol}: a 1%-of-ADV print should clear a 0.5% floor"


@pytest.mark.parametrize("symbol", ["AAPL", "NVDA", "PENNY"])
def test_a_tenth_of_a_percent_of_adv_is_filtered_everywhere(state, symbol):
    profile = PROFILES[symbol]
    size = max(1, int(profile.adv * 0.001))
    alerts = DarkPoolDetector().run(
        ctx_for(
            state,
            symbol,
            dark_settings(min_notional=1, min_pct_of_adv=0.5, combine="all"),
            prints=[make_trade(size=size, price=profile.price, raw_id=f"{symbol}-2")],
            adv=profile.adv,
        )
    )
    assert alerts == []


# --------------------------------------------------------------------------
# Independence
# --------------------------------------------------------------------------
def test_state_is_partitioned_per_ticker(state):
    """A spike in one name must not consume another name's watermark."""
    detector = VolumeAnomalyDetector()
    first = ctx_for(state, "AAPL", vol_settings(cooldown_minutes=0), spike(bars_for("AAPL"), 8.0))
    assert len(detector.run(first)) == 1
    state.flush_progress()

    second = ctx_for(state, "MSFT", vol_settings(cooldown_minutes=0), spike(bars_for("MSFT"), 8.0))
    assert len(detector.run(second)) == 1, "MSFT was suppressed by AAPL's watermark"


def test_cooldown_is_partitioned_per_ticker(state):
    detector = DarkPoolDetector()
    a = ctx_for(
        state, "AAPL", dark_settings(min_pct_of_adv=0, cooldown_minutes=240),
        prints=[make_trade(size=50_000, price=336.8, raw_id="a")], adv=55e6,
    )
    assert len(detector.run(a)) == 1
    state.flush_progress()

    b = ctx_for(
        state, "NVDA", dark_settings(min_pct_of_adv=0, cooldown_minutes=240),
        prints=[make_trade(size=50_000, price=178.9, raw_id="b")], adv=190e6,
    )
    assert len(detector.run(b)) == 1, "NVDA was suppressed by AAPL's cooldown"


@pytest.mark.parametrize("symbol", ALL)
def test_ticker_symbols_survive_config_validation(symbol):
    """Including the dotted class share BRK.A, which a naive regex would reject."""
    from monitor import config as config_mod

    cfg = config_mod.from_dict({"tickers": [symbol]})
    assert cfg.tickers == [symbol]
    assert not [i for i in cfg.issues if "ticker" in i.path]
