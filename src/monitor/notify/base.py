"""Notifier interface, plus a console implementation for dry runs."""

from __future__ import annotations

from typing import Protocol, Sequence

from ..models import Alert, Severity

SEVERITY_ICON = {
    Severity.LOW: "🔵",
    Severity.MEDIUM: "🟠",
    Severity.HIGH: "🔴",
}

DETECTOR_LABEL = {
    "volume_anomaly": "Unusual volume",
    "block_trades": "Block trade",
    "dark_pool": "Dark pool",
    "options_flow": "Options flow",
    "insider_trades": "Insider (Form 4)",
}


class Notifier(Protocol):
    def send(self, alert: Alert) -> bool: ...

    def send_summary(self, text: str) -> bool: ...


class ConsoleNotifier:
    """Prints instead of sending — what `--dry-run` uses."""

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def send(self, alert: Alert) -> bool:
        icon = SEVERITY_ICON[alert.severity]
        label = DETECTOR_LABEL.get(alert.detector, alert.detector)
        print(f"\n{icon} [{alert.ticker}] {label}: {alert.headline}")
        for line in alert.lines:
            print(f"   {_strip_tags(line)}")
        if alert.url:
            print(f"   {alert.url}")
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
    tail = f'\n\n<a href="{alert.url}">View filing</a>' if alert.url else ""
    stamp = f"\n\n<i>{alert.occurred_at.astimezone():%Y-%m-%d %H:%M:%S %Z}</i>"
    return head + body + tail + stamp


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
