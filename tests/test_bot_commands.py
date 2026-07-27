"""The bot's command surface.

Tested through the router rather than either front end, because the console and
Telegram both go through exactly this — so these tests cover both.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from monitor import config as config_mod
from monitor.bot.commands import BotContext, CommandRouter
from monitor.bot.console import Console
from monitor.runtime import Overlay
from monitor.state import State

NOW = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)

BASE_CONFIG = """
tickers:
  - NVDA
  - AAPL
detectors:
  volume_anomaly:
    enabled: true
  dark_pool:
    enabled: true
"""


@pytest.fixture
def router(tmp_path) -> CommandRouter:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(BASE_CONFIG)
    overlay = Overlay(path=tmp_path / "runtime.json")
    ctx = BotContext(
        config_path=config_path,
        state_path=tmp_path / "monitor.db",
        overlay=overlay,
        config=config_mod.load(config_path, overlay=overlay),
        canslim=None,
        now=lambda: NOW,
    )
    return CommandRouter(ctx)


def plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
def test_leading_slash_is_optional(router):
    assert plain(router.handle("/list").text) == plain(router.handle("list").text)


def test_empty_input_shows_help(router):
    assert "Watchlist" in router.handle("").text


def test_unknown_command_suggests_a_close_match(router):
    reply = router.handle("/lst")
    assert "Unknown command" in reply.text
    assert "/list" in reply.text


@pytest.mark.parametrize("alias", ["ls", "watchlist", "list"])
def test_aliases_reach_the_same_handler(router, alias):
    assert "Watchlist" in router.handle(alias).text


def test_a_handler_raising_does_not_kill_the_bot(router, monkeypatch):
    def boom(args):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(router, "cmd_status", boom)
    reply = router.handle("/status")
    assert "RuntimeError" in reply.text
    assert "kaboom" in reply.text


# --------------------------------------------------------------------------
# Watchlist
# --------------------------------------------------------------------------
def test_list_shows_configured_tickers(router):
    text = plain(router.handle("/list").text)
    assert "NVDA" in text and "AAPL" in text
    assert "no signals" in text


def test_list_offers_a_button_per_ticker(router):
    reply = router.handle("/list")
    commands = [b.command for b in reply.buttons]
    assert "history NVDA" in commands
    assert "history AAPL" in commands


def test_add_then_list_includes_the_new_ticker(router):
    reply = router.handle("/add pltr")
    assert reply.dirty
    assert "PLTR" in plain(reply.text)
    assert "PLTR" in plain(router.handle("/list").text)


def test_add_multiple_at_once(router):
    router.handle("/add PLTR CRWD SMCI")
    text = plain(router.handle("/list").text)
    for symbol in ("PLTR", "CRWD", "SMCI"):
        assert symbol in text


def test_bot_added_tickers_are_marked_as_such(router):
    router.handle("/add PLTR")
    text = plain(router.handle("/list").text)
    assert "added via bot" in text


def test_remove_drops_a_baseline_ticker(router):
    router.handle("/remove NVDA")
    assert "NVDA" not in plain(router.handle("/list").text)


def test_removing_an_unwatched_ticker_explains_itself(router):
    assert "not on the watchlist" in router.handle("/remove ZZZZ").text


def test_add_with_no_argument_shows_usage(router):
    assert "Usage" in router.handle("/add").text


def test_the_watchlist_change_survives_a_new_router(router, tmp_path):
    """A bot edit must persist to the next cron tick, not just this process."""
    router.handle("/add PLTR")
    reloaded = Overlay.load(tmp_path / "runtime.json")
    cfg = config_mod.load(tmp_path / "config.yaml", overlay=reloaded)
    assert "PLTR" in cfg.tickers


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------
def test_history_with_no_signals_says_so(router):
    text = plain(router.handle("/history NVDA").text)
    assert "No signals recorded" in text


def test_history_offers_the_canslim_button(router):
    reply = router.handle("/history NVDA")
    assert any(b.command == "grade NVDA" for b in reply.buttons)


def test_history_for_an_unwatched_ticker_offers_to_add_it(router):
    text = plain(router.handle("/history ZZZZ").text)
    assert "Not currently on the watchlist" in text
    assert "/add ZZZZ" in text


def test_history_reports_recorded_signals(router):
    with State(router.ctx.state_path) as state:
        for i in range(3):
            state.record_signal(
                dedup_id=f"d{i}",
                ticker="NVDA",
                detector="dark_pool",
                severity="high",
                headline=f"Dark pool print — $5.0{i}M",
                detail="50.0K shares off-exchange",
                occurred_at=NOW - timedelta(days=i),
            )
        state.commit()

    text = plain(router.handle("/history NVDA").text)
    assert "3 signal(s)" in text
    assert "dark_pool ×3" in text
    assert "Dark pool print" in text


def test_history_respects_the_window(router):
    with State(router.ctx.state_path) as state:
        state.record_signal(
            dedup_id="old", ticker="NVDA", detector="dark_pool", severity="low",
            headline="ancient", detail="", occurred_at=NOW - timedelta(days=40),
        )
        state.record_signal(
            dedup_id="new", ticker="NVDA", detector="dark_pool", severity="low",
            headline="recent", detail="", occurred_at=NOW - timedelta(days=2),
        )
        state.commit()

    fortnight = plain(router.handle("/history NVDA").text)
    assert "recent" in fortnight and "ancient" not in fortnight

    quarter = plain(router.handle("/history NVDA 60").text)
    assert "ancient" in quarter


def test_history_rejects_a_non_numeric_day_count(router):
    assert "not a number" in router.handle("/history NVDA soon").text


def test_list_shows_signal_counts_once_there_are_some(router):
    with State(router.ctx.state_path) as state:
        state.record_signal(
            dedup_id="d1", ticker="NVDA", detector="dark_pool", severity="high",
            headline="big print", detail="", occurred_at=NOW - timedelta(hours=2),
        )
        state.commit()
    text = plain(router.handle("/list").text)
    assert "1 signal" in text


# --------------------------------------------------------------------------
# Levels and tuning
# --------------------------------------------------------------------------
def test_levels_lists_every_detector_with_its_level(router):
    text = plain(router.handle("/levels").text)
    for name in config_mod.DETECTOR_SPECS:
        assert name in text
    assert "[L1]" in text and "[L2]" in text and "[L3]" in text


def test_on_and_off_toggle_a_detector(router):
    assert "disabled" in router.handle("/off dark_pool").text
    assert router.ctx.config.detector("dark_pool")["enabled"] is False
    assert "enabled" in router.handle("/on dark_pool").text
    assert router.ctx.config.detector("dark_pool")["enabled"] is True


def test_enabling_a_paid_detector_mentions_the_key(router):
    assert "UW_API_KEY" in router.handle("/on options_flow").text


def test_enabling_the_ibkr_detector_mentions_the_gateway(router):
    assert "gateway" in router.handle("/on option_volume").text


def test_set_changes_a_threshold(router):
    reply = router.handle("/set volume_anomaly rvol_threshold 3.5")
    assert reply.dirty
    assert router.ctx.config.detector("volume_anomaly")["rvol_threshold"] == 3.5


def test_set_accepts_shorthand(router):
    router.handle("/set dark_pool min_notional 2.5M")
    assert router.ctx.config.detector("dark_pool")["min_notional"] == 2_500_000


def test_set_rejects_an_out_of_range_value_with_the_range(router):
    reply = router.handle("/set volume_anomaly rvol_threshold 500")
    assert "rejected" in reply.text
    assert "1.2 to 20" in reply.text
    assert router.ctx.config.detector("volume_anomaly")["rvol_threshold"] == 2.0


def test_set_per_ticker(router):
    reply = router.handle("/set NVDA volume_anomaly rvol_threshold 4")
    assert "NVDA" in reply.text
    assert router.ctx.config.detector("volume_anomaly", "NVDA")["rvol_threshold"] == 4.0
    assert router.ctx.config.detector("volume_anomaly", "AAPL")["rvol_threshold"] == 2.0


def test_set_per_ticker_requires_a_watched_ticker(router):
    assert "not on the watchlist" in router.handle(
        "/set ZZZZ volume_anomaly rvol_threshold 4"
    ).text


def test_set_with_too_few_arguments_shows_usage(router):
    assert "Usage" in router.handle("/set volume_anomaly").text


def test_set_with_an_unknown_detector_shows_usage(router):
    assert "Unknown detector" in router.handle("/set nonsense foo 1").text


def test_config_shows_bounds_and_marks_changes(router):
    router.handle("/set volume_anomaly rvol_threshold 3.5")
    text = plain(router.handle("/config volume_anomaly").text)
    assert "rvol_threshold = 3.5" in text
    assert "1.2 to 20" in text
    assert "✏️" in text


def test_config_without_arguments_shows_run_settings(router):
    text = plain(router.handle("/config").text)
    assert "Run settings" in text
    assert "min_severity" in text


def test_run_changes_a_global_setting(router):
    router.handle("/run min_severity high")
    assert router.ctx.config.run["min_severity"] == "high"


def test_run_rejects_an_unknown_setting(router):
    assert "unknown run setting" in router.handle("/run nonsense 1").text


def test_explain_gives_ranges_and_reasoning(router):
    text = plain(router.handle("/explain volume_anomaly").text)
    assert "rvol_threshold" in text
    assert "1.2 to 20" in text
    assert "standard" in text.lower()


def test_reset_restores_the_committed_defaults(router):
    router.handle("/set volume_anomaly rvol_threshold 5")
    router.handle("/reset volume_anomaly")
    assert router.ctx.config.detector("volume_anomaly")["rvol_threshold"] == 2.0


def test_changes_lists_the_diff_from_config_yaml(router):
    assert "No live changes" in router.handle("/changes").text
    router.handle("/add PLTR")
    router.handle("/set dark_pool min_notional 3M")
    text = plain(router.handle("/changes").text)
    assert "PLTR" in text
    assert "dark_pool.min_notional" in text


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------
def test_status_reports_the_essentials(router):
    text = plain(router.handle("/status").text)
    assert "Market:" in text
    assert "Watching 2 ticker(s)" in text
    assert "never" in text


def test_status_surfaces_unhealthy_sources(router):
    with State(router.ctx.state_path) as state:
        state.bump_counter("health:bars:NVDA")
        state.bump_counter("health:bars:NVDA")
        state.commit()
    text = plain(router.handle("/status").text)
    assert "Data source problems" in text
    assert "bars:NVDA" in text


def test_status_says_canslim_is_unavailable_when_it_is(router):
    assert "unavailable" in plain(router.handle("/status").text)


def test_grade_without_the_service_explains_how_to_get_it(router):
    text = router.handle("/grade NVDA").text
    assert "can-slim-grader" in text
    assert "git clone" in text


# --------------------------------------------------------------------------
# Console rendering
# --------------------------------------------------------------------------
def test_console_strips_html_for_the_terminal(router):
    console = Console(router=router, colour=False)
    rendered = console.render(router.handle("/list"))
    assert "<b>" not in rendered
    assert "NVDA" in rendered


def test_console_numbers_the_buttons(router):
    console = Console(router=router, colour=False)
    rendered = console.render(router.handle("/list"))
    assert "[1]" in rendered
    assert "→ /history" in rendered


def test_console_button_number_dispatches_that_command(router):
    console = Console(router=router, colour=False)
    listing = router.handle("/list")
    followed = console.run_line("1", listing)
    assert "NVDA" in plain(followed.text)


def test_console_ignores_a_number_out_of_range(router):
    console = Console(router=router, colour=False)
    listing = router.handle("/list")
    reply = console.run_line("99", listing)
    assert "Unknown command" in reply.text


def test_console_shows_attachments(router, tmp_path):
    from monitor.bot.commands import Reply

    attachment = tmp_path / "NVDA-canslim.pdf"
    attachment.write_bytes(b"%PDF-1.4 fake")
    console = Console(router=router, colour=False)
    rendered = console.render(Reply(text="done", files=[attachment]))
    assert "attachment" in rendered
    assert "NVDA-canslim.pdf" in rendered
    assert "bytes" in rendered


# --------------------------------------------------------------------------
# /narrator
# --------------------------------------------------------------------------
def test_narrator_shows_its_state_and_offers_the_switch(router):
    reply = router.handle("/narrator")
    text = plain(reply.text)
    assert "off (computed only)" in text
    assert "ungraded rather than guessed" in text
    assert [b.command for b in reply.buttons] == ["narrator llm"]


def test_a_bare_backend_name_is_enough_to_switch_it_on(router):
    """`/narrator llm` is what anyone will type; it must not need the key name."""
    reply = router.handle("/narrator llm")
    assert "canslim.narrator set to llm" in plain(reply.text)
    assert router.ctx.config.canslim["narrator"] == "llm"
    assert reply.dirty is True


def test_turning_it_on_declares_the_cost_and_the_guardrail(router):
    router.handle("/narrator llm")
    text = plain(router.handle("/narrator").text)
    assert "🟢 Claude" in text
    assert "cannot" in text and "C, A, S, L, M" in text
    assert "ANTHROPIC_API_KEY" in text
    assert "$0.10-0.40 per ticker per day" in text


def test_the_narrator_prefix_is_optional_on_a_setting(router):
    assert "narrator_effort set to high" in plain(router.handle("/narrator effort high").text)
    assert router.ctx.config.canslim["narrator_effort"] == "high"


def test_a_rejected_narrator_setting_says_what_is_allowed(router):
    reply = router.handle("/narrator max_tokens 999999")
    assert "rejected" in reply.text
    assert "2,000 to 64,000" in reply.text
    assert router.ctx.config.canslim["narrator_max_tokens"] == 16000


def test_the_narrator_change_shows_up_in_changes(router):
    router.handle("/narrator llm")
    assert "canslim.narrator = llm" in plain(router.handle("/changes").text)


def test_reset_puts_the_narrator_back(router):
    router.handle("/narrator llm")
    router.handle("/reset")
    assert router.ctx.config.canslim["narrator"] == "off"


def test_help_mentions_the_narrator(router):
    assert "/narrator" in router.handle("/help").text


# --------------------------------------------------------------------------
# Freshness warnings — the bot reads records a separate cron writes
# --------------------------------------------------------------------------
def _record_run(router, when):
    with State(router.ctx.state_path) as state:
        state.record_run(when, 0, "ok")
        state.commit()


def test_no_run_yet_is_called_out_not_shown_as_quiet(router):
    """An empty watchlist means "nobody is looking", not "nothing is happening"."""
    for command in ("/list", "/history NVDA", "/status"):
        text = plain(router.handle(command).text)
        assert "No run has ever been recorded" in text, command


def test_a_stale_cron_is_flagged_on_every_view(router):
    _record_run(router, NOW - timedelta(hours=3))
    for command in ("/list", "/history NVDA", "/status"):
        text = plain(router.handle(command).text)
        assert "Last poll was" in text, command
        assert "may be out of date" in text, command


def test_a_recent_run_shows_no_warning(router):
    _record_run(router, NOW - timedelta(minutes=6))
    text = plain(router.handle("/list").text)
    assert "Last poll was" not in text
    assert "No run has ever" not in text


def test_a_stale_cron_during_the_session_says_alerts_are_being_missed(tmp_path):
    """The warning is sharper when the market is actually open."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(BASE_CONFIG)
    overlay = Overlay(path=tmp_path / "runtime.json")
    open_now = datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc)  # 14:00 ET, Monday
    ctx = BotContext(
        config_path=config_path,
        state_path=tmp_path / "monitor.db",
        overlay=overlay,
        config=config_mod.load(config_path, overlay=overlay),
        canslim=None,
        now=lambda: open_now,
    )
    router = CommandRouter(ctx)
    with State(ctx.state_path) as state:
        state.record_run(open_now - timedelta(hours=2), 0, "ok")
        state.commit()

    text = plain(router.handle("/status").text)
    assert "alerts are being missed right now" in text


