"""Optional Claude commentary on a scorecard.

Two letters resist arithmetic. **N** needs somebody to know whether there is a
genuinely new product, management team or industry condition. **I** needs a read
on whether the funds on the register are quality sponsors or a crowded trade.
Those are judgement calls, and this is where a model earns its place.

**The merge is deliberately asymmetric: the narrator may lower a computed grade
or fill an UNKNOWN one, and may never raise a computed grade.** The arithmetic
is checkable and the prose is not, so an LLM that can talk a FAIL into a PASS
turns a scorecard into a sales pitch. Downgrades are allowed because a model
that spots an accounting problem behind a clean-looking EPS line is adding real
information, and because the failure mode of being too cautious is losing an
opportunity rather than losing money.

The dependency is optional. Without `anthropic` installed, or without a key, the
narrator reports itself unavailable and the deterministic card still ships.
"""

from __future__ import annotations

import json
import logging
import os

from .grader import Grade, Scorecard

log = logging.getLogger("monitor.canslim.narrate")

MODEL = "claude-opus-5"

#: Letters the narrator may fill in when the monitor could not measure them.
SOFT_LETTERS = ("N", "I")

SYSTEM = """You are grading one stock against the CAN SLIM framework as published by \
William O'Neil. You are given a scorecard that was computed arithmetically from \
financial data, and your job is to add the judgement the arithmetic cannot make.

Rules you must follow:
- You may LOWER a letter's grade if the evidence warrants it, and you may fill in a \
letter marked "unknown". You may NEVER raise a grade that was computed from data.
- For the N letter, judge whether there is a genuinely new product, management team, \
or industry condition. For I, judge the quality of institutional sponsorship.
- Be concrete. Cite what you actually know about the company. If you do not know, \
say the letter remains unknown rather than guessing.
- This is decision support against a published framework. Never give personalised \
investment advice, never suggest an order size, never predict a price.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "letters": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "grade": {"type": "string", "enum": ["pass", "partial", "fail", "unknown"]},
                    "comment": {"type": "string"},
                },
                "required": ["grade", "comment"],
            },
        },
        "summary": {"type": "string", "description": "Two sentences on the setup as a whole."},
    },
    "required": ["letters", "summary"],
}


class NarratorUnavailable(Exception):
    """The narrator could not run. Never fatal — the computed card still ships."""


def narrate(card: Scorecard, *, model: str = MODEL, api_key: str | None = None,
            client=None) -> dict:
    """Ask Claude for commentary. Returns {'letters': {...}, 'summary': str}."""
    if client is None:
        try:
            import anthropic
        except ImportError as exc:
            raise NarratorUnavailable(
                "the 'anthropic' package is not installed — "
                "pip install -r requirements-narrator.txt"
            ) from exc
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise NarratorUnavailable("ANTHROPIC_API_KEY is not set")
        client = anthropic.Anthropic(api_key=key)

    prompt = _prompt(card)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:                        # noqa: BLE001 - degraded, not fatal
        raise NarratorUnavailable(f"{type(exc).__name__}: {exc}") from exc

    if getattr(response, "stop_reason", None) == "refusal":
        raise NarratorUnavailable("the model declined to answer")

    text = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NarratorUnavailable(f"response was not JSON: {exc}") from exc
    if not isinstance(payload.get("letters"), dict):
        raise NarratorUnavailable("response had no letters object")
    return payload


def merge(card: Scorecard, narration: dict, *, allow_downgrade: bool = True) -> list[str]:
    """Fold commentary into the card. Returns the changes made, for the audit trail.

    See the module docstring: fills unknowns, permits downgrades, refuses upgrades.
    """
    order = [Grade.FAIL, Grade.PARTIAL, Grade.PASS]
    changes: list[str] = []

    for letter in card.letters:
        entry = narration.get("letters", {}).get(letter.key)
        if not isinstance(entry, dict):
            continue
        comment = str(entry.get("comment", "")).strip()
        try:
            proposed = Grade(str(entry.get("grade", "unknown")).lower())
        except ValueError:
            continue

        if comment:
            letter.evidence.append(f"Narrator: {comment}")

        if proposed is Grade.UNKNOWN:
            continue
        if letter.grade is Grade.UNKNOWN:
            if letter.key not in SOFT_LETTERS:
                # Refuse to let prose invent an earnings figure the data did not have.
                letter.evidence.append(
                    f"Narrator proposed {proposed.value} but {letter.key} is a "
                    f"computed letter — left unmeasured."
                )
                continue
            letter.grade = proposed
            changes.append(f"{letter.key}: unknown → {proposed.value}")
        elif allow_downgrade and order.index(proposed) < order.index(letter.grade):
            changes.append(f"{letter.key}: {letter.grade.value} → {proposed.value} (downgrade)")
            letter.grade = proposed
        elif order.index(proposed) > order.index(letter.grade):
            letter.evidence.append(
                f"Narrator suggested {proposed.value}; computed grade "
                f"{letter.grade.value} kept — narration cannot raise a measured letter."
            )

    summary = str(narration.get("summary", "")).strip()
    if summary:
        card.notes.append(f"Narrator: {summary}")

    if changes:
        _rescore(card)
    return changes


def _rescore(card: Scorecard) -> None:
    from .grader import WEIGHTS, _verdict
    known = [letter for letter in card.letters if letter.grade is not Grade.UNKNOWN]
    card.score = sum(WEIGHTS[letter.key] * letter.grade.points for letter in known)
    card.max_score = sum(WEIGHTS[letter.key] for letter in known)
    card.graded = len(known)
    from .grader import Facts
    card.verdict, card.summary = _verdict(card.letters, Facts(ticker=card.ticker))


def _prompt(card: Scorecard) -> str:
    lines = [
        f"Ticker: {card.ticker}",
        f"Computed verdict: {card.verdict} ({card.score:.1f}/{card.max_score:.1f})",
        "",
        "Computed letters:",
    ]
    for letter in card.letters:
        lines.append(f"  {letter.key} ({letter.name}): {letter.grade.value}")
        lines += [f"      - {item}" for item in letter.evidence]
    if card.notes:
        lines += ["", "Data gaps:", *(f"  - {note}" for note in card.notes)]
    lines += [
        "",
        "Add your judgement. Fill in any letter marked unknown that you can genuinely "
        "assess (especially N and I). Lower any grade the evidence does not support. "
        "Do not raise a computed grade.",
    ]
    return "\n".join(lines)
