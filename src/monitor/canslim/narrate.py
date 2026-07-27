"""LLM narrator — the judgement half of the CAN SLIM grade.

The deterministic pass in `grader.py` computes everything the rubric can measure:
EPS growth, ROE, relative strength, base depth, distribution days. What it
cannot do is the part the skill was written for an agent to do — judge whether
there is a genuine **new** product, management change or industry condition
behind **N**, and whether the institutions holding the name are sponsors worth
following for **I**. Those need current information and judgement.

This module hands those to Claude and merges the answer back.

**The guardrail matters more than the feature.** An LLM given a scorecard will
happily rewrite the arithmetic. So the merge is asymmetric:

* The narrator always supplies the per-letter `read` prose — that is its job.
* It may change a **score** only for a letter the deterministic pass marked
  `unknown` (it has data we didn't), or for **N** and **I** (the two judgement
  letters). A proposed score for C, A, S, L or M is *rejected and logged* —
  those are computed from numbers and are not up for reinterpretation.

Every applied and rejected override is recorded on the grade, so the report can
show exactly what the model changed.

Two phases, because they want different tools:

1. **Research** (optional, needs web search) — gathers the "new" story and any
   recent sponsorship news. Skipped when `research` is off.
2. **Grade** — structured output against a JSON schema, no tools, so the shape
   is guaranteed rather than hoped for.

Cost control: the methodology document is ~4.5k tokens and identical on every
call, so it sits behind a cache breakpoint in the system prompt. Combined with
the once-per-ticker-per-day grade cache in `service.py`, a busy watchlist pays
for one grade per name per day with the methodology served from cache.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .grader import (
    FAIL,
    PARTIAL,
    PASS,
    UNKNOWN,
    Grade,
    LetterScore,
    _entry_stop,
    _verdict,
)
from .skill import SkillPaths

log = logging.getLogger(__name__)

#: Per the claude-api skill: always this model unless the operator names another.
DEFAULT_MODEL = "claude-opus-5"

#: Letters whose score the narrator is allowed to set outright. N's "new" story
#: and I's sponsorship quality are the two the numbers genuinely cannot settle.
JUDGEMENT_LETTERS = frozenset({"N", "I"})

VALID_SCORES = (PASS, PARTIAL, FAIL, UNKNOWN)

#: Server-side web search. The dated variant is the one Opus 5 supports.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 6}

#: Structured-output schema. Every object needs `required` on all keys and
#: `additionalProperties: false` — that is what makes the shape guaranteed.
GRADE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "letters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "enum": ["C", "A", "N", "S", "L", "I", "M"]},
                    "read": {
                        "type": "string",
                        "description": (
                            "Two or three sentences on this letter, in CAN SLIM terms "
                            "only, citing the actual figures supplied."
                        ),
                    },
                    "score": {
                        "type": "string",
                        "enum": ["pass", "partial", "fail", "unknown", "unchanged"],
                        "description": (
                            "Use 'unchanged' unless you are correcting a letter marked "
                            "unknown, or scoring N or I. Any other proposal is rejected."
                        ),
                    },
                    "score_rationale": {
                        "type": "string",
                        "description": "Why you changed the score, or '' if unchanged.",
                    },
                },
                "required": ["key", "read", "score", "score_rationale"],
                "additionalProperties": False,
            },
        },
        "summary": {
            "type": "string",
            "description": "One or two sentences: the verdict and the letters driving it.",
        },
        "new_story": {
            "type": "string",
            "description": (
                "The 'new' half of N: a new product, management change or industry "
                "condition, with its date. Say plainly if there isn't one."
            ),
        },
        "sponsorship_note": {
            "type": "string",
            "description": (
                "The quality half of I: who owns it and whether they are sponsors "
                "worth following. Say plainly if unknown."
            ),
        },
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["label", "url"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["letters", "summary", "new_story", "sponsorship_note", "sources"],
    "additionalProperties": False,
}

SYSTEM_ROLE = """\
You grade a single stock against the CAN SLIM model, following the methodology \
supplied below. You are completing a grade whose measurable parts have already \
been computed for you.

