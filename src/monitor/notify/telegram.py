"""Telegram delivery.

Messages are sent as HTML rather than MarkdownV2. Telegram's MarkdownV2 requires
escaping eighteen characters including `.`, `-`, `(` and `)` — every one of which
appears constantly in "$205.44 (+1.43%)" — and a single missed escape rejects the
whole message with a 400. HTML needs three escapes and fails loudly on the rest.

`disable_notification` is wired to severity: low-severity alerts land in the chat
without buzzing the phone. A monitor that treats "volume was mildly unusual" and
"three insiders bought" as equally urgent gets muted within a week, and then it
is not a monitor.
"""

from __future__ import annotations

import logging
import time

from ..models import Severity
from ..sources.base import HttpClient, NotConfigured, redact

log = logging.getLogger("monitor.notify.telegram")

API = "https://api.telegram.org"

#: Telegram rejects anything longer. Splitting is on paragraph boundaries so a
#: fact list never gets cut mid-line.
MAX_MESSAGE = 4096


class TelegramNotifier:
    name = "telegram"

    def __init__(self, token: str | None, chat_id: str | None,
                 http: HttpClient | None = None, sleeper=time.sleep):
        if not token or not chat_id:
            missing = ", ".join(
                name for name, value in
                (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id))
                if not value
            )
            raise NotConfigured("telegram", f"{missing} not set")
        self.token = token
        self.chat_id = str(chat_id)
        self.http = http or HttpClient("telegram", retries=2)
        self._sleep = sleeper

    def _url(self, method: str) -> str:
        return f"{API}/bot{self.token}/{method}"

    def send(self, text: str, severity: Severity = Severity.MEDIUM,
             silent: bool | None = None) -> bool:
        quiet = (not severity.notifies) if silent is None else silent
        ok = True
        for chunk in split_message(text):
            ok = self._post(chunk, quiet) and ok
        return ok

    def _post(self, text: str, quiet: bool) -> bool:
        try:
            payload = self.http.session.post(
                self._url("sendMessage"),
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                    "disable_notification": quiet,
                },
                timeout=self.http.timeout,
            )
        except Exception as exc:                    # noqa: BLE001 - surfaced below
            log.error("telegram send failed: %s", redact(exc))
            return False

        if payload.ok:
            self._sleep(0.05)                       # Telegram allows ~30 msg/s
            return True

        # The body carries the actual reason ("chat not found", "bot was
        # blocked"), and none of those are fixed by retrying.
        log.error("telegram rejected the message: HTTP %s — %s",
                  payload.status_code, redact(payload.text[:300]))
        return False

    def verify(self) -> dict:
        """Confirm the token is valid and the chat is reachable."""
        me = self.http.get_json(self._url("getMe"))
        if not me.get("ok"):
            raise NotConfigured("telegram", "getMe rejected the bot token")
        chat = self.http.get_json(self._url("getChat"), {"chat_id": self.chat_id})
        if not chat.get("ok"):
            raise NotConfigured(
                "telegram",
                f"bot {me['result'].get('username')} cannot see chat {self.chat_id} — "
                "send it a message first so the chat exists",
            )
        return {"bot": me["result"], "chat": chat["result"]}


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Break an over-long message on paragraph, then line, boundaries."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(block) <= limit:
            current = block
            continue
        # A single paragraph over the limit — fall back to line boundaries.
        current = ""
        for line in block.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = line[:limit]
    if current:
        chunks.append(current)
    return chunks
