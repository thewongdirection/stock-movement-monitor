"""Message rendering and delivery."""

from __future__ import annotations

from datetime import datetime

import pytest
import responses

from monitor.clock import ET
from monitor.models import Alert, Severity, SourceIssue
from monitor.notify import build
from monitor.notify.base import (ConsoleNotifier, format_alert, format_digest,
                                 format_issues, should_notify)
from monitor.notify.telegram import TelegramNotifier, split_message
from monitor.sources.base import NotConfigured


def alert(**kwargs) -> Alert:
    defaults = dict(
        ticker="NVDA", signal="volume", severity=Severity.HIGH,
        headline="NVDA surged on 3.4x normal 11:00 volume (+1.43%)",
        occurred_at=datetime(2026, 7, 24, 11, 0, tzinfo=ET),
        facts=["Volume 12.4M vs 3.6M median", "Price 202.00 → 204.90 (+1.43%)"],
        read="Heavy buying pressure — volume-backed moves tend to follow through.",
        caveats=["Volume alone carries no direction."],
        identity=("2026-07-24T11:00",),
    )
    defaults.update(kwargs)
    return Alert(**defaults)


class TestSeverity:
    def test_low_does_not_buzz_the_phone(self):
        assert not Severity.LOW.notifies
        assert Severity.MEDIUM.notifies and Severity.HIGH.notifies

    def test_ranks_order_correctly(self):
        assert Severity.LOW.rank < Severity.MEDIUM.rank < Severity.HIGH.rank


class TestFormatAlert:
    def test_every_part_is_present(self):
        text = format_alert(alert())
        assert "NVDA surged" in text
        assert "• Volume 12.4M" in text
        assert "👉 Heavy buying pressure" in text
        assert "⚠ Volume alone carries no direction." in text
        assert "· volume" in text

    def test_the_read_is_never_dropped(self):
        """The regression this layout exists to prevent.

        A second copy of the layout in the console renderer once silently
        stopped printing the read, so --dry-run showed something different from
        what Telegram received.
        """
        for as_html in (False, True):
            assert "Heavy buying pressure" in format_alert(alert(), as_html=as_html)

    def test_html_mode_escapes_user_text(self):
        text = format_alert(alert(headline="NVDA <b>surge</b> & rally"), as_html=True)
        assert "&lt;b&gt;surge&lt;/b&gt;" in text and "&amp;" in text

    def test_plain_mode_does_not_escape(self):
        text = format_alert(alert(headline="NVDA surge & rally"), as_html=False)
        assert "NVDA surge & rally" in text

    def test_a_url_becomes_a_link_in_html(self):
        text = format_alert(alert(url="https://sec.gov/x"), as_html=True)
        assert '<a href="https://sec.gov/x">source</a>' in text
        assert "https://sec.gov/x" in format_alert(alert(url="https://sec.gov/x"))

    def test_a_canslim_line_is_included_when_given(self):
        text = format_alert(alert(), canslim="CAN SLIM B+ (72%) — WATCH")
        assert "📊 CAN SLIM B+" in text
        assert "📊" not in format_alert(alert())

    def test_an_alert_with_no_facts_still_renders(self):
        text = format_alert(alert(facts=[], caveats=[], read=""))
        assert "NVDA surged" in text


class TestFormatIssues:
    def test_nothing_wrong_renders_nothing(self):
        assert format_issues([]) == ""

    def test_issues_are_listed_with_the_warning_that_silence_is_not_calm(self):
        text = format_issues([SourceIssue("fmp", "NVDA", "unreachable", "timeout")])
        assert "Data source problems (1)" in text
        assert "fmp/NVDA" in text
        assert "this is not a quiet market" in text

    def test_a_watchlist_wide_issue_omits_the_ticker(self):
        text = format_issues([SourceIssue("fmp", "*", "stale", "frozen")])
        assert "fmp —" in text and "fmp/*" not in text


class TestDigest:
    def test_an_empty_digest_says_so(self):
        assert format_digest([]) == "No alerts."

    def test_each_alert_gets_one_line(self):
        text = format_digest([alert(), alert(ticker="MSFT")])
        assert len(text.splitlines()) == 2


class TestConsoleNotifier:
    def test_it_prints_through_the_shared_layout(self, capsys):
        ConsoleNotifier().send(format_alert(alert()))
        out = capsys.readouterr().out
        assert "👉 Heavy buying pressure" in out
        assert "─" in out


