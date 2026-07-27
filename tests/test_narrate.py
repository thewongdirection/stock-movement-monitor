"""The LLM narrator, and above all its guardrail.

The narrator is the one component that lets a language model touch the
scorecard, so most of what follows is about what it is *not* allowed to do. The
deterministic pass computes C, A, S, L and M from figures; a model handed that
scorecard will cheerfully rewrite the arithmetic, and these tests are what stop
it. The other half is failure containment: every way the API call can go wrong
must leave the computed grade exactly as it was.

The `anthropic` package is an optional extra, so everything here runs against a
stub module injected into ``sys.modules``. That also lets us assert on the
*shape* of the request — no sampling parameters, adaptive thinking, the dated
web-search tool, a cache breakpoint on the methodology.
"""

from __future__ import annotations

import builtins
import json
import sys
import types

import pytest

from monitor.canslim.grader import FAIL, PARTIAL, PASS, UNKNOWN, Grade, LetterScore
from monitor.canslim.narrate import (
    DEFAULT_MODEL,
    NarratorConfig,
    apply as narrate_apply,
    config_from,
)
from monitor.canslim.skill import SkillPaths


# --------------------------------------------------------------------------
# Stub Anthropic SDK
# --------------------------------------------------------------------------
class StubBlock:
    def __init__(self, type_: str, text: str = ""):
        self.type = type_
        self.text = text


class StubUsage:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class StubResponse:
    """Just enough of a Message: stop_reason, content blocks, usage."""

    def __init__(
        self,
        *,
        stop_reason: str = "end_turn",
        text: str | None = None,
        blocks: list | None = None,
        usage=None,
        stop_details=None,
    ):
        self.stop_reason = stop_reason
        if blocks is not None:
            self.content = blocks
        else:
            self.content = [StubBlock("text", text)] if text is not None else []
        self.usage = usage
        self.stop_details = stop_details


def json_response(payload: dict, **kwargs) -> StubResponse:
    return StubResponse(text=json.dumps(payload), **kwargs)


class _Messages:
    def __init__(self, client, beta: bool):
        self._client = client
        self._beta = beta

    def create(self, **kwargs):
        kwargs["_beta_endpoint"] = self._beta
        self._client.requests.append(kwargs)
        if not self._client.queue:
            raise AssertionError("the narrator made more API calls than the test queued")
        item = self._client.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class StubClient:
    def __init__(self, queue, timeout=None):
        self.queue = list(queue)
        self.requests: list[dict] = []
        self.timeout = timeout
        self.messages = _Messages(self, beta=False)
        self.beta = types.SimpleNamespace(messages=_Messages(self, beta=True))


#: The SDK's exception hierarchy, in the shape `_describe()` discriminates on.
#: Defined at module level so a test can raise one without first building the
#: fake module.
class APIError(Exception):
    pass


class APIConnectionError(APIError):
    pass


