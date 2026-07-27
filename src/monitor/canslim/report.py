"""Render a Grade into the skill's own HTML dashboard, then a PDF.

The template is driven entirely by a ``CONFIG`` object — the skill is explicit
that this is the only thing you edit and that the DOM must not be hand-touched.
So this replaces exactly that object and leaves the rest of the file alone.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .grader import Grade
from .skill import SkillPaths, export_pdf

log = logging.getLogger(__name__)

CONFIG_START = re.compile(r"const\s+CONFIG\s*=\s*\{")

#: Two disclaimers, because the two grading passes are not equivalent and the
#: report must not imply otherwise.
DISCLAIMER_COMPUTED = (
    "Informational decision support, not investment advice; not a financial "
    "advisor. Figures are as-of the timestamp; RS is a relative-strength proxy "
    "(vs SPY), not a full-market 1-99 rating; fundamentals may lag. Letters "
    "were scored programmatically against the can-slim-grader rubric — the "
    "qualitative halves of N and I are flagged rather than judged. Nothing "
    "here is an order."
)

DISCLAIMER_NARRATED = (
    "Informational decision support, not investment advice; not a financial "
    "advisor. Figures are as-of the timestamp; RS is a relative-strength proxy "
    "(vs SPY), not a full-market 1-99 rating; fundamentals may lag. The "
    "measurable letters were scored programmatically against the "
    "can-slim-grader rubric; the per-letter commentary and the judgement "
    "letters (N's new driver, I's sponsorship quality) were written by Claude "
    "and may contain errors — the Notes section lists every score it set. "
    "Nothing here is an order."
)


@dataclass
class Report:
    grade: Grade
    html_path: Path
    pdf_path: Path | None
    pdf_error: str | None = None

    @property
    def has_pdf(self) -> bool:
        return self.pdf_path is not None and self.pdf_path.exists()


def build_report(
    grade: Grade,
    skill: SkillPaths,
    out_dir: str | Path,
    *,
    want_pdf: bool = True,
) -> Report:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    template = skill.template.read_text()
    html = _replace_config(template, _config_for(grade))
    html_path = out / f"{grade.ticker}-canslim.html"
    html_path.write_text(html)

    pdf_path: Path | None = None
    pdf_error: str | None = None
    if want_pdf:
        candidate = out / f"{grade.ticker}-canslim.pdf"
        ok, detail = export_pdf(skill, html_path, candidate)
        if ok:
            pdf_path = candidate
        else:
            pdf_error = detail
            log.warning("PDF export failed for %s: %s", grade.ticker, detail)

    return Report(grade=grade, html_path=html_path, pdf_path=pdf_path, pdf_error=pdf_error)


def _replace_config(template: str, config: dict) -> str:
    """Swap the template's CONFIG literal for ours, brace-matching to find its end."""
    match = CONFIG_START.search(template)
    if not match:
        raise ValueError(
            "the skill's evaluation_template.html has no `const CONFIG = {` block — "
            "it may have been restructured upstream"
        )
    start = template.index("{", match.start())
    depth = 0
    end = start
    in_string: str | None = None
    escaped = False
    for i in range(start, len(template)):
        char = template[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char in "\"'`":
            in_string = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    else:  # pragma: no cover - defensive
        raise ValueError("unterminated CONFIG object in the skill template")

    rendered = json.dumps(config, indent=2, ensure_ascii=False)
    return template[:start] + rendered + template[end + 1 :]


def _config_for(grade: Grade) -> dict:
    tech = grade.technicals
    market = tech.get("market") or {}

    chart = []
    if tech.get("rs_blend_pct") is not None:
        rs = tech["rs_blend_pct"]
        chart.append(
            {
                "l": "Relative strength vs SPY",
                "v": f"{rs:+.1f}pp",
                "tone": "pos" if rs > 0 else "neg",
            }
        )
    if tech.get("off_high_pct") is not None:
        off = tech["off_high_pct"]
        chart.append(
            {
                "l": "% off 52-wk high",
                "v": f"{off:.1f}%",
                "tone": "pos" if off <= 5 else ("neg" if off > 15 else ""),
            }
        )
    if tech.get("base_depth_pct") is not None:
        depth = tech["base_depth_pct"]
        label = f"{depth:.0f}% deep"
        if tech.get("base_length_weeks"):
            label += f", ~{tech['base_length_weeks']} wks"
        if tech.get("wide_loose"):
            label += " (wide/loose)"
        chart.append({"l": "Base (heuristic)", "v": label})
    if tech.get("pivot"):
        chart.append({"l": "Pivot buy point", "v": f"~${tech['pivot']:,.2f}"})
    if tech.get("breakout_vol_pct") is not None:
        vol = tech["breakout_vol_pct"]
        chart.append(
            {
                "l": "Latest volume vs avg",
                "v": f"{vol:+.0f}%",
                "tone": "pos" if vol >= 40 else ("neg" if vol < 0 else ""),
            }
        )
    if market.get("label"):
        chart.append({"l": "Market direction (M)", "v": market["label"]})

    buy_items = []
    if tech.get("pivot") and grade.verdict == "BUY-RANGE":
        buy_items.append(
            f"Pivot buy point ~${tech['pivot']:,.2f}; do not chase more than 5% past it."
        )
    buy_items.append(grade.stop_note)
    buy_items.append(
        "Sell signals to watch: a climax run or exhaustion gap, relative strength "
        "breaking down, weeks of closes below the 10-week line, or two quarters of "
        "earnings deceleration."
    )

    sources = [
        {"label": "can-slim-grader methodology", "url": "https://github.com/thewongdirection/can-slim-grader"},
        {"label": "SEC EDGAR", "url": "https://www.sec.gov/edgar/searchedgar/companysearch"},
    ]

    notes = ""
    if grade.warnings:
        notes = " Data gaps: " + "; ".join(grade.warnings[:4]) + "."

    sources = list(sources)
    for source in grade.narrator_sources[:8]:
        if source.get("url"):
            sources.append(
                {"label": str(source.get("label") or source["url"])[:80], "url": str(source["url"])}
            )

    # An audit line per narrator score change, so a reader can see exactly what
    # the model decided versus what the rubric computed.
    if grade.narrator_notes:
        buy_items.extend(f"Note: {note}" for note in grade.narrator_notes[:6])

    return {
        "ticker": grade.ticker,
        "company": grade.company,
        "price": f"${grade.price:,.2f}" if grade.price else "n/a",
        "asOf": grade.as_of,
        "dataSource": grade.data_sources
        + (" — letters narrated by Claude" if grade.narrated else " — letters scored programmatically"),
        "verdict": {
            "label": grade.verdict,
            "tone": grade.tone,
            "scoreText": grade.score_text,
            "summary": grade.summary + notes,
            "plan": f"Buy point: {grade.entry} | Stop: {grade.stop}",
        },
        "entryStop": {
            "entry": grade.entry,
            "entryNote": grade.entry_note,
            "stop": grade.stop,
            "stopNote": grade.stop_note,
        },
        "letters": [
            {
                "key": letter.key,
                "name": letter.name,
                "score": letter.template_score,
                "threshold": letter.threshold,
                "actual": letter.actual
                + ("  [ungraded — see read]" if letter.score == "unknown" else ""),
                "read": letter.read,
            }
            for letter in grade.letters
        ],
        "chart": chart,
        "essentials": [{"l": label, "v": value} for label, value in grade.essentials],
        "buyPlan": {
            "text": (
                "If the setup is valid: buy at the pivot and size the position so "
                "the stop below is your maximum loss."
            ),
            "items": buy_items,
        },
        "disclaimer": DISCLAIMER_NARRATED if grade.narrated else DISCLAIMER_COMPUTED,
        "sources": sources,
    }
