"""Telegram delivery.

One message per alert, so each finding gets its own push notification and can
be acted on individually. Low-severity alerts are sent with notifications
suppressed — they land in the chat for review without buzzing the phone.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Sequence

import requests

from ..models import Alert, Severity
from ..params import ConfigError
from .base import chunk, format_alert

log = logging.getLogger(__name__)

API = "https://api.telegram.org"

#: Telegram throttles a single chat to roughly one message per second.
SEND_SPACING = 1.1


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, timeout: int = 20):
        # ConfigError, not ValueError: the CLI catches this and prints one clean
        # line. A bare ValueError escaped as a stack trace, which is a poor way
        # for a tool built around legible failures to say "you forgot a secret".
        missing = [
            name
            for name, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id))
            if not value
        ]
        if missing:
            raise ConfigError(
                f"{' and '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} not set, so there is nowhere "
                "to send alerts.\n"
                "  • Locally:  export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...\n"
                "  • On GitHub Actions:  Settings → Secrets and variables → Actions → "
                "New repository secret\n"
                "Get the token from @BotFather, message your new bot once, then run "
                "`monitor telegram-chat-id` to read the chat id.\n"
                "To run without sending anything, use `monitor run --dry-run`."
            )
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.session = requests.Session()
        self._last_send = 0.0
        self.failures: list[str] = []

    def send(self, alert: Alert, files: Sequence[Path] | None = None) -> bool:
        silent = alert.severity is Severity.LOW
        ok = True
        for part in chunk(format_alert(alert)):
            ok = self._post(part, silent=silent, preview=bool(alert.url)) and ok
        # Attachments follow the message. A failed upload does not fail the
        # alert — the text carries the finding; the report is supporting detail.
        for path in files or []:
            self._send_document(Path(path), silent=silent)
        return ok

    def _send_document(self, path: Path, silent: bool = False) -> bool:
        if not path.exists():
            log.warning("attachment missing, skipping: %s", path)
            return False
        self._space_out()
        try:
            with path.open("rb") as handle:
                response = self.session.post(
                    f"{API}/bot{self.token}/sendDocument",
                    data={
                        "chat_id": self.chat_id,
                        "caption": path.name,
                        "disable_notification": silent,
                    },
                    files={"document": (path.name, handle)},
                    timeout=max(self.timeout, 120),
                )
        except (requests.RequestException, OSError) as exc:
            self.failures.append(f"attachment {path.name}: {exc}")
            log.error("could not upload %s: %s", path, exc)
            return False
        if not response.ok:
            detail = _describe(response)
            self.failures.append(f"attachment {path.name}: {detail}")
            log.error("sendDocument failed for %s: %s", path, detail)
            return False
        return True

    def send_summary(self, text: str) -> bool:
        return all(self._post(part, silent=True, preview=False) for part in chunk(text))

    def _post(self, text: str, *, silent: bool, preview: bool) -> bool:
        self._space_out()
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
            "disable_notification": silent,
        }
        try:
            response = self.session.post(
                f"{API}/bot{self.token}/sendMessage",
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            self.failures.append(f"network error: {exc}")
            log.error("Telegram send failed: %s", exc)
            return False

        if response.status_code == 429:
            wait = _retry_after(response)
            log.warning("Telegram rate limited; waiting %.1fs then retrying once", wait)
            time.sleep(wait)
            return self._post(text, silent=silent, preview=preview)

        if not response.ok:
            detail = _describe(response)
            self.failures.append(detail)
            log.error("Telegram send failed: %s", detail)
            return False
        return True

    def _space_out(self) -> None:
        wait = self._last_send + SEND_SPACING - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_send = time.monotonic()

    def resolve_chat_id(self) -> list[tuple[str, str]]:
        """Chats that have messaged the bot — for first-time setup."""
        response = self.session.get(
            f"{API}/bot{self.token}/getUpdates", timeout=self.timeout
        )
        response.raise_for_status()
        found: list[tuple[str, str]] = []
        for update in response.json().get("result", []):
            message = (
                update.get("message")
                or update.get("channel_post")
                or update.get("edited_message")
                or {}
            )
            chat = message.get("chat") or {}
            if chat.get("id") is not None:
                name = chat.get("title") or chat.get("username") or chat.get("first_name", "")
                entry = (str(chat["id"]), str(name))
                if entry not in found:
                    found.append(entry)
        return found


def _retry_after(response: requests.Response) -> float:
    try:
        return min(float(response.json()["parameters"]["retry_after"]), 30.0)
    except (ValueError, KeyError, TypeError):
        return 3.0


def _describe(response: requests.Response) -> str:
    try:
        body = response.json()
        description = body.get("description", response.text[:200])
    except ValueError:
        description = response.text[:200]
    hints = {
        400: "bad request — usually malformed HTML in the message, or a wrong chat id",
        401: "the bot token is invalid",
        403: "the bot cannot message this chat — send it /start first, or re-add "
        "it to the group",
        404: "token not recognised by Telegram",
    }
    hint = hints.get(response.status_code, "")
    return f"HTTP {response.status_code}: {description}" + (f" ({hint})" if hint else "")