class APIStatusError(APIError):
    def __init__(self, message: str = "", status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class BadRequestError(APIStatusError):
    pass


class AuthenticationError(APIStatusError):
    pass


class PermissionDeniedError(APIStatusError):
    pass


class NotFoundError(APIStatusError):
    pass


class RateLimitError(APIStatusError):
    pass


EXCEPTIONS = {
    cls.__name__: cls
    for cls in (
        APIError,
        APIConnectionError,
        APIStatusError,
        BadRequestError,
        AuthenticationError,
        PermissionDeniedError,
        NotFoundError,
        RateLimitError,
    )
}


def stub_anthropic(monkeypatch, queue, *, construct_error: Exception | None = None):
    """Install a fake `anthropic` module. Returns a handle on the built client."""
    mod = types.ModuleType("anthropic")
    holder: dict = {"client": None}

    def Anthropic(**kwargs):
        if construct_error is not None:
            raise construct_error
        holder["client"] = StubClient(queue, timeout=kwargs.get("timeout"))
        return holder["client"]

    for name, cls in EXCEPTIONS.items():
        setattr(mod, name, cls)
    mod.Anthropic = Anthropic

    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return mod, holder


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
BASE_SCORES = {
    "C": PASS,
    "A": PASS,
    "N": FAIL,
    "S": PARTIAL,
    "L": PASS,
    "I": PARTIAL,
    "M": PASS,
}


def make_grade(**overrides) -> Grade:
    scores = dict(BASE_SCORES)
    scores.update(overrides)
    letters = [
        LetterScore(
            key=key,
            score=score,
            threshold=f"{key} threshold",
            actual=f"{key} measured",
            read=f"computed read for {key}",
        )
        for key, score in scores.items()
    ]
    return Grade(
        ticker="LEAD",
        company="Leader Corp",
        as_of="2026-07-27 16:00 ET",
        price=100.0,
        letters=letters,
        verdict="WATCH",
        tone="pressure",
        summary="computed summary",
        warnings=["institutional holder count unavailable"],
        technicals={
            "pivot": 102.0,
            "off_high_pct": 2.0,
            "rs_blend_pct": 18.0,
            "base_depth_pct": 14.0,
            "base_length_weeks": 8,
            "breakout_vol_pct": 55.0,
            "market": {"label": "Confirmed uptrend", "state": PASS, "detail": "0 dist days"},
        },
    )


def make_skill(tmp_path, methodology: str | None = None) -> SkillPaths:
    root = tmp_path / "can-slim-grader"
    (root / "references").mkdir(parents=True, exist_ok=True)
    if methodology is not None:
        (root / "references" / "canslim-methodology.md").write_text(methodology)
    return SkillPaths(
        root=root,
        template=root / "assets" / "evaluation_template.html",
        relative_strength=root / "scripts" / "relative_strength.py",
        html_to_pdf=root / "scripts" / "html_to_pdf.py",
    )


def payload(letters: list[dict], **extra) -> dict:
    body = {
        "letters": letters,
        "summary": "narrated summary",
        "new_story": "",
        "sponsorship_note": "",
        "sources": [],
    }
    body.update(extra)
    return body


def letter(key: str, *, score: str = "unchanged", read: str | None = None, why: str = "") -> dict:
    return {
        "key": key,
        "read": read if read is not None else f"narrated read for {key}",
        "score": score,
        "score_rationale": why,
    }


def all_letters(**scores) -> list[dict]:
    return [letter(key, score=scores.get(key, "unchanged")) for key in BASE_SCORES]


@pytest.fixture
def config() -> NarratorConfig:
    """Enabled, no research — one API call per grade keeps the queues honest."""
    return NarratorConfig(enabled=True, research=False)


def run(monkeypatch, tmp_path, config, queue, *, grade: Grade | None = None, **skill_kwargs):
    _, holder = stub_anthropic(monkeypatch, queue)
    skill = make_skill(tmp_path, **skill_kwargs)
    graded, result = narrate_apply(
        grade if grade is not None else make_grade(), config, skill, today="2026-07-27"
    )
    return graded, result, holder["client"]


# --------------------------------------------------------------------------
# The guardrail
# --------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["C", "A", "S", "L", "M"])
def test_a_computed_letter_score_is_never_the_narrators_to_set(
    monkeypatch, tmp_path, config, key
):
    """C, A, S, L and M come out of arithmetic. A proposal is logged and dropped."""
    before = make_grade().letter(key).score
    graded, result, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters(**{key: FAIL})))],
    )

    assert result.ok
    assert key not in result.applied_scores
    assert result.rejected_scores[key] == FAIL
    assert graded.letter(key).score == before
    assert any(
        f"{key}: narrator proposed 'fail' — rejected" in note
        for note in graded.narrator_notes
    )


@pytest.mark.parametrize("key,proposed", [("N", PASS), ("I", FAIL)])
def test_the_two_judgement_letters_may_be_scored(
    monkeypatch, tmp_path, config, key, proposed
):
    """N's "new" story and I's sponsorship quality are what the model is for."""
    graded, result, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters(**{key: proposed})))],
    )

    assert result.applied_scores == {key: proposed}
    assert not result.rejected_scores
    assert graded.letter(key).score == proposed
    assert f"{key} score set to {proposed} by the narrator" in graded.narrator_notes


