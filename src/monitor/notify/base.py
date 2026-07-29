"""One message layout, rendered plain or as Telegram HTML.

There is exactly one `format_alert`. An earlier version of this project had the
console renderer keep its own copy of the layout, and when the interpretive
`read` line was added the console silently stopped showing it — so `--dry-run`,
the thing you use to check what will be sent, showed something different from
what was sent. Any renderer that needs a different *transport* gets a flag;
none of them get their own layout.
"""

from __future__ import annotations

import html
from typing import Protocol

from ..models import Alert, Severity, SourceIssue


class Notifier(Protocol):
    name: str

    def send(self, text: str) -> bool:
        """Deliver one message. False means it did not arrive."""


def _bold(text: str, as_html: bool) -> str:
    return f"<b>{html.escape(text)}</b>" if as_html else text


def _italic(text: str, as_html: bool) -> str:
    return f"<i>{html.escape(text)}</i>" if as_html else text


def _plain(text: str, as_html: bool) -> str:
    return html.escape(text) if as_html else text


def format_alert(alert: Alert, *, as_html: bool = False,
                 canslim: str | None = None) -> str:
    """Render one alert: what happened, what it means, and what it does not.

    The order is deliberate. Facts first, so the numbers are checkable. The
    read second, clearly marked as interpretation. Caveats last but never
    omitted — an alert that cannot say what it fails to prove is advice, and
    this tool does not give advice.
    """
    lines = [f"{alert.severity.icon} {_bold(alert.headline, as_html)}"]

    if alert.facts:
        lines.append("")
        lines += [f"• {_plain(fact, as_html)}" for fact in alert.facts]

    if alert.read:
        lines += ["", f"👉 {_plain(alert.read, as_html)}"]

    if canslim:
        lines += ["", f"📊 {_plain(canslim, as_html)}"]

    if alert.caveats:
        lines.append("")
        lines += [_italic(f"⚠ {caveat}", as_html) for caveat in alert.caveats]

    footer = f"{alert.occurred_at:%Y-%m-%d %H:%M %Z} · {alert.signal}"
    lines += ["", _italic(footer, as_html)]

    if alert.url:
        lines.append(
            f'<a href="{html.escape(alert.url, quote=True)}">source</a>'
            if as_html else alert.url
        )
    return "\n".join(lines)


def format_issues(issues: list[SourceIssue], *, as_html: bool = False) -> str:
    """Render the data-health block.

    Sent even when there are no alerts, because "we saw nothing" and "we could
    not see" are different messages and only one of them needs your attention.
    """
    if not issues:
        return ""
    head = _bold(f"Data source problems ({len(issues)})", as_html)
    body = [f"  {_plain(issue.line(), as_html)}" for issue in issues]
    tail = _italic(
        "Signals depending on these sources did not run — this is not a quiet market.",
        as_html,
    )
    return "\n".join(["⚠️ " + head, *body, "", tail])


def format_digest(alerts: list[Alert], *, as_html: bool = False) -> str:
    """A one-line-per-alert summary, for `/status` and for capped runs."""
    if not alerts:
        return "No alerts."
    return "\n".join(
        f"{a.severity.icon} {_plain(a.headline, as_html)}" for a in alerts
    )


class ConsoleNotifier:
    """Prints to stdout. Used by `--dry-run` and by the console bot.

    Renders through `format_alert` like every other channel, so what you see
    here is what Telegram would receive.
    """

    name = "console"

    def __init__(self, stream=None):
        import sys
        self.stream = stream or sys.stdout

    def send(self, text: str) -> bool:
        print(text, file=self.stream)
        print("─" * 72, file=self.stream)
        return True


def should_notify(alert: Alert, minimum: str) -> bool:
    return alert.severity.rank >= Severity(minimum).rank