What you are for:
- Write the per-letter `read`: two or three sentences each, in CAN SLIM concepts \
only (letters, bases, pivots, relative strength, new highs, accumulation, \
leadership, sponsorship, market direction). Cite the actual figures given. No \
generic macro commentary, no analyst price targets, no "good company" language.
- Judge the two letters the numbers cannot settle: N's "new" driver (a new \
product, management change or industry condition) and I's sponsorship quality \
(whether the holders are institutions worth following).

Rules you must follow:
- The computed figures are facts. Do not restate them differently, recompute \
them, or argue with them.
- Set `score` to "unchanged" for every letter except one already marked \
`unknown`, or N, or I. A score you propose for C, A, S, L or M will be discarded.
- Where you lack evidence, say so plainly in the `read`. An honest "no new \
driver found" is worth more than a manufactured one.
- Be concise. Two or three sentences per letter. No preamble, no restating the \
task, no closing summary beyond the `summary` field.
- This is decision support, never advice, and never an order.\
"""

RESEARCH_PROMPT = """\
Research {ticker} ({company}) for two specific CAN SLIM inputs. Today is {today}.

1. **N — the "new" driver.** Is there a genuinely new product, service, \
management change, or industry condition in roughly the last 12 months? Give the \
date. A routine earnings beat is not a "new" driver.

2. **I — sponsorship quality.** Which institutions hold it, and is ownership \
rising or falling recently? Are these funds with a track record worth following, \
or is it index-tracking bulk?