def test_an_ungraded_letter_can_be_filled_in(monkeypatch, tmp_path, config):
    """`unknown` means we lacked data — if research supplies it, that is a win."""
    graded, result, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters(S=PASS)))],
        grade=make_grade(S=UNKNOWN),
    )

    assert result.applied_scores == {"S": PASS}
    assert graded.letter("S").score == PASS


def test_filling_in_one_unknown_does_not_unlock_the_computed_letters(
    monkeypatch, tmp_path, config
):
    """The permission is per-letter, not a mode the model can switch on."""
    graded, result, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters(S=PASS, C=FAIL, A=PARTIAL)))],
        grade=make_grade(S=UNKNOWN),
    )

    assert result.applied_scores == {"S": PASS}
    assert result.rejected_scores == {"C": FAIL, "A": PARTIAL}
    assert graded.letter("C").score == PASS
    assert graded.letter("A").score == PASS


def test_a_score_outside_the_vocabulary_is_rejected_even_for_a_judgement_letter(
    monkeypatch, tmp_path, config
):
    graded, result, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters(N="strong buy")))],
    )

    assert result.applied_scores == {}
    assert result.rejected_scores == {"N": "strong buy"}
    assert graded.letter("N").score == FAIL


def test_restating_the_existing_score_is_not_recorded_as_a_change(
    monkeypatch, tmp_path, config
):
    """N is already `fail`; agreeing with the rubric is not an override."""
    _, result, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters(N=FAIL)))],
    )

    assert result.applied_scores == {}
    assert result.rejected_scores == {}


def test_the_prose_is_always_accepted_even_when_the_score_is_not(
    monkeypatch, tmp_path, config
):
    """Rejecting a score must not throw away the commentary that came with it."""
    graded, result, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters(C=FAIL)))],
    )

    assert graded.letter("C").read == "narrated read for C"
    assert graded.letter("C").score == PASS
    assert set(result.reads) == set(BASE_SCORES)


def test_a_letter_the_narrator_skipped_keeps_the_computed_prose(
    monkeypatch, tmp_path, config
):
    graded, result, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload([letter(k) for k in ("C", "A", "N", "L", "M")]))],
    )

    assert graded.letter("S").read == "computed read for S"
    assert graded.letter("C").read == "narrated read for C"
    assert any("no read for I, S" in note for note in result.notes)


def test_an_unknown_letter_key_is_ignored(monkeypatch, tmp_path, config):
    graded, result, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters() + [letter("Z", score=PASS)]))],
    )

    assert "Z" not in result.applied_scores and "Z" not in result.rejected_scores
    assert len(graded.letters) == 7


def test_a_repeated_letter_takes_the_first_verdict_only(monkeypatch, tmp_path, config):
    """Otherwise a second entry could launder a rejected score into an applied one."""
    _, result, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [
            json_response(
                payload(all_letters(N=PASS) + [letter("N", score=FAIL, read="second take")])
            )
        ],
    )

    assert result.applied_scores == {"N": PASS}
    assert result.reads["N"] == "narrated read for N"


# --------------------------------------------------------------------------
# Re-deriving what the scores drive
# --------------------------------------------------------------------------
def test_upgrading_n_re_derives_the_verdict_and_the_buy_point(
    monkeypatch, tmp_path, config
):
    """A stale AVOID next to an upgraded letter would be worse than no narration."""
    graded, _, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters(N=PASS)))],
    )

    assert graded.verdict == "BUY-RANGE"
    assert "102.00" in graded.entry
    assert "pivot" in graded.entry.lower()
    assert graded.stop.startswith("$93.84")


def test_downgrading_a_judgement_letter_can_lose_the_buy_point(
    monkeypatch, tmp_path, config
):
    graded, _, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters(N=FAIL)))],
        grade=make_grade(N=PASS),
    )

    assert graded.verdict == "WATCH"
    assert graded.entry == "None now"


