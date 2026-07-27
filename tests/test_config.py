from __future__ import annotations

import pytest

from monitor import config as config_mod
from monitor.params import ConfigError


def test_defaults_match_the_documented_analyst_conventions():
    cfg = config_mod.from_dict({"tickers": ["AAPL"]})
    vol = cfg.detector("volume_anomaly")
    assert vol["rvol_threshold"] == 2.0
    assert vol["zscore_threshold"] == 3.0
    assert vol["baseline_sessions"] == 20
    assert cfg.detector("options_flow")["min_premium"] == 100_000
    assert cfg.detector("options_flow")["require_volume_gt_oi"] is True
    insider = cfg.detector("insider_trades")
    assert insider["min_notional_sale"] > insider["min_notional_purchase"]
    assert insider["exclude_10b5_1_sales"] is True


def test_override_within_bounds_is_accepted():
    cfg = config_mod.from_dict(
        {
            "tickers": ["AAPL"],
            "detectors": {"volume_anomaly": {"rvol_threshold": 5.5}},
        }
    )
    assert cfg.detector("volume_anomaly")["rvol_threshold"] == 5.5
    assert cfg.issues == []


def test_out_of_range_override_is_clamped_and_reported():
    cfg = config_mod.from_dict(
        {
            "tickers": ["AAPL"],
            "detectors": {"volume_anomaly": {"rvol_threshold": 500}},
        }
    )
    assert cfg.detector("volume_anomaly")["rvol_threshold"] == 20.0
    assert any("above the supported range" in i.message for i in cfg.issues)


def test_strict_mode_rejects_what_run_mode_clamps():
    payload = {
        "tickers": ["AAPL"],
        "detectors": {"volume_anomaly": {"rvol_threshold": 500}},
    }
    config_mod.from_dict(payload, strict=False)  # tolerated
    with pytest.raises(ConfigError, match="rvol_threshold"):
        config_mod.from_dict(payload, strict=True)


def test_unknown_setting_is_flagged_not_silently_dropped():
    cfg = config_mod.from_dict(
        {"tickers": ["AAPL"], "detectors": {"volume_anomaly": {"rvol_treshold": 3}}}
    )
    assert any("unknown setting" in i.message for i in cfg.issues)


def test_choice_settings_reject_nonsense():
    cfg = config_mod.from_dict(
        {"tickers": ["AAPL"], "detectors": {"volume_anomaly": {"combine": "either"}}}
    )
    assert cfg.detector("volume_anomaly")["combine"] == "all"
    assert any("not allowed" in i.message for i in cfg.issues)


def test_list_settings_filter_unsupported_members():
    cfg = config_mod.from_dict(
        {
            "tickers": ["AAPL"],
            "detectors": {"options_flow": {"trade_types": ["sweep", "telepathy"]}},
        }
    )
    assert cfg.detector("options_flow")["trade_types"] == ["sweep"]
    assert any("unsupported value" in i.message for i in cfg.issues)


@pytest.mark.parametrize(
    "preset,shares,notional",
    [
        ("classic", 10_000, 200_000),
        ("institutional", 25_000, 1_000_000),
        ("mega", 100_000, 5_000_000),
    ],
)
def test_block_presets_move_the_sizing_defaults(preset, shares, notional):
    cfg = config_mod.from_dict(
        {"tickers": ["AAPL"], "detectors": {"block_trades": {"preset": preset}}}
    )
    settings = cfg.detector("block_trades")
    assert settings["min_shares"] == shares
    assert settings["min_notional"] == notional


def test_explicit_sizing_beats_the_preset():
    cfg = config_mod.from_dict(
        {
            "tickers": ["AAPL"],
            "detectors": {"block_trades": {"preset": "classic", "min_notional": 750_000}},
        }
    )
    settings = cfg.detector("block_trades")
    assert settings["min_notional"] == 750_000
    assert settings["min_shares"] == 10_000  # still from the preset


def test_per_ticker_override_applies_only_to_that_ticker():
    cfg = config_mod.from_dict(
        {
            "tickers": ["AAPL", "TSLA"],
            "overrides": {"TSLA": {"volume_anomaly": {"rvol_threshold": 4.0}}},
        }
    )
    assert cfg.detector("volume_anomaly", "TSLA")["rvol_threshold"] == 4.0
    assert cfg.detector("volume_anomaly", "AAPL")["rvol_threshold"] == 2.0


def test_override_for_unwatched_ticker_is_reported():
    cfg = config_mod.from_dict(
        {
            "tickers": ["AAPL"],
            "overrides": {"MSFT": {"volume_anomaly": {"rvol_threshold": 4.0}}},
        }
    )
    assert "MSFT" not in cfg.overrides
    assert any("not in `tickers`" in i.message for i in cfg.issues)


def test_tickers_are_normalised_and_junk_rejected():
    cfg = config_mod.from_dict({"tickers": ["aapl", "brk.b", "not a ticker", "AAPL"]})
    assert cfg.tickers == ["AAPL", "BRK.B"]
    assert any("does not look like a ticker" in i.message for i in cfg.issues)


def test_empty_ticker_list_is_fatal():
    with pytest.raises(ConfigError, match="tickers"):
        config_mod.from_dict({"tickers": []})


def test_needs_reflects_which_detectors_are_on():
    cfg = config_mod.from_dict(
        {
            "tickers": ["AAPL"],
            "detectors": {
                "volume_anomaly": {"enabled": False},
                "block_trades": {"enabled": False},
                "dark_pool": {"enabled": False},
                "options_flow": {"enabled": False},
                "insider_trades": {"enabled": True},
            },
        }
    )
    assert cfg.needs("insider") is True
    assert cfg.needs("bars") is False
    assert cfg.needs("trades") is False
    assert cfg.needs("flow") is False


def test_unknown_provider_choice_is_reported():
    cfg = config_mod.from_dict(
        {"tickers": ["AAPL"], "providers": {"bars": "bloomberg"}}
    )
    assert cfg.providers.bars == "fmp"
    assert any("not supported" in i.message for i in cfg.issues)


def test_uw_paths_are_overridable():
    cfg = config_mod.from_dict(
        {
            "tickers": ["AAPL"],
            "providers": {
                "unusual_whales": {"paths": {"dark_pool_ticker": "/v2/dp/{ticker}"}}
            },
        }
    )
    assert cfg.providers.uw_paths["dark_pool_ticker"] == "/v2/dp/{ticker}"
    # Untouched keys keep their defaults.
    assert cfg.providers.uw_paths["flow_alerts"] == "/api/option-trades/flow-alerts"
