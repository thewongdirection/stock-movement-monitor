"""Bounded parameters, per-ticker overrides, and the file/chat asymmetry."""

from __future__ import annotations

import json

import pytest

from monitor.config import SCHEMA, Config, ConfigError, Overlay, Param, load, tunable_paths


def write(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


class TestParam:
    def test_a_number_below_the_floor_is_clamped_not_rejected(self):
        param = SCHEMA["signals.volume.rvol_threshold"]
        assert param.check(0.2) is not None
        assert param.clamp(0.2) == param.lo

    def test_an_int_param_stays_an_int_after_clamping(self):
        param = SCHEMA["poll.baseline_sessions"]
        assert isinstance(param.clamp(1000), int)

    def test_a_bool_accepts_the_words_people_actually_type(self):
        param = Param(True, kind="bool")
        assert param.coerce("yes") is True
        assert param.coerce("OFF") is False
        with pytest.raises(ConfigError):
            param.coerce("maybe")

    def test_a_choice_outside_the_list_is_refused(self):
        param = SCHEMA["signals.volume.combine"]
        with pytest.raises(ConfigError):
            param.coerce("some")

    def test_a_bool_is_not_silently_accepted_as_a_number(self):
        """True would otherwise coerce to 1.0 and pass every bound."""
        with pytest.raises(ConfigError):
            SCHEMA["signals.volume.rvol_threshold"].coerce(True)

    def test_a_list_accepts_a_comma_string(self):
        assert SCHEMA["watchlist"].coerce("nvda, msft ") == ["nvda", "msft"]

    def test_describe_shows_the_band(self):
        assert "1.1..20" in SCHEMA["signals.volume.rvol_threshold"].describe()


class TestYamlQuirks:
    def test_bare_off_is_not_read_as_false(self, tmp_path):
        """YAML 1.1 parses `off` as boolean False, which then fails a choices check."""
        path = write(tmp_path, "watchlist: [NVDA]\ncanslim:\n  narrator: off\n")
        config = load(path)
        assert config.get("canslim.narrator") == "off"
        assert not any("narrator" in w for w in config.warnings)

    def test_a_quoted_field_id_stays_a_string(self, tmp_path):
        path = write(tmp_path, 'watchlist: [NVDA]\nibkr:\n  oi_field: "7638"\n')
        assert load(path).get("ibkr.oi_field") == "7638"


class TestLoading:
    def test_an_out_of_range_value_is_clamped_and_reported(self, tmp_path):
        path = write(tmp_path, "watchlist: [NVDA]\nsignals:\n  volume:\n    rvol_threshold: 0.2\n")
        config = load(path)
        assert config.get("signals.volume.rvol_threshold") == 1.1
        assert any("clamped" in w for w in config.warnings)

    def test_an_unknown_key_is_reported_and_ignored(self, tmp_path):
        path = write(tmp_path, "watchlist: [NVDA]\nnonsense: 1\n")
        config = load(path)
        assert any("nonsense" in w for w in config.warnings)

    def test_a_missing_file_raises(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load(tmp_path / "absent.yaml")

    def test_unparsable_yaml_raises(self, tmp_path):
        path = write(tmp_path, "watchlist: [NVDA\n  broken")
        with pytest.raises(ConfigError, match="not valid YAML"):
            load(path)

    def test_a_top_level_list_raises(self, tmp_path):
        path = write(tmp_path, "- one\n- two\n")
        with pytest.raises(ConfigError, match="mapping"):
            load(path)

    def test_an_empty_file_loads_with_defaults(self, tmp_path):
        path = write(tmp_path, "")
        assert load(path).get("signals.volume.rvol_threshold") == 2.0


class TestOverrides:
    def test_a_ticker_override_wins_over_the_global(self, tmp_path):
        path = write(tmp_path, """
watchlist: [NVDA, MSFT]
signals: {volume: {rvol_threshold: 2.0}}
overrides:
  MSFT: {signals: {volume: {rvol_threshold: 1.4}}}
""")
        config = load(path)
        assert config.get("signals.volume.rvol_threshold") == 2.0
        assert config.get("signals.volume.rvol_threshold", "MSFT") == 1.4
        assert config.get("signals.volume.rvol_threshold", "NVDA") == 2.0

    def test_ticker_lookup_is_case_insensitive(self, tmp_path):
        path = write(tmp_path, """
watchlist: [MSFT]
overrides: {msft: {signals: {volume: {rvol_threshold: 1.4}}}}
""")
        assert load(path).get("signals.volume.rvol_threshold", "msft") == 1.4

    def test_an_override_for_an_unwatched_ticker_is_flagged(self, tmp_path):
        path = write(tmp_path, """
watchlist: [NVDA]
overrides: {TSLA: {signals: {volume: {rvol_threshold: 3.0}}}}
""")
        assert any("TSLA" in w for w in load(path).warnings)

    def test_an_override_is_bounded_like_anything_else(self, tmp_path):
        path = write(tmp_path, """
watchlist: [MSFT]
overrides: {MSFT: {signals: {volume: {rvol_threshold: 900}}}}
""")
        config = load(path)
        assert config.get("signals.volume.rvol_threshold", "MSFT") == 20.0
        assert any("clamped" in w for w in config.warnings)


class TestValidate:
    def test_an_empty_watchlist_is_a_hard_error(self, config):
        config.values["watchlist"] = []
        assert any("watchlist is empty" in p for p in config.validate())

    def test_duplicates_are_reported(self, config):
        config.values["watchlist"] = ["NVDA", "NVDA"]
        assert any("duplicates" in p for p in config.validate())

    def test_open_interest_without_an_options_source_is_an_error(self, config):
        config.values["sources.options"] = "off"
        config.values["signals.open_interest.enabled"] = True
        assert any("open_interest" in p for p in config.validate())

    def test_blocks_without_a_trades_source_is_an_error(self, config):
        config.values["signals.blocks.enabled"] = True
        assert any("blocks" in p for p in config.validate())

    def test_all_signals_disabled_is_an_error(self, config):
        for name in ("volume", "blocks", "open_interest", "insider"):
            config.values[f"signals.{name}.enabled"] = False
        assert any("every signal is disabled" in p for p in config.validate())

    def test_a_baseline_that_can_never_fill_is_an_error(self, config):
        config.values["poll.baseline_sessions"] = 5
        config.values["poll.min_baseline_samples"] = 10
        assert any("min_baseline_samples" in p for p in config.validate())

    def test_retention_shorter_than_the_cluster_window_is_an_error(self, config):
        """Pruning would delete the rows a cluster is about to be built from."""
        config.values["state.retention_days"] = 10
        config.values["signals.insider.cluster_window_days"] = 30
        assert any("retention_days" in p for p in config.validate())

    def test_the_shipped_example_config_validates(self):
        from pathlib import Path
        example = Path(__file__).resolve().parents[1] / "config.example.yaml"
        config = load(example)
        assert config.validate() == []
        assert config.warnings == []


class TestInteractiveEdits:
    def test_an_out_of_range_set_is_refused_not_clamped(self, config):
        with pytest.raises(ConfigError, match="above the maximum"):
            config.set("signals.volume.rvol_threshold", 99)
        assert config.get("signals.volume.rvol_threshold") == 2.0

    def test_an_unknown_path_is_refused(self, config):
        with pytest.raises(ConfigError, match="unknown setting"):
            config.set("signals.volume.nope", 1)

    def test_a_non_tunable_path_is_refused(self, config):
        with pytest.raises(ConfigError, match="not adjustable"):
            config.set("state.path", "/tmp/x")

    def test_a_valid_set_lands_on_the_ticker(self, config):
        config.set("signals.volume.rvol_threshold", "1.4", "msft")
        assert config.get("signals.volume.rvol_threshold", "MSFT") == 1.4
        assert config.get("signals.volume.rvol_threshold") == 2.0

    def test_nothing_that_breaks_the_next_run_silently_is_tunable_from_chat(self):
        """Thresholds are fair game. Endpoints, files and transports are not.

        A mistyped threshold produces too many or too few alerts, which is
        obvious. A mistyped state path produces a monitor that appears to work
        and quietly loses its dedup and open-interest history.
        """
        tunable = set(tunable_paths())
        for path in ("state.path", "sources.bars", "sources.options", "sources.insider",
                     "sources.trades", "sources.replay_dir", "notify.channel",
                     "canslim.model", "poll.bar_minutes",
                     "ibkr.base_url", "ibkr.verify_tls", "ibkr.oi_field",
                     "http.timeout_seconds", "http.retries"):
            assert path in SCHEMA, path
            assert path not in tunable, path


class TestOverlay:
    def test_a_set_survives_a_reload(self, tmp_path):
        path = tmp_path / "runtime.json"
        Overlay.load(path).set("signals.volume.rvol_threshold", 1.5)
        assert Overlay.load(path).values["signals.volume.rvol_threshold"] == 1.5

    def test_the_overlay_is_validated_on_write(self, tmp_path):
        with pytest.raises(ConfigError):
            Overlay.load(tmp_path / "runtime.json").set("signals.volume.rvol_threshold", 99)

    def test_the_overlay_wins_over_the_file(self, tmp_path):
        config_path = write(tmp_path, "watchlist: [NVDA]\nsignals: {volume: {rvol_threshold: 2.0}}\n")
        overlay_path = tmp_path / "runtime.json"
        Overlay.load(overlay_path).set("signals.volume.rvol_threshold", 3.0)
        assert load(config_path, overlay=overlay_path).get("signals.volume.rvol_threshold") == 3.0

    def test_a_corrupt_overlay_is_reported_and_skipped(self, tmp_path):
        config_path = write(tmp_path, "watchlist: [NVDA]\n")
        overlay_path = tmp_path / "runtime.json"
        overlay_path.write_text("{not json")
        config = load(config_path, overlay=overlay_path)
        assert any("overlay" in w for w in config.warnings)
        assert config.watchlist == ["NVDA"]

    def test_clear_removes_everything(self, tmp_path):
        path = tmp_path / "runtime.json"
        overlay = Overlay.load(path)
        overlay.set("signals.volume.rvol_threshold", 1.5)
        overlay.set("signals.volume.rvol_threshold", 1.5, "MSFT")
        overlay.clear()
        assert Overlay.load(path).is_empty()

    def test_an_overlay_write_is_atomic(self, tmp_path):
        path = tmp_path / "runtime.json"
        Overlay.load(path).set("notify.max_per_run", 5)
        assert json.loads(path.read_text())["values"]["notify.max_per_run"] == 5
        assert not (tmp_path / "runtime.json.tmp").exists()


class TestRoundTrip:
    def test_dump_yaml_reloads_to_the_same_values(self, tmp_path, config):
        config.set("signals.volume.rvol_threshold", 3.0)
        config.set("signals.volume.rvol_threshold", 1.4, "MSFT")
        path = write(tmp_path, config.dump_yaml())
        again = load(path)
        assert again.get("signals.volume.rvol_threshold") == 3.0
        assert again.get("signals.volume.rvol_threshold", "MSFT") == 1.4

    def test_section_collects_a_prefix(self, config):
        section = config.section("signals.open_interest")
        assert section["min_oi_change"] == 500
        assert "enabled" in section