def test_the_narrators_summary_wins_but_only_if_it_wrote_one(
    monkeypatch, tmp_path, config
):
    graded, _, _ = run(
        monkeypatch, tmp_path, config, [json_response(payload(all_letters()))]
    )
    assert graded.summary == "narrated summary"

    blank, _, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters(), summary="   "))],
    )
    assert blank.summary.startswith("Fundamentals hold up but")


def test_the_judgement_findings_are_recorded_as_notes(monkeypatch, tmp_path, config):
    graded, _, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [
            json_response(
                payload(
                    all_letters(N=PASS),
                    new_story="Launched the Atlas platform, 2026-03-04.",
                    sponsorship_note="Three funds with long records added in Q1.",
                )
            )
        ],
    )

    assert "N — new driver: Launched the Atlas platform, 2026-03-04." in graded.narrator_notes
    assert any(note.startswith("I — sponsorship:") for note in graded.narrator_notes)


def test_sources_are_carried_through_and_junk_is_dropped(monkeypatch, tmp_path, config):
    graded, result, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [
            json_response(
                payload(
                    all_letters(),
                    sources=[
                        {"label": "Q1 release", "url": "https://example.com/q1"},
                        {"label": "no url here", "url": ""},
                        "not even a dict",
                        {"url": "https://example.com/bare"},
                    ],
                )
            )
        ],
    )

    assert [s["url"] for s in graded.narrator_sources] == [
        "https://example.com/q1",
        "https://example.com/bare",
    ]
    # A source with no label falls back to its own URL rather than rendering blank.
    assert result.sources[1]["label"] == "https://example.com/bare"


def test_narration_is_declared_on_the_grade(monkeypatch, tmp_path, config):
    graded, _, _ = run(
        monkeypatch, tmp_path, config, [json_response(payload(all_letters()))]
    )

    assert graded.narrated is True
    assert graded.data_sources.endswith(" + Claude narration")


# --------------------------------------------------------------------------
# Failure containment — the computed grade must survive everything
# --------------------------------------------------------------------------
def assert_untouched(graded: Grade) -> None:
    assert graded.narrated is False
    assert graded.narrator_notes == []
    assert graded.summary == "computed summary"
    assert graded.letter("C").read == "computed read for C"
    assert [letter_.score for letter_ in graded.letters] == list(BASE_SCORES.values())
    assert "Claude narration" not in graded.data_sources


def test_a_refusal_is_detected_before_the_content_is_read(monkeypatch, tmp_path, config):
    """On a refusal `content` is empty or partial — reading it as complete is the bug."""
    refusal = StubResponse(
        stop_reason="refusal",
        blocks=[],
        stop_details=types.SimpleNamespace(category="financial_advice"),
    )
    graded, result, _ = run(monkeypatch, tmp_path, config, [refusal])

    assert not result.ok
    assert "declined by safety classifiers (financial_advice)" == result.skipped
    assert_untouched(graded)


def test_a_refusal_without_a_category_still_reports_cleanly(monkeypatch, tmp_path, config):
    _, result, _ = run(
        monkeypatch, tmp_path, config, [StubResponse(stop_reason="refusal", blocks=[])]
    )
    assert result.skipped == "declined by safety classifiers (no category)"


def test_malformed_json_leaves_the_scorecard_alone(monkeypatch, tmp_path, config):
    graded, result, _ = run(
        monkeypatch, tmp_path, config, [StubResponse(text="{not json at all")]
    )

    assert not result.ok
    assert "not valid JSON" in result.skipped
    assert_untouched(graded)


def test_an_empty_response_is_a_skip_not_a_crash(monkeypatch, tmp_path, config):
    graded, result, _ = run(monkeypatch, tmp_path, config, [StubResponse(text="   ")])

    assert not result.ok
    assert result.skipped == "empty response"
    assert_untouched(graded)


def test_truncated_output_is_flagged(monkeypatch, tmp_path, config):
    """max_tokens usually means the JSON is cut off — but if it parsed, say so."""
    _, result, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters()), stop_reason="max_tokens")],
    )

    assert result.ok
    assert any("hit max_tokens=16000" in note for note in result.notes)