Report only what you find, with dates and sources. If you find nothing for \
either, say so — do not fill the gap with speculation. Keep it under 300 words.\
"""


@dataclass
class NarratorConfig:
    enabled: bool = False
    model: str = DEFAULT_MODEL
    effort: str = "medium"
    research: bool = True
    max_tokens: int = 16000
    #: Server-side refusal fallback. Opus 5's classifiers can decline a request;
    #: "default" routes by category so we don't pin a model that later retires.
    use_fallbacks: bool = True
    timeout: int = 300


@dataclass
class NarratorResult:
    ok: bool
    summary: str = ""
    reads: dict[str, str] = field(default_factory=dict)
    applied_scores: dict[str, str] = field(default_factory=dict)
    rejected_scores: dict[str, str] = field(default_factory=dict)
    new_story: str = ""
    sponsorship_note: str = ""
    sources: list[dict[str, str]] = field(default_factory=list)
    research: str = ""
    notes: list[str] = field(default_factory=list)
    skipped: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


class Narrator:
    """Wraps the Claude API calls. Never raises into the caller's run."""

    def __init__(self, config: NarratorConfig, skill: SkillPaths):
        self.config = config
        self.skill = skill
        self._client: Any | None = None
        self._methodology: str | None = None

    # -- setup ------------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "narrator disabled in config (canslim.narrator: llm to enable)"
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "the `anthropic` package is not installed (pip install anthropic)"
        # An unset ANTHROPIC_API_KEY does not mean there are no credentials —
        # the SDK also resolves ANTHROPIC_AUTH_TOKEN and `ant auth login`
        # profiles, so a bare client() may well work. Only report a hard miss.
        return True, ""

    def _client_or_none(self) -> Any | None:
        if self._client is not None:
            return self._client
        try:
            import anthropic

            self._client = anthropic.Anthropic(timeout=self.config.timeout)
        except Exception as exc:  # noqa: BLE001 - credential resolution varies
            log.warning("could not construct an Anthropic client: %s", exc)
            return None
        return self._client

    def _methodology_text(self) -> str:
        if self._methodology is None:
            try:
                self._methodology = self.skill.methodology.read_text()
            except OSError:
                self._methodology = ""
        return self._methodology

    def _system_blocks(self) -> list[dict[str, Any]]:
        """Stable prefix, cached. Volatile per-ticker data goes in messages."""
        blocks: list[dict[str, Any]] = [{"type": "text", "text": SYSTEM_ROLE}]
        methodology = self._methodology_text()
        if methodology:
            blocks.append(
                {
                    "type": "text",
                    "text": "# CAN SLIM methodology\n\n" + methodology,
                }
            )
        # Breakpoint on the last stable block: this prefix is byte-identical on
        # every grade, so each subsequent ticker reads it from cache.
        blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        return blocks

    # -- phase 1: research ------------------------------------------------
    def research(self, grade: Grade, today: str) -> tuple[str, list[str]]:
        """Web-search the two judgement inputs. Returns (digest, notes)."""
        client = self._client_or_none()
        if client is None:
            return "", ["research skipped: no Anthropic client"]

        prompt = RESEARCH_PROMPT.format(
            ticker=grade.ticker, company=grade.company or grade.ticker, today=today
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        notes: list[str] = []

        # Server-side tools can stop with pause_turn when the tool loop hits its
        # iteration cap. Resume by re-sending; bounded so a stuck turn can't spin.
        for attempt in range(4):
            try:
                response = client.messages.create(
                    model=self.config.model,
                    max_tokens=4096,
                    system=self._system_blocks(),
                    thinking={"type": "adaptive"},
                    output_config={"effort": self.config.effort},
                    tools=[WEB_SEARCH_TOOL],
                    messages=messages,
                )
            except Exception as exc:  # noqa: BLE001 - research is a nicety
                return "", [f"research failed ({_describe(exc)})"]

            if response.stop_reason == "refusal":
                return "", ["research declined by safety classifiers"]

            if response.stop_reason == "pause_turn":
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": response.content},
                ]
                notes.append(f"research resumed after pause_turn (round {attempt + 1})")
                continue

            text = "\n".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
            return text, notes

        return "", notes + ["research abandoned: still paused after 4 rounds"]

    # -- phase 2: grade ---------------------------------------------------
    def narrate(self, grade: Grade, today: str, research: str = "") -> NarratorResult:
        client = self._client_or_none()
        if client is None:
            return NarratorResult(ok=False, skipped="no Anthropic client available")

        payload = _facts_payload(grade, research, today)
        request: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": self._system_blocks(),
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": self.config.effort,
                "format": {"type": "json_schema", "schema": GRADE_SCHEMA},
            },
            "messages": [{"role": "user", "content": payload}],
        }

        notes: list[str] = []
        if self.config.use_fallbacks:
            request["betas"] = ["server-side-fallback-2026-07-01"]
            request["fallbacks"] = "default"

        response = None
        for use_fallbacks in (self.config.use_fallbacks, False):
            attempt = dict(request)
            if not use_fallbacks:
                attempt.pop("betas", None)
                attempt.pop("fallbacks", None)
            try:
                caller = (
                    client.beta.messages.create
                    if "betas" in attempt
                    else client.messages.create
                )
                response = caller(**attempt)
                break
            except Exception as exc:  # noqa: BLE001 - classified below
                message = str(exc)
                retryable_beta = (
                    use_fallbacks
                    and _is_bad_request(exc)
                    and ("fallback" in message.lower() or "beta" in message.lower())
                )
                if retryable_beta:
                    # The beta flag moved or isn't enabled for this key. Losing
                    # grading over an optional resilience feature would be worse
                    # than losing the feature.
                    notes.append(
                        "refusal fallbacks unavailable for this key; retried without them"
                    )
                    continue
                return NarratorResult(ok=False, skipped=_describe(exc), notes=notes)

        if response is None:
            return NarratorResult(ok=False, skipped="no response from the API", notes=notes)

        # Check stop_reason before touching content: on a refusal, `content` is
        # empty (pre-output) or a partial that must not be read as complete.
        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            return NarratorResult(
                ok=False,
                skipped=f"declined by safety classifiers ({category or 'no category'})",
                notes=notes,
            )
        if response.stop_reason == "max_tokens":
            notes.append(
                f"hit max_tokens={self.config.max_tokens}; the JSON may be truncated"
            )

        text = "".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        if not text:
            return NarratorResult(ok=False, skipped="empty response", notes=notes)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return NarratorResult(
                ok=False, skipped=f"response was not valid JSON ({exc})", notes=notes
            )

        result = _merge(grade, data)
        result.research = research
        result.notes = notes + result.notes
        result.usage = _usage(response)
        return result


# --------------------------------------------------------------------------
def apply(
    grade: Grade,
    config: NarratorConfig,
    skill: SkillPaths,
    *,
    today: str,
) -> tuple[Grade, NarratorResult]:
    """Narrate a grade in place-ish, returning the updated grade and the result.

    On any failure the original grade is returned untouched — a narrator problem
    must never cost you the deterministic scorecard.
    """
    narrator = Narrator(config, skill)
    ok, reason = narrator.available()
    if not ok:
        return grade, NarratorResult(ok=False, skipped=reason)

    research = ""
    research_notes: list[str] = []
    if config.research:
        research, research_notes = narrator.research(grade, today)

    result = narrator.narrate(grade, today, research=research)
    result.notes = research_notes + result.notes
    if not result.ok:
        return grade, result

    return _rebuild(grade, result), result


