"""Delivery channels, and the one message layout they all share."""

from __future__ import annotations

import os
from typing import Mapping

from ..config import Config
from .base import (ConsoleNotifier, Notifier, format_alert, format_digest,
                   format_issues, should_notify)
from .telegram import TelegramNotifier, split_message

__all__ = [
    "ConsoleNotifier", "Notifier", "TelegramNotifier", "build",
    "format_alert", "format_digest", "format_issues", "should_notify", "split_message",
]


def build(config: Config, env: Mapping[str, str] | None = None,
          force_console: bool = False) -> tuple[Notifier, bool]:
    """Return the configured channel and whether it wants HTML.

    Raises `NotConfigured` for missing Telegram credentials rather than falling
    back to the console: a silent downgrade means alerts print to a log on a
    server nobody is reading, which looks exactly like no alerts.
    """
    env = env if env is not None else os.environ
    if force_console or config.get("notify.channel") == "console":
        return ConsoleNotifier(), False
    return TelegramNotifier(
        env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    ), True