@pytest.mark.parametrize(
    "exc_name,kwargs,expected",
    [
        ("AuthenticationError", {}, "Anthropic authentication rejected"),
        ("PermissionDeniedError", {}, "lacks access to this model or feature"),
        ("NotFoundError", {}, "model not found"),
        ("RateLimitError", {}, "rate limited"),
        ("APIConnectionError", {}, "could not reach the Anthropic API"),
        ("APIStatusError", {"status_code": 529}, "Anthropic API error 529"),
    ],
)
def test_every_sdk_failure_is_described_not_raised(
    monkeypatch, tmp_path, config, exc_name, kwargs, expected
):
    """The typed exception chain, so a retryable failure reads unlike a permanent one."""
    graded, result, _ = run(
        monkeypatch, tmp_path, config, [EXCEPTIONS[exc_name]("boom", **kwargs)]
    )

    assert not result.ok
    assert expected in result.skipped
    assert_untouched(graded)


def test_a_client_that_cannot_be_constructed_is_reported(monkeypatch, tmp_path, config):
    stub_anthropic(monkeypatch, [], construct_error=RuntimeError("no credentials"))
    graded, result = narrate_apply(
        make_grade(), config, make_skill(tmp_path), today="2026-07-27"
    )

    assert result.skipped == "no Anthropic client available"
    assert_untouched(graded)


def test_a_missing_anthropic_package_is_a_clear_skip(monkeypatch, tmp_path, config):
    """The package is an optional extra; not having it must not be a traceback."""
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("No module named 'anthropic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    graded, result = narrate_apply(
        make_grade(), config, make_skill(tmp_path), today="2026-07-27"
    )

    assert "pip install anthropic" in result.skipped
    assert_untouched(graded)


def test_the_narrator_is_off_unless_asked_for(monkeypatch, tmp_path):
    graded, result = narrate_apply(
        make_grade(), NarratorConfig(), make_skill(tmp_path), today="2026-07-27"
    )

    assert not result.ok
    assert "canslim.narrator: llm to enable" in result.skipped
    assert_untouched(graded)


# --------------------------------------------------------------------------
# Request shape
# --------------------------------------------------------------------------
def test_no_sampling_parameters_are_sent(monkeypatch, tmp_path, config):
    """Opus 5 rejects budget_tokens, and sampling knobs fight adaptive thinking."""
    _, _, client = run(
        monkeypatch, tmp_path, config, [json_response(payload(all_letters()))]
    )
    request = client.requests[-1]

    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in request
    assert request["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in request["thinking"]
    assert request["model"] == DEFAULT_MODEL


def test_the_grading_call_constrains_its_own_output_shape(monkeypatch, tmp_path, config):
    _, _, client = run(
        monkeypatch, tmp_path, config, [json_response(payload(all_letters()))]
    )
    output_config = client.requests[-1]["output_config"]

    assert output_config["format"]["type"] == "json_schema"
    schema = output_config["format"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "letters",
        "summary",
        "new_story",
        "sponsorship_note",
        "sources",
    }
    assert output_config["effort"] == "medium"


def test_the_grading_call_has_no_tools(monkeypatch, tmp_path, config):
    """Structured output is the point of phase two; a tool call would break it."""
    _, _, client = run(
        monkeypatch, tmp_path, config, [json_response(payload(all_letters()))]
    )
    assert "tools" not in client.requests[-1]


def test_the_methodology_sits_behind_a_cache_breakpoint(monkeypatch, tmp_path, config):
    """It is identical on every grade, so every ticker after the first reads it free."""
    _, _, client = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters()))],
        methodology="# CAN SLIM\n\nThe seven letters..." + "filler " * 500,
    )
    system = client.requests[-1]["system"]

    assert len(system) == 2
    assert "CAN SLIM methodology" in system[1]["text"]
    assert system[1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in system[0]


def test_a_missing_methodology_file_still_leaves_a_valid_prompt(
    monkeypatch, tmp_path, config
):
    _, result, client = run(
        monkeypatch, tmp_path, config, [json_response(payload(all_letters()))]
    )
    system = client.requests[-1]["system"]

    assert result.ok
    assert len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_the_facts_prompt_marks_which_letters_are_open(monkeypatch, tmp_path, config):
    _, _, client = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters()))],
        grade=make_grade(S=UNKNOWN),
    )
    prompt = client.requests[-1]["messages"][0]["content"]

    assert "these figures are facts" in prompt
    assert prompt.count("this is a judgement letter") == 2  # N and I
    assert "ungraded for want of data" in prompt
    assert "institutional holder count unavailable" in prompt
    assert "Today is 2026-07-27" in prompt


