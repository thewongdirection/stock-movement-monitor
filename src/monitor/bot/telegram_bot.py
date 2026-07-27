"""Telegram front end — long-polling `getUpdates` over the shared router.

Long polling rather than webhooks, because webhooks need a public HTTPS endpoint
and this project's whole premise is not running a server. The trade-off is that
the bot only answers commands while this process is running: the cron sends
alerts on its own, but interactive commands need `monitor bot` up. Run it on a
laptop when you want to change something, or keep it on a small always-on box.

Only the configured chat is served. An unknown chat gets one polite refusal —
the bot token is a bearer credential, and anyone who learns it could otherwise
edit your watchlist.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

from .commands import BotContext, CommandRouter, Reply

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
POLL_TIMEOUT = 50


class TelegramBot:
    def __init__(self, token: str, chat_id: str, ctx: BotContext):
        if not token or not chat_id:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are both required to run "
                "the bot. Use `monitor console` to try the commands without either."
            )
        self.token = token
        self.chat_id = str(chat_id)
        self.router = CommandRouter(ctx)
        self.session = requests.Session()
        self.offset: int | None = None

    # -- transport --------------------------------------------------------
    def _call(self, method: str, **payload) -> dict:
        response = self.session.post(
            f"{API}/bot{self.token}/{method}", json=payload, timeout=POLL_TIMEOUT + 15
        )
        response.raise_for_status()
        return response.json()

    def register_commands(self) -> None:
        """Populate Telegram's command menu so the UI offers autocomplete."""
        commands = [
            ("list", "Watched tickers with 14-day signal counts"),
            ("add", "Add a ticker to the watchlist"),
            ("remove", "Remove a ticker"),
            ("history", "Signals for a ticker in the last 14 days"),
            ("grade", "CAN SLIM scorecard and PDF"),
            ("levels", "Detection levels L1-L3, on or off"),
            ("config", "Current thresholds"),
            ("set", "Change a threshold"),
            ("explain", "What a setting does and its range"),
            ("reset", "Back to the committed defaults"),
            ("changes", "What has been changed from config.yaml"),
            ("status", "Health, last run, what's enabled"),
            ("help", "All commands"),
        ]
        try:
            self._call(
                "setMyCommands",
                commands=[{"command": c, "description": d} for c, d in commands],
            )
        except requests.RequestException as exc:
            log.warning("could not register the command menu: %s", exc)

    def send(self, reply: Reply) -> None:
        keyboard = None
        if reply.buttons:
            # Two per row reads well on a phone.
            rows, row = [], []
            for button in reply.buttons[:16]:
                row.append({"text": button.label, "callback_data": button.command[:60]})
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            keyboard = {"inline_keyboard": rows}

        for chunk in _split(reply.text):
            payload = {
                "chat_id": self.chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if keyboard and chunk is _split(reply.text)[-1]:
                payload["reply_markup"] = keyboard
            try:
                self._call("sendMessage", **payload)
            except requests.RequestException as exc:
                log.error("sendMessage failed: %s", exc)

        for path in reply.files:
            self._send_document(Path(path))

    def _send_document(self, path: Path) -> None:
        if not path.exists():
            log.warning("attachment missing: %s", path)
            return
        try:
            with path.open("rb") as handle:
                response = self.session.post(
                    f"{API}/bot{self.token}/sendDocument",
                    data={"chat_id": self.chat_id, "caption": path.name},
                    files={"document": (path.name, handle)},
                    timeout=120,
                )
            if not response.ok:
                log.error("sendDocument failed: %s", response.text[:200])
        except (requests.RequestException, OSError) as exc:
            log.error("could not upload %s: %s", path, exc)

    # -- loop -------------------------------------------------------------
    def poll_once(self) -> int:
        """One getUpdates cycle. Returns how many updates were handled."""
        params = {"timeout": POLL_TIMEOUT}
        if self.offset is not None:
            params["offset"] = self.offset
        try:
            payload = self._call("getUpdates", **params)
        except requests.RequestException as exc:
            log.warning("getUpdates failed: %s", exc)
            time.sleep(5)
            return 0

        updates = payload.get("result") or []
        for update in updates:
            self.offset = int(update["update_id"]) + 1
            self._dispatch(update)
        return len(updates)

    def _dispatch(self, update: dict) -> None:
        callback = update.get("callback_query")
        if callback:
            chat_id = str(((callback.get("message") or {}).get("chat") or {}).get("id", ""))
            text = callback.get("data") or ""
            try:
                self._call("answerCallbackQuery", callback_query_id=callback["id"])
            except requests.RequestException:
                pass
        else:
            message = update.get("message") or update.get("edited_message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = message.get("text") or ""

        if not text:
            return
        if chat_id != self.chat_id:
            log.warning("ignoring a message from unauthorised chat %s", chat_id)
            try:
                self._call(
                    "sendMessage",
                    chat_id=chat_id,
                    text="This bot is private to its configured chat.",
                )
            except requests.RequestException:
                pass
            return

        log.info("command: %s", text)
        self.send(self.router.handle(text))

    def run_forever(self) -> int:  # pragma: no cover - long-running loop
        self.register_commands()
        log.info("bot listening; send /help in Telegram")
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                log.info("stopping")
                return 0
            except Exception:  # noqa: BLE001 - keep the bot alive
                log.exception("unexpected error in the poll loop")
                time.sleep(5)


def _split(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            parts.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        parts.append(current)
    return parts
