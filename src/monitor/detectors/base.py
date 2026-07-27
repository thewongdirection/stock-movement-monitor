"""Detector contract and shared helpers."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..models import Alert, Bar, InsiderTransaction, OptionTrade, Severity, Trade
from ..state import State


@dataclass
class Context:
    """Everything a detector needs for one ticker in one run."""

    ticker: str
    now: datetime
    settings: dict[str, Any]
    state: State
    cold_start_minutes: int

    #: Intraday bars, fetched once per ticker and shared by every detector.
    bars: list[Bar] = field(default_factory=list)
    #: Off-exchange / block prints, fetched once per ticker.
    prints: list[Trade] = field(default_factory=list)
    #: Options flow events, fetched once per ticker.
    option_trades: list[OptionTrade] = field(default_factory=list)
    #: Form 4 lines parsed for this ticker.
    insider_transactions: list[InsiderTransaction] = field(default_factory=list)
    #: Average daily volume, or None when no bars provider is configured.
    adv: float | None = None
    #: Print IDs already claimed this run. `block_trades` and `dark_pool` read
    #: the same off-exchange stream from Unusual Whales, so without this a
    #: single print could produce two alerts.
    claimed_prints: set[str] = field(default_factory=set)
    #: Non-fatal notes to surface in the run summary.
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        entry = f"{self.ticker}: {message}"
        if entry not in self.notes:
            self.notes.append(entry)


class Detector:
    name: str = ""
    level: str = ""
    #: Which provider role this detector needs ("bars", "trades", "flow", "insider").
    requires: str = ""

    def run(self, ctx: Context) -> list[Alert]:  # pragma: no cover - interface
        raise NotImplementedError

    # -- helpers shared by subclasses -------------------------------------
    def cooled_down(self, ctx: Context) -> bool:
        """False when the ticker is inside this detector's silence window."""
        return not ctx.state.in_cooldown(self.name, ctx.ticker, ctx.now)

    def finish(self, ctx: Context, alerts: list[Alert]) -> list[Alert]:
        """Apply the per-run cap, start the cooldown, and return what to send."""
        if not alerts:
            return []
        cap = int(ctx.settings.get("max_alerts_per_run", 5))
        alerts.sort(key=lambda a: (-a.severity.rank, a.occurred_at))
        kept = alerts[:cap]
        if len(alerts) > cap:
            ctx.note(
                f"{len(alerts) - cap} further {self.name} alert(s) suppressed by "
                f"max_alerts_per_run={cap}"
            )
        ctx.state.start_cooldown(
            self.name, ctx.ticker, ctx.now, int(ctx.settings.get("cooldown_minutes", 0))
        )
        return kept


def escalate(base: Severity, *conditions: bool) -> Severity:
    """Bump severity one step per satisfied condition, capped at HIGH."""
    rank = base.rank + sum(1 for c in conditions if c)
    return [Severity.LOW, Severity.MEDIUM, Severity.HIGH][min(rank, 2)]


def esc(value: object) -> str:
    """Escape provider-supplied text before it lands in an HTML message.

    Alert lines are assembled with our own markup, so escaping has to happen
    here at the point a third-party string enters — an insider's name or a
    venue code containing `&` would otherwise break Telegram's parser.
    """
    return html.escape(str(value if value is not None else ""), quote=False)


def money(value: float | None) -> str:
    if value is None:
        return "n/a"
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= size:
            return f"${value / size:,.2f}{unit}"
    return f"${value:,.0f}"


def shares(value: float) -> str:
    if abs(value) >= 1e6:
        return f"{value / 1e6:,.2f}M"
    if abs(value) >= 1e3:
        return f"{value / 1e3:,.1f}K"
    return f"{value:,.0f}"


def combine_ok(mode: str, *tests: bool | None) -> bool:
    """Apply an any/all rule, ignoring tests that were disabled (None)."""
    active = [t for t in tests if t is not None]
    if not active:
        return False
    return all(active) if mode == "all" else any(active)