def _rebuild(grade: Grade, result: NarratorResult) -> Grade:
    """Fold the narrator's prose and permitted score changes into the grade.

    Scores may have moved, so the verdict and the entry/stop band are re-derived
    rather than carried over — otherwise a letter upgrade could leave a stale
    AVOID on the report.
    """
    letters = [
        LetterScore(
            key=letter.key,
            score=result.applied_scores.get(letter.key, letter.score),
            threshold=letter.threshold,
            actual=letter.actual,
            read=result.reads.get(letter.key) or letter.read,
        )
        for letter in grade.letters
    ]
    grade.letters = letters

    verdict, tone, summary = _verdict(letters, grade.technicals)
    grade.verdict = verdict
    grade.tone = tone
    # The narrator's summary is the better prose; keep the deterministic one only
    # if it declined to write one.
    grade.summary = result.summary.strip() or summary

    market = grade.technicals.get("market") or {}
    entry, entry_note, stop, stop_note = _entry_stop(
        verdict, grade.technicals, market, grade.price
    )
    grade.entry, grade.entry_note = entry, entry_note
    grade.stop, grade.stop_note = stop, stop_note

    grade.narrated = True
    grade.narrator_notes = _audit_notes(result)
    if result.new_story:
        grade.narrator_notes.append(f"N — new driver: {result.new_story}")
    if result.sponsorship_note:
        grade.narrator_notes.append(f"I — sponsorship: {result.sponsorship_note}")
    grade.narrator_sources = list(result.sources)
    grade.data_sources += " + Claude narration"
    return grade


def _audit_notes(result: NarratorResult) -> list[str]:
    notes: list[str] = []
    for key, score in sorted(result.applied_scores.items()):
        notes.append(f"{key} score set to {score} by the narrator")
    for key, score in sorted(result.rejected_scores.items()):
        notes.append(
            f"{key}: narrator proposed {score!r} — rejected, that letter is "
            "computed from the figures"
        )
    notes.extend(result.notes)
    return notes


def _merge(grade: Grade, data: dict[str, Any]) -> NarratorResult:
    """Validate the model's output against the guardrail before trusting it."""
    result = NarratorResult(ok=True)
    result.summary = str(data.get("summary") or "")
    result.new_story = str(data.get("new_story") or "")
    result.sponsorship_note = str(data.get("sponsorship_note") or "")

    for source in data.get("sources") or []:
        if isinstance(source, dict) and source.get("url"):
            result.sources.append(
                {"label": str(source.get("label") or source["url"]), "url": str(source["url"])}
            )

    by_key = {letter.key: letter for letter in grade.letters}
    seen: set[str] = set()

    for item in data.get("letters") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip().upper()
        if key not in by_key or key in seen:
            continue
        seen.add(key)

        read = str(item.get("read") or "").strip()
        if read:
            result.reads[key] = read

        proposed = str(item.get("score") or "unchanged").strip().lower()
        if proposed in ("", "unchanged"):
            continue
        if proposed not in VALID_SCORES:
            result.rejected_scores[key] = proposed
            continue

        # The guardrail: a computed letter's score is not the narrator's to set.
        was_unknown = by_key[key].score == UNKNOWN
        if key in JUDGEMENT_LETTERS or was_unknown:
            if proposed != by_key[key].score:
                result.applied_scores[key] = proposed
        else:
            result.rejected_scores[key] = proposed

    missing = [key for key in by_key if key not in result.reads]
    if missing:
        result.notes.append(
            f"narrator returned no read for {', '.join(sorted(missing))}; kept the "
            "computed prose for those"
        )
    return result