def test_the_prompt_says_plainly_when_no_research_was_run(monkeypatch, tmp_path, config):
    _, _, client = run(
        monkeypatch, tmp_path, config, [json_response(payload(all_letters()))]
    )
    prompt = client.requests[-1]["messages"][0]["content"]

    assert "web research was not run" in prompt
    assert "do not fill the gap" not in prompt  # that's the research prompt's line


def test_usage_is_reported_for_cost_tracking(monkeypatch, tmp_path, config):
    usage = StubUsage(
        input_tokens=6100,
        output_tokens=1400,
        cache_read_input_tokens=4500,
        cache_creation_input_tokens=0,
        server_tool_use="not an int",
    )
    _, result, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [json_response(payload(all_letters()), usage=usage)],
    )

    assert result.usage["input_tokens"] == 6100
    assert result.usage["cache_read_input_tokens"] == 4500
    assert "server_tool_use" not in result.usage


# --------------------------------------------------------------------------
# Refusal fallbacks
# --------------------------------------------------------------------------
def test_fallbacks_are_requested_through_the_beta_endpoint(monkeypatch, tmp_path, config):
    _, _, client = run(
        monkeypatch, tmp_path, config, [json_response(payload(all_letters()))]
    )
    request = client.requests[-1]

    assert request["_beta_endpoint"] is True
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["fallbacks"] == "default"


def test_a_key_without_the_fallback_beta_retries_plainly(monkeypatch, tmp_path, config):
    """Losing grading over an optional resilience feature would be the worse trade."""
    bad = BadRequestError("unsupported beta: server-side-fallback-2026-07-01")
    graded, result, client = run(
        monkeypatch, tmp_path, config, [bad, json_response(payload(all_letters()))]
    )

    assert result.ok
    assert graded.narrated
    assert any("refusal fallbacks unavailable" in note for note in result.notes)
    assert [r["_beta_endpoint"] for r in client.requests] == [True, False]
    assert "fallbacks" not in client.requests[1]


def test_a_bad_request_about_something_else_is_not_retried(monkeypatch, tmp_path, config):
    bad = BadRequestError("output_config: effort must be one of low, medium, high")
    graded, result, client = run(monkeypatch, tmp_path, config, [bad])

    assert not result.ok
    assert "Anthropic API error 400" in result.skipped
    assert len(client.requests) == 1
    assert_untouched(graded)


def test_fallbacks_can_be_turned_off(monkeypatch, tmp_path):
    config = NarratorConfig(enabled=True, research=False, use_fallbacks=False)
    _, _, client = run(
        monkeypatch, tmp_path, config, [json_response(payload(all_letters()))]
    )

    assert client.requests[-1]["_beta_endpoint"] is False
    assert "betas" not in client.requests[-1]


# --------------------------------------------------------------------------
# Phase one: research
# --------------------------------------------------------------------------
@pytest.fixture
def researching() -> NarratorConfig:
    return NarratorConfig(enabled=True, research=True)


def test_research_uses_the_dated_web_search_tool(monkeypatch, tmp_path, researching):
    _, _, client = run(
        monkeypatch,
        tmp_path,
        researching,
        [
            StubResponse(text="Atlas platform launched 2026-03-04."),
            json_response(payload(all_letters())),
        ],
    )
    tools = client.requests[0]["tools"]

    assert tools == [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 6}
    ]
    assert "output_config" in client.requests[0]
    assert "format" not in client.requests[0]["output_config"]


