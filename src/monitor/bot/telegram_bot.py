"""Long-polling Telegram bot.

Long polling rather than a webhook, because a webhook needs a public HTTPS
endpoint and a certificate — an unreasonable amount of infrastructure for a
single-user tool on a home box behind NAT.

Two safety properties matter here. The bot answers **only** the configured chat
id, so a stranger who finds the bot username cannot read your watchlist or
change your thresholds. And the update offset is advanced *before* the command
runs, so a command that crashes is not retried forever on every poll — the
message is acknowledged, the error is reported into the chat, and the loop
continues.
"""

from __future__ import annotations

import logging
import time

from ..sources.base import HttpClient, NotConfigured, redact
from .commands import Commands

log = logging.getLogger("monitor.bot")

API = "https://api.telegram.org"
POLL_TIMEOUT = 50


class TelegramBot:
    def __init__(self, token: str | None, chat_id: str | None, commands: Commands,
                 http: HttpClient | None = None, sleeper=time.sleep):
        if not token or not chat_id:
            raise NotConfigured(
                "telegram", "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are both required"
            )
        self.token = token
        self.chat_id = str(chat_id)
        self.commands = commands
        # The read timeout must outlast the long poll or every poll is an error.
        self.http = http or HttpClient("telegram-bot", timeout=POLL_TIMEOUT + 15, retries=1)
        self._sleep = sleeper
        self.offset = 0

    def _url(self, method: str) -> str:
        return f"{API}/bot{self.token}/{method}"

    def send(self, text: str) -> None:
        for chunk in _split(text):
            try:
                self.http.session.post(
                    self._url("sendMessage"),
                    json={"chat_id": self.chat_id, "text": chunk,
                          "disable_web_page_preview": True},
                    timeout=20,
                )
            except Exception as exc:                # noqa: BLE001
                log.error("failed to reply: %s", redact(exc))

    def poll_once(self) -> int:
        """Fetch and handle one batch. Returns the number of messages handled."""
        payload = self.http.get_json(
            self._url("getUpdates"),
            {"offset": self.offset, "timeout": POLL_TIMEOUT, "allowed_updates": '["message"]'},
        )
        updates = (payload or {}).get("result", []) or []
        handled = 0
        for update in updates:
            self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or {}
            chat = str((message.get("chat") or {}).get("id", ""))
            text = (message.get("text") or "").strip()
            if not text:
                continue
            if chat != self.chat_id:
                log.warning("ignoring a message from unauthorised chat %s", chat)
                continue
            handled += 1
            log.info("command: %s", text.split()[0])
            reply = self.commands.dispatch(text)
            self.send(reply.text or "(no output)")
        return handled

    def run_forever(self, max_iterations: int | None = None) -> int:
        """Poll until interrupted. `max_iterations` exists for tests."""
        self.send("Monitor bot is up. /help for commands.")
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            try:
                self.poll_once()
            except KeyboardInterrupt:
                self.send("Monitor bot stopping.")
                return 0
            except Exception as exc:                # noqa: BLE001
                # A transient network failure must not kill a service that
                # systemd will only restart five times before giving up.
                log.error("poll failed: %s", redact(exc))
                self._sleep(5)
        return 0


def _split(text: str, limit: int = 4000) -> list[str]:
    from ..notify.telegram import split_message
    return split_message(text, limit)