def test_status_names_the_credentials_it_is_missing(router, monkeypatch):
    """A keyless detector never fires, which looks exactly like a quiet market."""
    for name in ("FMP_API_KEY", "UW_API_KEY", "SEC_USER_AGENT"):
        monkeypatch.delenv(name, raising=False)
    text = plain(router.handle("/status").text)
    assert "Data sources that cannot be reached" in text
    assert "FMP_API_KEY" in text
    assert "Silence from them is not an all-clear" in text


def test_status_stops_naming_a_credential_once_it_is_set(router, monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.delenv("UW_API_KEY", raising=False)
    text = plain(router.handle("/status").text)
    assert "FMP_API_KEY" not in text
    assert "UW_API_KEY" in text


def test_status_asks_for_the_anthropic_key_only_when_the_narrator_is_on(router, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert "ANTHROPIC_API_KEY" not in plain(router.handle("/status").text)
    router.handle("/narrator llm")
    assert "ANTHROPIC_API_KEY" in plain(router.handle("/status").text)


# --------------------------------------------------------------------------
# /grade freshness and provenance
# --------------------------------------------------------------------------
class StubGradeService:
    """Records how it was called and returns a report we control."""

    def __init__(self, report, skipped=None):
        self.report = report
        self.skipped = skipped
        self.calls: list[dict] = []
        self.unavailable_reason = None

    def grade(self, ticker, now=None, *, max_age_minutes=None, force=False):
        from monitor.canslim.service import GradeOutcome

        self.calls.append({"ticker": ticker, "force": force, "max_age": max_age_minutes})
        if self.skipped:
            return GradeOutcome(ticker, skipped=self.skipped)
        return GradeOutcome(ticker, report=self.report)


def _report(tmp_path, *, narrated=False, graded_at=None, from_cache=False):
    from monitor.canslim.grader import Grade, LetterScore
    from monitor.canslim.report import Report

    html = tmp_path / "NVDA-canslim.html"
    html.write_text("<html></html>")
    grade = Grade(
        ticker="NVDA",
        company="Nvidia",
        as_of="2026-07-27 14:00 ET",
        price=100.0,
        letters=[
            LetterScore(key=k, score="pass", threshold="", actual="", read="")
            for k in ("C", "A", "N", "S", "L", "I", "M")
        ],
        verdict="BUY-RANGE",
        tone="up",
        summary="Passes every letter.",
        narrated=narrated,
    )
    return Report(
        grade=grade,
        html_path=html,
        pdf_path=None,
        graded_at=graded_at,
        from_cache=from_cache,
    )


def _grade_router(router, service):
    router.ctx.canslim = service
    return router


def test_grade_refetches_by_default(router, tmp_path):
    """Someone who types /grade is asking about now, not about this morning."""
    service = StubGradeService(_report(tmp_path))
    _grade_router(router, service)
    reply = router.handle("/grade NVDA")
    assert service.calls[0]["force"] is True
    assert "Graded just now" in plain(reply.text)


def test_grade_can_be_asked_for_the_cached_one(router, tmp_path):
    service = StubGradeService(
        _report(tmp_path, graded_at=NOW - timedelta(minutes=45), from_cache=True)
    )
    _grade_router(router, service)
    reply = router.handle("/grade NVDA cached")
    assert service.calls[0]["force"] is False
    text = plain(reply.text)
    assert "Reused a grade from 45 min ago" in text
    assert "figures are as of then" in text
    assert any(b.command == "grade NVDA" for b in reply.buttons)


def test_grade_says_when_claude_wrote_the_letters(router, tmp_path):
    """The PDF got this right; the chat message used to claim it was computed."""
    service = StubGradeService(_report(tmp_path, narrated=True))
    _grade_router(router, service)
    text = plain(router.handle("/grade NVDA").text)
    assert "written by Claude" in text
    assert "may contain errors" in text


def test_grade_says_when_the_rubric_wrote_the_letters(router, tmp_path):
    service = StubGradeService(_report(tmp_path, narrated=False))
    _grade_router(router, service)
    text = plain(router.handle("/grade NVDA").text)
    assert "scored programmatically" in text
    assert "written by Claude" not in text


def test_a_failed_grade_does_not_fall_back_to_stale_figures(router, tmp_path):
    service = StubGradeService(None, skipped="price history unavailable (HTTP 429)")
    _grade_router(router, service)
    text = plain(router.handle("/grade NVDA").text)
    assert "price history unavailable" in text
    assert "no stale figures are being shown" in text
