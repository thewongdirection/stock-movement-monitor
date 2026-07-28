"""Notifier interface, plus a console implementation for dry runs."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from ..models import Alert, Severity

SEVERITY_ICON = {
    Severity.LOW: "🔵",
    Severity.MEDIUM: "🟠",
    Severity.HIGH: "🔴",
}

DETECTOR_LABEL = {
    "volume_anomaly": "Unusual volume",
    "option_volume": "Unusual option volume",
    "block_trades": "Block trade",
    "dark_pool": "Dark pool",
    "options_flow": "Options flow",
    "insider_trades": "Insider (Form 4)",
}


class Notifier(Protocol):
    def send(self, alert: Alert, files: Sequence[Path] | None = None) -> bool: ...

    def send_summary(self, text: str) -> bool: ...


class ConsoleNotifier:
    """Prints instead of sending — what `--dry-run` uses."""

    def __init__(self) -> None:
        self.sent: list[Alert] = []
        self.attachments: list[Path] = []

    def send(self, alert: Alert, files: Sequence[Path] | None = None) -> bool:
        # Rendered through format_alert, not a second copy of the layout: a
        # preview that can drift from the real message is worse than no preview.
        # (It already had — the `read` line was missing here for a while.)
        print()
        for line in _strip_tags(format_alert(alert)).splitlines():
            print(f"   {line}" if line else "")
        for path in files or []:
            print(f"   attachment: {path}")
            self.attachments.append(Path(path))
        self.sent.append(alert)
        return True

    def send_summary(self, text: str) -> bool:
        print(f"\n--- {_strip_tags(text)}")
        return True


def _strip_tags(text: str) -> str:
    import html
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", text))


def format_alert(alert: Alert) -> str:
    """Render an alert as a Telegram-flavoured HTML message."""
    icon = SEVERITY_ICON[alert.severity]
    label = DETECTOR_LABEL.get(alert.detector, alert.detector)
    head = (
        f"{icon} <b>{alert.ticker}</b> · {label}\n"
        f"<b>{alert.headline}</b>\n\n"
    )
    body = "\n".join(alert.lines)
    # Set apart from the evidence above it. A reader should be able to tell at a
    # glance which lines are measurements and which line is the interpretation.
    read = f"\n\n➤ <b>{alert.read}</b>" if alert.read else ""
    tail = f'\n\n<a href="{alert.url}">View filing</a>' if alert.url else ""
    stamp = f"\n\n<i>{alert.occurred_at.astimezone():%Y-%m-%d %H:%M:%S %Z}</i>"
    return head + body + read + tail + stamp


def chunk(text: str, limit: int = 3800) -> Sequence[str]:
    """Split on line boundaries to stay under Telegram's 4096-char cap."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            parts.append(current)
            current = ""
        current += (("\n" if current else "") + line)
    if current:
        parts.append(current)
    return parts