def test_the_research_digest_reaches_the_grading_prompt(monkeypatch, tmp_path, researching):
    _, _, client = run(
        monkeypatch,
        tmp_path,
        researching,
        [
            StubResponse(text="Atlas platform launched 2026-03-04."),
            json_response(payload(all_letters())),
        ],
    )
    prompt = client.requests[1]["messages"][0]["content"]

    assert "## Research findings (web search)" in prompt
    assert "Atlas platform launched 2026-03-04." in prompt


def test_a_paused_tool_loop_is_resumed(monkeypatch, tmp_path, researching):
    """Server-side tools stop with pause_turn at the loop cap; resume by re-sending."""
    paused = StubResponse(
        stop_reason="pause_turn", blocks=[StubBlock("text", "searching...")]
    )
    _, result, client = run(
        monkeypatch,
        tmp_path,
        researching,
        [paused, StubResponse(text="Found it."), json_response(payload(all_letters()))],
    )

    assert result.ok
    assert any("resumed after pause_turn" in note for note in result.notes)
    assert client.requests[1]["messages"][-1]["role"] == "assistant"
    assert "Found it." in client.requests[2]["messages"][0]["content"]


def test_a_turn_that_never_unpauses_is_abandoned(monkeypatch, tmp_path, researching):
    paused = [
        StubResponse(stop_reason="pause_turn", blocks=[StubBlock("text", "...")])
        for _ in range(4)
    ]
    _, result, client = run(
        monkeypatch,
        tmp_path,
        researching,
        paused + [json_response(payload(all_letters()))],
    )

    assert result.ok  # grading still happens, just without research
    assert any("abandoned: still paused" in note for note in result.notes)
    assert "web research was not run" in client.requests[-1]["messages"][0]["content"]


def test_a_failed_research_call_does_not_stop_the_grade(monkeypatch, tmp_path, researching):
    graded, result, _ = run(
        monkeypatch,
        tmp_path,
        researching,
        [RateLimitError("slow down"), json_response(payload(all_letters(N=PASS)))],
    )

    assert result.ok
    assert graded.narrated
    assert any("research failed" in note for note in result.notes)
    assert any("research failed" in note for note in graded.narrator_notes)


def test_a_declined_research_call_is_noted_and_skipped(monkeypatch, tmp_path, researching):
    _, result, _ = run(
        monkeypatch,
        tmp_path,
        researching,
        [
            StubResponse(stop_reason="refusal", blocks=[]),
            json_response(payload(all_letters())),
        ],
    )

    assert result.ok
    assert "research declined by safety classifiers" in result.notes


def test_research_ignores_non_text_blocks(monkeypatch, tmp_path, researching):
    """The response carries thinking and tool-result blocks too; only text is the digest."""
    blocks = [
        StubBlock("thinking", "let me search"),
        StubBlock("server_tool_use", ""),
        StubBlock("text", "Ownership rose 12% in Q1."),
    ]
    _, _, client = run(
        monkeypatch,
        tmp_path,
        researching,
        [
            StubResponse(blocks=blocks),
            json_response(payload(all_letters())),
        ],
    )
    prompt = client.requests[1]["messages"][0]["content"]

    assert "Ownership rose 12% in Q1." in prompt
    assert "let me search" not in prompt


# --------------------------------------------------------------------------
# Config plumbing and the report
# --------------------------------------------------------------------------
def test_config_from_reads_the_canslim_block():
    cfg = config_from(
        {
            "narrator": "llm",
            "narrator_model": "claude-sonnet-5",
            "narrator_effort": "high",
            "narrator_research": False,
            "narrator_max_tokens": 24000,
            "narrator_fallbacks": False,
        }
    )

    assert cfg.enabled is True
    assert cfg.model == "claude-sonnet-5"
    assert cfg.effort == "high"
    assert cfg.research is False
    assert cfg.max_tokens == 24000
    assert cfg.use_fallbacks is False


def test_config_from_defaults_to_off():
    cfg = config_from({})
    assert cfg.enabled is False
    assert cfg.model == DEFAULT_MODEL
    assert config_from({"narrator": "off"}).enabled is False