def _facts_payload(grade: Grade, research: str, today: str) -> str:
    """The volatile half of the prompt — after the cache breakpoint."""
    tech = grade.technicals
    market = tech.get("market") or {}

    lines = [
        f"# {grade.ticker} — {grade.company or 'company name unavailable'}",
        f"As of {grade.as_of}. Today is {today}.",
        f"Price: {f'${grade.price:,.2f}' if grade.price else 'unavailable'}",
        "",
        "## Computed letters — these figures are facts",
        "",
    ]
    for letter in grade.letters:
        lines.append(f"### {letter.key} — {letter.name}")
        lines.append(f"- computed score: **{letter.score}**")
        lines.append(f"- threshold: {letter.threshold}")
        lines.append(f"- measured: {letter.actual}")
        if letter.score == UNKNOWN:
            lines.append(
                "- NOTE: ungraded for want of data. You may set this score if your "
                "research supplies what was missing."
            )
        if letter.key in JUDGEMENT_LETTERS:
            lines.append(
                "- NOTE: this is a judgement letter. Score it yourself using the "
                "rubric and your research."
            )
        lines.append("")

    lines.append("## Technicals")
    for label, key, suffix in (
        ("Relative strength vs SPY", "rs_blend_pct", "pp"),
        ("% off 52-week high", "off_high_pct", "%"),
        ("Base depth", "base_depth_pct", "%"),
        ("Base length", "base_length_weeks", " weeks"),
        ("Latest volume vs average", "breakout_vol_pct", "%"),
        ("Derived pivot", "pivot", ""),
    ):
        value = tech.get(key)
        if value is not None:
            shown = f"{value:,.1f}" if isinstance(value, float) else str(value)
            lines.append(f"- {label}: {shown}{suffix}")
    if tech.get("wide_loose"):
        lines.append("- Base flagged wide and loose")
    if market.get("label"):
        lines.append(f"- Market (M): {market['label']} — {market.get('detail', '')}")

    if grade.warnings:
        lines.append("")
        lines.append("## Data gaps in the computed pass")
        lines.extend(f"- {w}" for w in grade.warnings)

    lines.append("")
    if research.strip():
        lines.append("## Research findings (web search)")
        lines.append(research.strip())
    else:
        lines.append("## Research findings")
        lines.append(
            "None — web research was not run or returned nothing. Judge N and I on "
            "the price and ownership evidence above, and say plainly what you could "
            "not establish."
        )

    lines.append("")
    lines.append(
        "Return the JSON object described by the schema: a `read` for all seven "
        "letters, `score` set to \"unchanged\" except where permitted above, plus "
        "`summary`, `new_story`, `sponsorship_note` and `sources`."
    )
    return "\n".join(lines)


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    out: dict[str, int] = {}
    for field_name in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = getattr(usage, field_name, None)
        if isinstance(value, int):
            out[field_name] = value
    return out


def _is_bad_request(exc: Exception) -> bool:
    try:
        import anthropic

        return isinstance(exc, anthropic.BadRequestError)
    except ImportError:  # pragma: no cover - anthropic is present when we get here
        return False


def _describe(exc: Exception) -> str:
    """Turn an SDK exception into something a run footer can carry.

    Uses the typed exception classes rather than string matching, so a retryable
    failure reads differently from a permanent one.
    """
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return f"{type(exc).__name__}: {exc}"

    if isinstance(exc, anthropic.AuthenticationError):
        return "Anthropic authentication rejected — check ANTHROPIC_API_KEY"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "the Anthropic key lacks access to this model or feature"
    if isinstance(exc, anthropic.NotFoundError):
        return f"model not found ({exc})"
    if isinstance(exc, anthropic.RateLimitError):
        return "Anthropic rate limited; the grade will be retried on the next run"
    if isinstance(exc, anthropic.APIConnectionError):
        return "could not reach the Anthropic API"
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic API error {exc.status_code}: {str(exc)[:120]}"
    return f"{type(exc).__name__}: {str(exc)[:120]}"


def config_from(settings: dict[str, Any]) -> NarratorConfig:
    """Build a NarratorConfig from the resolved `canslim` config block."""
    return NarratorConfig(
        enabled=str(settings.get("narrator", "off")).lower() == "llm",
        model=str(settings.get("narrator_model") or DEFAULT_MODEL),
        effort=str(settings.get("narrator_effort") or "medium"),
        research=bool(settings.get("narrator_research", True)),
        max_tokens=int(settings.get("narrator_max_tokens") or 16000),
        use_fallbacks=bool(settings.get("narrator_fallbacks", True)),
    )
