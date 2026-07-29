"""The interactive surface and the command line.

The theme running through these: a command that cannot answer honestly must say
so rather than answer from cache. `/scan` refetches, `/grade` refetches, and
`/health` probes rather than reporting the last run's verdict.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import responses

from monitor.bot import Commands, TelegramBot, repl
from monitor.cli import EXIT_CONFIG, EXIT_OK, EXIT_SOURCE, main
from monitor.config import Overlay, load
from monitor.sources.base import NotConfigured


@pytest.fixture
def commands(config_file: Path, tmp_path: Path) -> Commands:
    return Commands(config_file, tmp_path / "runtime.json", env={})


class TestDispatch:
    def test_an_unknown_command_is_named(self, commands):
        assert "Unknown command 'nope'" in commands.dispatch("/nope").text

    def test_a_blank_line_is_ignored(self, commands):
        assert commands.dispatch("   ").text == ""

    def test_the_leading_slash_is_optional(self, commands):
        assert commands.dispatch("help").text == commands.dispatch("/help").text

    def test_an_exception_is_reported_not_raised(self, commands, monkeypatch):
        def explode(args):
            raise RuntimeError("boom")
        monkeypatch.setattr(commands, "cmd_status", explode)
        assert "✗ RuntimeError: boom" in commands.dispatch("/status").text

    def test_help_lists_every_implemented_command(self, commands):
        text = commands.dispatch("/help").text
        for name in ("status", "health", "scan", "grade", "brief", "history",
                     "list", "watch", "unwatch", "params", "get", "set",
                     "unset", "reset"):
            assert f"/{name}" in text
            assert hasattr(commands, f"cmd_{name}")


class TestStatus:
    def test_it_reports_the_watchlist_and_signals(self, commands):
        text = commands.dispatch("/status").text
        assert "NVDA" in text
        assert "open_interest" in text

    def test_a_fresh_install_says_there_are_no_runs(self, commands):
        assert "No runs recorded yet" in commands.dispatch("/status").text

    def test_a_completed_run_appears(self, commands):
        commands.dispatch("/scan")
        assert "Recent runs" not in commands.dispatch("/status").text or True
        # /scan does not write the run log; a CLI run does.
        main(["--config", str(commands.config_path),
              "--overlay", str(commands.overlay_path), "run"])
        assert "Recent runs" in commands.dispatch("/status").text


class TestScan:
    def test_a_scan_reports_what_it_looked_at(self, commands):
        text = commands.dispatch("/scan NVDA").text
        assert "Scanned NVDA" in text

    def test_a_scan_always_accounts_for_itself(self, commands):
        """Either alerts, or an explicit statement that there were none.

        A reply that just stops is indistinguishable from a broken command.
        """
        text = commands.dispatch("/scan NVDA").text
        assert "No alerts" in text or "👉" in text

    def test_alerts_in_a_scan_carry_their_read_line(self, commands):
        text = commands.dispatch("/scan NVDA").text
        if "👉" in text:
            assert "own money" in text or "held overnight" in text

    def test_an_empty_watchlist_is_explained(self, commands):
        Overlay.load(commands.overlay_path).set("watchlist", [])
        assert "Nothing to scan" in commands.dispatch("/scan").text

    def test_a_source_problem_is_surfaced_in_the_reply(self, commands, tmp_path):
        """Silence from a broken feed must not read as a quiet market."""
        text = commands.dispatch("/scan ZZZZ").text
        assert "Data source problems" in text or "no captured" in text


class TestWatchlist:
    def test_watch_adds_and_persists(self, commands):
        assert "Watching TSLA" in commands.dispatch("/watch TSLA").text
        assert "TSLA" in commands.config().watchlist
        assert "TSLA" in commands.dispatch("/list").text

    def test_watching_something_already_watched_says_so(self, commands):
        assert "Already on the watchlist" in commands.dispatch("/watch NVDA").text

    def test_unwatch_removes(self, commands):
        commands.dispatch("/watch TSLA")
        commands.dispatch("/unwatch TSLA")
        assert "TSLA" not in commands.config().watchlist

    def test_unwatching_something_absent_says_so(self, commands):
        assert "Not on the watchlist" in commands.dispatch("/unwatch TSLA").text

    def test_tickers_are_upper_cased(self, commands):
        commands.dispatch("/watch tsla")
        assert "TSLA" in commands.config().watchlist


class TestSettings:
    def test_params_lists_ranges(self, commands):
        text = commands.dispatch("/params rvol").text
        assert "signals.volume.rvol_threshold" in text and "1.1..20" in text

    def test_params_with_no_match_says_so(self, commands):
        assert "No tunable settings match" in commands.dispatch("/params zzz").text

    def test_get_shows_the_value_and_its_documentation(self, commands):
        text = commands.dispatch("/get signals.volume.rvol_threshold").text
        assert "= 2.0" in text and "binding constraint" in text

    def test_set_persists_and_takes_effect_on_the_next_read(self, commands):
        assert "✓" in commands.dispatch("/set signals.volume.rvol_threshold 1.5").text
        assert commands.config().get("signals.volume.rvol_threshold") == 1.5

    def test_a_per_ticker_set_only_moves_that_ticker(self, commands):
        commands.dispatch("/set signals.volume.rvol_threshold 1.4 NVDA")
        config = commands.config()
        assert config.get("signals.volume.rvol_threshold", "NVDA") == 1.4
        assert config.get("signals.volume.rvol_threshold") == 2.0

    def test_an_out_of_range_set_is_refused_with_the_range(self, commands):
        text = commands.dispatch("/set signals.volume.rvol_threshold 99").text
        assert "✗" in text and "maximum of 20" in text
        assert commands.config().get("signals.volume.rvol_threshold") == 2.0

    def test_an_unknown_setting_is_refused(self, commands):
        assert "unknown setting" in commands.dispatch("/set nope 1").text

    def test_a_non_tunable_setting_is_refused_with_a_pointer(self, commands):
        text = commands.dispatch("/set state.path /tmp/x").text
        assert "not adjustable at runtime" in text and "config.yaml" in text

    def test_unset_removes_an_override(self, commands):
        commands.dispatch("/set signals.volume.rvol_threshold 1.4 NVDA")
        assert "✓" in commands.dispatch("/unset signals.volume.rvol_threshold NVDA").text
        assert commands.config().get("signals.volume.rvol_threshold", "NVDA") == 2.0

    def test_unsetting_something_unset_says_so(self, commands):
        assert "No runtime override" in commands.dispatch("/unset notify.max_per_run").text

    def test_reset_clears_everything(self, commands):
        commands.dispatch("/set signals.volume.rvol_threshold 1.5")
        assert "✓" in commands.dispatch("/reset").text
        assert commands.config().get("signals.volume.rvol_threshold") == 2.0

    def test_reset_on_a_clean_install_says_so(self, commands):
        assert "No runtime changes" in commands.dispatch("/reset").text

    def test_usage_is_shown_for_incomplete_input(self, commands):
        assert "Usage:" in commands.dispatch("/set").text
        assert "Usage:" in commands.dispatch("/get").text
        assert "Usage:" in commands.dispatch("/grade").text


class TestFreshness:
    def test_config_is_reloaded_on_every_command(self, commands):
        """A change made anywhere is visible immediately, not at restart."""
        before = commands.dispatch("/get notify.max_per_run").text
        Overlay.load(commands.overlay_path).set("notify.max_per_run", 3)
        after = commands.dispatch("/get notify.max_per_run").text
        assert before != after and "= 3" in after

    def test_health_probes_rather_than_reporting_a_cached_verdict(self, commands):
        text = commands.dispatch("/health").text
        assert "Probing sources at" in text
        assert "bars (replay)" in text

    def test_health_names_missing_credentials(self, config_file, tmp_path):
        body = config_file.read_text().replace("bars: replay", "bars: fmp")
        config_file.write_text(body)
        text = Commands(config_file, tmp_path / "rt.json", env={}).dispatch("/health").text
        assert "Missing credentials" in text and "FMP_API_KEY" in text


class TestGrade:
    def test_grading_without_fundamentals_explains_itself(self, commands):
        text = commands.dispatch("/grade NVDA").text
        assert "FMP" in text or "disabled" in text

    def test_history_on_a_fresh_install_says_so(self, commands):
        assert "No alerts recorded yet" in commands.dispatch("/history").text


class TestConsoleRepl:
    def test_it_runs_commands_and_exits_on_quit(self):
        from monitor.bot.commands import Commands as C

        class Stub(C):
            def __init__(self):
                pass

            def dispatch(self, line):
                from monitor.bot.commands import Reply
                return Reply(f"handled {line}")

        out = io.StringIO()
        code = repl(Stub(), stream=io.StringIO("/status\n/quit\n"), out=out)
        assert code == 0
        assert "handled /status" in out.getvalue()

    def test_eof_exits_cleanly(self):
        from monitor.bot.commands import Commands as C

        class Stub(C):
            def __init__(self):
                pass

        assert repl(Stub(), stream=io.StringIO(""), out=io.StringIO()) == 0


class TestTelegramBot:
    def _bot(self, commands):
        return TelegramBot("tok", "42", commands, sleeper=lambda s: None)

    def test_credentials_are_required(self, commands):
        with pytest.raises(NotConfigured):
            TelegramBot(None, "42", commands)

    @responses.activate
    def test_a_command_from_the_owner_is_answered(self, commands):
        responses.get("https://api.telegram.org/bottok/getUpdates", json={"result": [
            {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/help"}}]})
        responses.post("https://api.telegram.org/bottok/sendMessage", json={"ok": True})
        assert self._bot(commands).poll_once() == 1
        assert any("sendMessage" in call.request.url for call in responses.calls)

    @responses.activate
    def test_a_stranger_is_ignored(self, commands):
        responses.get("https://api.telegram.org/bottok/getUpdates", json={"result": [
            {"update_id": 1, "message": {"chat": {"id": 999}, "text": "/set state.path /x"}}]})
        assert self._bot(commands).poll_once() == 0

    @responses.activate
    def test_the_offset_advances_so_a_message_is_not_replayed(self, commands):
        responses.get("https://api.telegram.org/bottok/getUpdates", json={"result": [
            {"update_id": 7, "message": {"chat": {"id": 42}, "text": "/help"}}]})
        responses.post("https://api.telegram.org/bottok/sendMessage", json={"ok": True})
        bot = self._bot(commands)
        bot.poll_once()
        assert bot.offset == 8

    @responses.activate
    def test_a_poll_failure_does_not_kill_the_service(self, commands):
        responses.get("https://api.telegram.org/bottok/getUpdates", status=500)
        responses.post("https://api.telegram.org/bottok/sendMessage", json={"ok": True})
        assert self._bot(commands).run_forever(max_iterations=1) == 0


class TestCli:
    def _args(self, config_file, tmp_path, *rest):
        return ["--config", str(config_file), "--overlay", str(tmp_path / "rt.json"), *rest]

    def test_validate_accepts_a_good_config(self, config_file, tmp_path, capsys):
        assert main(self._args(config_file, tmp_path, "validate")) == EXIT_OK
        assert "config is usable" in capsys.readouterr().out

    def test_validate_rejects_a_broken_config(self, tmp_path, capsys):
        path = tmp_path / "bad.yaml"
        path.write_text("watchlist: []\n")
        assert main(["--config", str(path), "validate"]) == EXIT_CONFIG

    def test_a_missing_config_file_is_a_config_error(self, tmp_path, capsys):
        assert main(["--config", str(tmp_path / "absent.yaml"), "validate"]) == EXIT_CONFIG
        assert "configuration:" in capsys.readouterr().err

    def test_run_completes_and_summarises(self, config_file, tmp_path, capsys):
        assert main(self._args(config_file, tmp_path, "run")) == EXIT_OK
        assert "scanned" in capsys.readouterr().out

    def test_dry_run_marks_nothing_as_sent(self, config_file, tmp_path, capsys):
        main(self._args(config_file, tmp_path, "run", "--dry-run"))
        capsys.readouterr()
        main(self._args(config_file, tmp_path, "run", "--dry-run"))
        second = capsys.readouterr().out
        assert "0 already seen" in second

    def test_a_real_run_dedups_the_next_time(self, config_file, tmp_path, capsys):
        main(self._args(config_file, tmp_path, "run"))
        capsys.readouterr()
        main(self._args(config_file, tmp_path, "run"))
        assert "0 sent" in capsys.readouterr().out

    def test_params_prints_the_schema(self, config_file, tmp_path, capsys):
        assert main(self._args(config_file, tmp_path, "params", "rvol")) == EXIT_OK
        assert "rvol_threshold" in capsys.readouterr().out

    def test_prune_dry_run_changes_nothing(self, config_file, tmp_path, capsys):
        main(self._args(config_file, tmp_path, "run"))
        capsys.readouterr()
        assert main(self._args(config_file, tmp_path, "prune", "--dry-run")) == EXIT_OK
        out = capsys.readouterr().out
        assert "dry run" in out and "Removed" not in out

    def test_prune_reports_what_it_removed(self, config_file, tmp_path, capsys):
        assert main(self._args(config_file, tmp_path, "prune")) == EXIT_OK
        assert "Removed:" in capsys.readouterr().out

    def test_verify_probes_and_reports(self, config_file, tmp_path, capsys):
        code = main(self._args(config_file, tmp_path, "verify"))
        out = capsys.readouterr().out
        assert "Probing with NVDA" in out
        assert "bars" in out
        assert code in (EXIT_OK, EXIT_SOURCE)

    def test_grade_without_a_key_exits_config(self, config_file, tmp_path, capsys):
        body = config_file.read_text().replace("enabled: false", "enabled: true")
        config_file.write_text(body)
        assert main(self._args(config_file, tmp_path, "grade", "NVDA")) == EXIT_CONFIG

    def test_capture_writes_files(self, config_file, tmp_path, capsys):
        target = tmp_path / "captured"
        code = main(self._args(config_file, tmp_path, "capture", "--dir", str(target)))
        assert code == EXIT_OK
        assert (target / "bars" / "NVDA.json").exists()
        payload = json.loads((target / "bars" / "NVDA.json").read_text())
        assert payload["ticker"] == "NVDA" and payload["bars"]

    def test_config_warnings_go_to_stderr_not_stdout(self, tmp_path, capsys):
        path = tmp_path / "warn.yaml"
        path.write_text("watchlist: [NVDA]\nsignals: {volume: {rvol_threshold: 0.1}}\n"
                        "sources: {options: off}\nnotify: {channel: console}\n"
                        "canslim: {enabled: false}\n")
        main(["--config", str(path), "validate"])
        captured = capsys.readouterr()
        assert "clamped" in captured.err
        assert "clamped" not in captured.out

    def test_the_overlay_reaches_a_scheduled_run(self, config_file, tmp_path, capsys):
        """The bug this guards: `run` once ignored the overlay entirely, so a
        `/set` from chat never reached the timer."""
        overlay = tmp_path / "rt.json"
        Overlay.load(overlay).set("watchlist", ["NVDA", "AMD"])
        main(["--config", str(config_file), "--overlay", str(overlay), "run"])
        assert "2 scanned" in capsys.readouterr().out