def test_the_config_block_is_resolved_with_bounds():
    from monitor import config as config_mod

    cfg = config_mod.from_dict(
        {
            "tickers": ["LEAD"],
            "canslim": {"narrator": "llm", "narrator_max_tokens": 999_999},
        }
    )

    assert cfg.canslim["narrator"] == "llm"
    assert cfg.canslim["narrator_max_tokens"] == 64_000  # clamped to the ceiling
    assert any(
        issue.path == "canslim.narrator_max_tokens" and "clamped to 64,000" in issue.message
        for issue in cfg.issues
    )


def test_an_unknown_narrator_backend_is_refused():
    from monitor import config as config_mod

    cfg = config_mod.from_dict(
        {"tickers": ["LEAD"], "canslim": {"narrator": "gpt"}}
    )

    assert cfg.canslim["narrator"] == "off"
    assert any("'gpt' is not allowed" in issue.message for issue in cfg.issues)


def test_the_report_declares_which_pass_wrote_the_letters(monkeypatch, tmp_path, config):
    from monitor.canslim.report import DISCLAIMER_NARRATED, _config_for

    graded, _, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [
            json_response(
                payload(
                    all_letters(C=FAIL, N=PASS),
                    sources=[{"label": "Q1 release", "url": "https://example.com/q1"}],
                )
            )
        ],
    )
    rendered = _config_for(graded)

    assert rendered["disclaimer"] == DISCLAIMER_NARRATED
    assert "written by Claude" in rendered["disclaimer"]
    assert rendered["dataSource"].endswith("letters narrated by Claude")
    assert {"label": "Q1 release", "url": "https://example.com/q1"} in rendered["sources"]
    # The rejected override is visible to a human reading the PDF, not just in logs.
    assert any(
        "narrator proposed 'fail' — rejected" in item for item in rendered["buyPlan"]["items"]
    )


# --------------------------------------------------------------------------
# The daily cache
# --------------------------------------------------------------------------
def test_narration_state_survives_the_daily_cache(monkeypatch, tmp_path, config):
    """A cached grade must render the narrated disclaimer, not the computed one.

    Grades are reused all day, so every alert after the first is served from this
    round trip. If `narrated` were dropped here, the second alert of the day would
    quietly claim the letters were computed.
    """
    from datetime import datetime

    from monitor.canslim.report import Report
    from monitor.canslim.service import CanSlimService

    graded, _, _ = run(
        monkeypatch,
        tmp_path,
        config,
        [
            json_response(
                payload(
                    all_letters(N=PASS, C=FAIL),
                    sources=[{"label": "Q1 release", "url": "https://example.com/q1"}],
                )
            )
        ],
    )

    html = tmp_path / "LEAD-canslim.html"
    html.write_text("<html></html>")
    service = CanSlimService(fmp_api_key="", fmp_base_url="", cache_dir=tmp_path / "cache")
    now = datetime(2026, 7, 27, 16, 30)
    service._to_cache("LEAD", now, Report(grade=graded, html_path=html, pdf_path=None))

    restored = service._from_cache("LEAD", now)
    assert restored is not None
    assert restored.grade.narrated is True
    assert restored.grade.narrator_sources == [
        {"label": "Q1 release", "url": "https://example.com/q1"}
    ]
    assert "N score set to pass by the narrator" in restored.grade.narrator_notes
    assert any("rejected" in note for note in restored.grade.narrator_notes)


def test_a_computed_grade_stays_computed_through_the_cache(tmp_path):
    from datetime import datetime

    from monitor.canslim.report import Report
    from monitor.canslim.service import CanSlimService

    html = tmp_path / "LEAD-canslim.html"
    html.write_text("<html></html>")
    service = CanSlimService(fmp_api_key="", fmp_base_url="", cache_dir=tmp_path / "cache")
    now = datetime(2026, 7, 27, 16, 30)
    service._to_cache("LEAD", now, Report(grade=make_grade(), html_path=html, pdf_path=None))

    restored = service._from_cache("LEAD", now)
    assert restored.grade.narrated is False
    assert restored.grade.narrator_notes == []
