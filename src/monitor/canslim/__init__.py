"""CAN SLIM grading, wired to the monitor's cache and configuration.

Fundamentals move once a quarter. Fetching them on every hourly run would be six
wasted API calls per ticker per hour to learn that last quarter's EPS is still
last quarter's EPS, so grades are cached for `canslim.cache_hours` — with one
exception: an explicit `/grade` from chat always refetches, because the reason
somebody asks by hand is usually that they think something changed.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from ..config import Config
from ..store import Store
from .fundamentals import gather
from .grader import Facts, Grade, Letter, Scorecard, grade
from .narrate import NarratorUnavailable, merge, narrate
from .report import compact, render
from .skill import brief, locate, status

log = logging.getLogger("monitor.canslim")

__all__ = [
    "CanSlim", "Facts", "Grade", "Letter", "Scorecard", "grade", "gather",
    "render", "compact", "brief", "locate", "status", "narrate", "merge",
    "NarratorUnavailable",
]


class CanSlim:
    """Produces scorecards, cached, with the narrator applied if configured."""

    def __init__(self, config: Config, store: Store, fmp=None, client=None):
        self.config = config
        self.store = store
        self.fmp = fmp
        self.client = client

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("canslim.enabled"))

    def card(self, ticker: str, *, fresh: bool = False) -> Scorecard | None:
        """A scorecard for one ticker, or None if grading is off or unavailable."""
        if not self.enabled:
            return None
        ticker = ticker.upper()

        if not fresh:
            cached = self.store.read_canslim(ticker, self.config.get("canslim.cache_hours"))
            if cached:
                return _from_dict(cached)

        if self.fmp is None:
            log.info("canslim: no fundamentals source configured")
            return None

        facts = gather(ticker, self.fmp)
        card = grade(facts)

        mode = self.config.get("canslim.narrator")
        if mode in ("llm", "hybrid"):
            try:
                narration = narrate(
                    card, model=self.config.get("canslim.model"), client=self.client
                )
                changes = merge(card, narration, allow_downgrade=(mode == "hybrid"))
                if changes:
                    card.notes.append("Narrator adjustments: " + "; ".join(changes))
            except NarratorUnavailable as exc:
                card.notes.append(f"Narrator unavailable — {exc}. Computed grades stand.")

        self.store.cache_canslim(ticker, _to_dict(card))
        return card

    def line(self, ticker: str, *, fresh: bool = False) -> str | None:
        """The single line attached to an alert."""
        card = self.card(ticker, fresh=fresh)
        return card.one_liner() if card else None

    def should_attach(self, severity_rank: int) -> bool:
        from ..models import Severity
        if not self.enabled or not self.config.get("canslim.attach_to_alerts"):
            return False
        return severity_rank >= Severity(self.config.get("canslim.min_severity")).rank


def _to_dict(card: Scorecard) -> dict[str, Any]:
    payload = asdict(card)
    payload["letters"] = [
        {"key": letter.key, "grade": letter.grade.value, "evidence": letter.evidence}
        for letter in card.letters
    ]
    return payload


def _from_dict(payload: dict[str, Any]) -> Scorecard:
    return Scorecard(
        ticker=payload["ticker"],
        letters=[
            Letter(key=row["key"], grade=Grade(row["grade"]), evidence=list(row["evidence"]))
            for row in payload["letters"]
        ],
        verdict=payload["verdict"],
        summary=payload["summary"],
        score=payload["score"],
        max_score=payload["max_score"],
        graded=payload["graded"],
        notes=list(payload.get("notes", [])),
    )
