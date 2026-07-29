"""Handing off to the `can-slim-grader` agent skill.

The skill is not a Python library and cannot be imported. It is a set of
instructions for an agent with market-data connectors — it reads the news for
the N letter, pulls 13F filings for I, and renders an HTML dashboard. None of
that runs unattended on a VPS at 10:05 on a Tuesday.

So the split is: the monitor computes what arithmetic can settle and ships it
inside the alert, and this module produces a brief you can paste into Claude to
get the full interactive grade — pre-loaded with the numbers already fetched, so
the skill starts from the data instead of re-gathering it.
"""

from __future__ import annotations

from pathlib import Path

from .grader import Grade, Scorecard

SKILL_NAME = "can-slim-grader"

#: Where the skill is normally installed. Checked in order.
SEARCH_PATHS = (
    Path.home() / ".claude" / "skills" / SKILL_NAME,
    Path(".claude") / "skills" / SKILL_NAME,
    Path("/workspace") / SKILL_NAME,
)


def locate() -> Path | None:
    """Find an installed copy of the skill, or None."""
    for candidate in SEARCH_PATHS:
        if (candidate / "SKILL.md").exists():
            return candidate
    return None


def status() -> str:
    found = locate()
    if found:
        return f"can-slim-grader skill found at {found}"
    return (
        "can-slim-grader skill is not installed — the monitor's computed scorecard "
        "still works; install the skill for the letters that need judgement"
    )


def brief(card: Scorecard, movement: str | None = None) -> str:
    """A paste-ready request for the full interactive grade.

    Includes what the monitor already measured, so the skill spends its effort on
    the letters that need judgement rather than re-deriving EPS growth.
    """
    lines = [
        f"Grade {card.ticker} against CAN SLIM using the can-slim-grader skill.",
        "",
    ]
    if movement:
        lines += [f"Context — this came up because: {movement}", ""]

    lines.append("The monitor already computed these letters from financial data:")
    for letter in card.letters:
        if letter.grade is Grade.UNKNOWN:
            continue
        lines.append(f"- {letter.key} ({letter.name}): {letter.grade.value.upper()}")
        lines += [f"    {item}" for item in letter.evidence if not item.startswith("Narrator:")]

    unknown = [letter for letter in card.letters if letter.grade is Grade.UNKNOWN]
    if unknown:
        lines += ["", "It could not measure these — please cover them properly:"]
        for letter in unknown:
            reason = letter.evidence[0] if letter.evidence else "no data"
            lines.append(f"- {letter.key} ({letter.name}): {reason}")

    lines += [
        "",
        f"Computed verdict was {card.verdict} at {card.percent:.0f}% over "
        f"{card.graded} measurable letters. Please confirm or correct it, and produce "
        f"the HTML dashboard.",
    ]
    return "\n".join(lines)
