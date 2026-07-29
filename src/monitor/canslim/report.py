"""Rendering a scorecard for chat.

The coverage line is not decoration. A card that grades five of seven letters
and prints a confident percentage invites you to read it as a verdict on the
whole company, and the two letters most likely to be missing — institutional
sponsorship, and the story half of N — are exactly the ones a machine cannot
reach. So the card always says how much of itself it measured.
"""

from __future__ import annotations

import html

from .grader import Grade, Scorecard


def render(card: Scorecard, *, as_html: bool = False, verbose: bool = True) -> str:
    def esc(text: str) -> str:
        return html.escape(text) if as_html else text

    def bold(text: str) -> str:
        return f"<b>{html.escape(text)}</b>" if as_html else text

    def italic(text: str) -> str:
        return f"<i>{html.escape(text)}</i>" if as_html else text

    lines = [
        f"📊 {bold(f'{card.ticker} — CAN SLIM {card.letter_grade}')} "
        f"({card.score:.1f}/{card.max_score:.1f}, {card.percent:.0f}%)",
        f"{bold(card.verdict)} — {esc(card.summary)}",
        "",
    ]

    for letter in card.letters:
        head = f"{letter.grade.icon} {bold(letter.key)} {esc(letter.name)}"
        if letter.grade is Grade.UNKNOWN:
            head += esc(" — not measurable")
        lines.append(head)
        if verbose:
            lines += [f"    {esc(item)}" for item in letter.evidence]

    if card.graded < 7:
        lines += ["", italic(
            f"⚠ {card.graded} of 7 letters were measurable from the monitor's data "
            f"sources; the rest are excluded from the score rather than counted as "
            f"failures. Run the can-slim-grader skill for the full picture."
        )]
    for note in card.notes:
        lines.append(italic(f"· {esc(note)}"))

    lines += ["", italic(
        "Decision support against a published framework, not investment advice."
    )]
    return "\n".join(lines)


def compact(card: Scorecard) -> str:
    """The letter strip, e.g. 'C✅ A✅ N🟡 S✅ L✅ I❔ M✅'."""
    return " ".join(f"{letter.key}{letter.grade.icon}" for letter in card.letters)