class TestShouldNotify:
    @pytest.mark.parametrize("severity,minimum,expected", [
        (Severity.LOW, "low", True), (Severity.LOW, "medium", False),
        (Severity.MEDIUM, "medium", True), (Severity.HIGH, "high", True),
        (Severity.MEDIUM, "high", False),
    ])
    def test_the_floor_is_applied(self, severity, minimum, expected):
        assert should_notify(alert(severity=severity), minimum) is expected


class TestSplitMessage:
    def test_a_short_message_is_not_split(self):
        assert split_message("hello") == ["hello"]

    def test_splitting_happens_on_paragraph_boundaries(self):
        blocks = ["x" * 100 for _ in range(60)]
        chunks = split_message("\n\n".join(blocks), limit=1000)
        assert len(chunks) > 1
        assert all(len(chunk) <= 1000 for chunk in chunks)

    def test_an_oversized_paragraph_falls_back_to_lines(self):
        text = "\n".join("y" * 90 for _ in range(40))
        chunks = split_message(text, limit=500)
        assert all(len(chunk) <= 500 for chunk in chunks)
        assert len(chunks) > 1

    def test_nothing_is_lost_when_splitting(self):
        blocks = [f"block {n} " + "x" * 80 for n in range(40)]
        chunks = split_message("\n\n".join(blocks), limit=600)
        joined = "".join(chunks)
        for n in range(40):
            assert f"block {n} " in joined


class TestTelegramNotifier:
    def test_missing_credentials_are_named(self):
        with pytest.raises(NotConfigured, match="TELEGRAM_CHAT_ID"):
            TelegramNotifier("token", None)
        with pytest.raises(NotConfigured, match="TELEGRAM_BOT_TOKEN"):
            TelegramNotifier(None, "123")

    @responses.activate
    def test_a_message_is_posted_as_html(self):
        responses.post("https://api.telegram.org/bottok/sendMessage", json={"ok": True})
        assert TelegramNotifier("tok", "123", sleeper=lambda s: None).send("hi") is True
        body = responses.calls[0].request.body
        assert b'"parse_mode": "HTML"' in body or '"parse_mode": "HTML"' in str(body)

    @responses.activate
    def test_low_severity_arrives_silently(self):
        responses.post("https://api.telegram.org/bottok/sendMessage", json={"ok": True})
        notifier = TelegramNotifier("tok", "123", sleeper=lambda s: None)
        notifier.send("hi", Severity.LOW)
        assert '"disable_notification": true' in str(responses.calls[0].request.body)

    @responses.activate
    def test_high_severity_buzzes(self):
        responses.post("https://api.telegram.org/bottok/sendMessage", json={"ok": True})
        notifier = TelegramNotifier("tok", "123", sleeper=lambda s: None)
        notifier.send("hi", Severity.HIGH)
        assert '"disable_notification": false' in str(responses.calls[0].request.body)

    @responses.activate
    def test_a_rejection_returns_false_rather_than_raising(self):
        responses.post("https://api.telegram.org/bottok/sendMessage",
                       status=400, json={"description": "chat not found"})
        assert TelegramNotifier("tok", "123", sleeper=lambda s: None).send("hi") is False

    @responses.activate
    def test_verify_reports_a_bad_token(self):
        responses.get("https://api.telegram.org/bottok/getMe", json={"ok": False})
        with pytest.raises(NotConfigured, match="token"):
            TelegramNotifier("tok", "123", sleeper=lambda s: None).verify()

    @responses.activate
    def test_verify_reports_an_unreachable_chat(self):
        responses.get("https://api.telegram.org/bottok/getMe",
                      json={"ok": True, "result": {"username": "mybot"}})
        responses.get("https://api.telegram.org/bottok/getChat", json={"ok": False})
        with pytest.raises(NotConfigured, match="send it a message first"):
            TelegramNotifier("tok", "123", sleeper=lambda s: None).verify()


class TestBuild:
    def test_console_is_returned_plain(self, config):
        config.values["notify.channel"] = "console"
        notifier, as_html = build(config, env={})
        assert notifier.name == "console" and as_html is False

    def test_force_console_overrides_the_channel(self, config):
        config.values["notify.channel"] = "telegram"
        notifier, _ = build(config, env={}, force_console=True)
        assert notifier.name == "console"

    def test_missing_telegram_credentials_raise_rather_than_downgrade(self, config):
        """A silent fallback to console means alerts print to a log nobody reads."""
        config.values["notify.channel"] = "telegram"
        with pytest.raises(NotConfigured):
            build(config, env={})
