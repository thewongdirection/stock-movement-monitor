"""CAN SLIM scoring, the unknown grade, and the narrator's one-way merge."""

from __future__ import annotations

import pytest

from monitor.canslim import CanSlim, brief, compact, grade, locate, render, status
from monitor.canslim.fundamentals import gather
from monitor.canslim.grader import (Facts, Grade, Letter, relative_strength,
                                    score_a, score_c, score_l, score_m, score_n)
from monitor.canslim.narrate import NarratorUnavailable, merge, narrate
from monitor.sources.base import SourceError, Unreachable


def quarters(eps: list[float], revenue: list[float] | None = None) -> list[dict]:
    revenue = revenue or [e * 1000 for e in eps]
    return [{"date": f"2026-{n:02d}-30", "eps": e, "revenue": r}
            for n, (e, r) in enumerate(zip(eps, revenue), start=1)]


def years(eps: list[float]) -> list[dict]:
    return [{"date": f"{2026 - n}-12-31", "eps": e} for n, e in enumerate(eps)]


def strong_facts(**kwargs) -> Facts:
    defaults = dict(
        ticker="NVDA", price=205.0, high_52w=207.0, low_52w=120.0,
        # Accelerating: the newest quarter is +100% YoY against +82% the quarter
        # before. The rubric scores a decelerating grower as PARTIAL even when
        # the growth rate is high, so this has to accelerate to reach PASS.
        quarters=quarters([2.4, 2.0, 1.8, 1.7, 1.2, 1.1]),
        years=years([6.0, 4.5, 3.4, 2.5]),
        roe=0.42, debt_to_equity=0.4,
        closes=[100 + n * 0.5 for n in range(220)],
        volumes=[1_000_000] * 220,
        index_closes=[400 + n * 0.1 for n in range(220)],
    )
    defaults.update(kwargs)
    return Facts(**defaults)


class TestLetterC:
    def test_growth_is_measured_against_the_same_quarter_a_year_earlier(self):
        """Four rows back, never the previous quarter.

        A sequential compare makes every seasonal business look like it
        collapses in Q1 and explodes in Q4.
        """
        letter = score_c(strong_facts())
        assert letter.grade is Grade.PASS
        assert "+100%" in letter.evidence[0]         # 2.4 vs 1.2, not 2.4 vs 2.0

    def test_flat_earnings_fail(self):
        letter = score_c(strong_facts(quarters=quarters([1.0, 1.0, 1.0, 1.0, 1.0, 1.0])))
        assert letter.grade is Grade.FAIL

    def test_modest_growth_is_partial(self):
        letter = score_c(strong_facts(quarters=quarters([1.15, 1.1, 1.1, 1.05, 1.0, 1.0])))
        assert letter.grade is Grade.PARTIAL

    def test_too_few_quarters_is_unknown_not_a_failure(self):
        assert score_c(strong_facts(quarters=quarters([2.0, 1.9]))).grade is Grade.UNKNOWN

    def test_a_loss_a_year_ago_is_not_infinite_growth(self):
        """From -$2 to +$1 is not '150% growth', and must not score like it."""
        letter = score_c(strong_facts(quarters=quarters([1.0, 0.5, 0.0, -1.0, -2.0, -2.0])))
        assert letter.grade is Grade.UNKNOWN

    def test_deceleration_is_noticed(self):
        facts = strong_facts(quarters=quarters([3.0, 2.9, 2.0, 1.9, 1.0, 0.5]))
        letter = score_c(facts)
        assert any("celerating" in item for item in letter.evidence)


class TestLetterA:
    def test_three_strong_years_with_high_roe_pass(self):
        assert score_a(strong_facts()).grade is Grade.PASS

    def test_a_weak_roe_downgrades_a_strong_grower(self):
        assert score_a(strong_facts(roe=0.14)).grade is Grade.PARTIAL

    def test_declining_earnings_fail(self):
        letter = score_a(strong_facts(years=years([1.0, 2.0, 3.0, 4.0]), roe=0.05))
        assert letter.grade is Grade.FAIL

    def test_too_few_years_is_unknown(self):
        assert score_a(strong_facts(years=years([6.0, 4.5]))).grade is Grade.UNKNOWN


class TestLetterN:
    def test_at_the_high_with_a_sound_base_passes(self):
        assert score_n(strong_facts()).grade is Grade.PASS

    def test_far_below_the_high_fails(self):
        assert score_n(strong_facts(price=140.0, high_52w=207.0)).grade is Grade.FAIL

    def test_the_story_half_is_declared_unmeasurable(self):
        letter = score_n(strong_facts())
        assert any("not machine-readable" in item for item in letter.evidence)

    def test_no_range_is_unknown(self):
        assert score_n(strong_facts(high_52w=None)).grade is Grade.UNKNOWN


class TestLetterS:
    def test_identical_volumes_do_not_collapse_the_up_down_split(self):
        """Splitting by value membership once made every session an "up" day."""
        from monitor.canslim.grader import score_s
        closes = [100 + (n % 2) for n in range(220)]      # strict alternation
        facts = strong_facts(closes=closes, volumes=[1_000_000] * 220)
        letter = score_s(facts)
        assert letter.grade is not Grade.UNKNOWN
        assert any("5 sessions" in item for item in letter.evidence)

    def test_heavier_volume_on_up_days_passes(self):
        from monitor.canslim.grader import score_s
        closes = [100 + (n % 2) for n in range(220)]
        volumes = [3_000_000 if n % 2 else 500_000 for n in range(220)]
        assert score_s(strong_facts(closes=closes, volumes=volumes)).grade is Grade.PASS

    def test_heavier_volume_on_down_days_fails(self):
        from monitor.canslim.grader import score_s
        closes = [100 + (n % 2) for n in range(220)]
        volumes = [500_000 if n % 2 else 3_000_000 for n in range(220)]
        assert score_s(strong_facts(closes=closes, volumes=volumes)).grade is Grade.FAIL

    def test_an_unbroken_advance_is_reported_rather_than_left_unknown(self):
        from monitor.canslim.grader import score_s
        letter = score_s(strong_facts())
        assert letter.grade is Grade.PASS
        assert any("No down day" in item for item in letter.evidence)

    def test_heavy_debt_holds_it_back(self):
        from monitor.canslim.grader import score_s
        assert score_s(strong_facts(debt_to_equity=3.0)).grade is Grade.PARTIAL


class TestLetterL:
    def test_beating_the_index_passes(self):
        assert score_l(strong_facts()).grade is Grade.PASS

    def test_lagging_the_index_fails(self):
        facts = strong_facts(closes=[100 - n * 0.1 for n in range(220)])
        assert score_l(facts).grade is Grade.FAIL

    def test_no_index_is_unknown(self):
        assert score_l(strong_facts(index_closes=[])).grade is Grade.UNKNOWN

    def test_the_two_series_are_aligned_from_today_backwards(self):
        """Aligning from the left would compare six months against twelve."""
        stock = [100.0] * 10 + [100 + n for n in range(50)]
        index = [400 + n * 0.1 for n in range(50)]
        got = relative_strength(stock, index)
        assert got is not None
        stock_pct, index_pct, ratio = got
        assert ratio == pytest.approx(stock_pct - index_pct)

    def test_a_short_series_gives_no_answer(self):
        assert relative_strength([1, 2, 3], [1, 2, 3]) is None


class TestLetterM:
    def test_a_rising_index_is_a_confirmed_uptrend(self):
        assert score_m(strong_facts()).grade is Grade.PASS

    def test_an_index_below_its_200_day_is_a_correction(self):
        facts = strong_facts(index_closes=[500 - n for n in range(220)])
        letter = score_m(facts)
        assert letter.grade is Grade.FAIL
        assert "Correction" in letter.evidence[-1]

    def test_a_short_index_history_is_unknown(self):
        assert score_m(strong_facts(index_closes=[400.0] * 50)).grade is Grade.UNKNOWN


class TestScorecard:
    def test_unknown_letters_are_excluded_from_the_denominator(self):
        """An unmeasured letter is not a failed one.

        Counting the letters the monitor cannot reach as zeros would mark every
        stock down for the tool's blind spots.
        """
        card = grade(strong_facts())
        assert card.graded < 7                       # I is never measurable here
        assert card.max_score < sum((1.5, 1.5, 1.5, 1.0, 1.0, 1.0, 1.0))
        assert card.percent > 0

    def test_the_coverage_is_stated_on_the_card(self):
        card = grade(strong_facts())
        assert f"{card.graded}/7" in card.one_liner()

    def test_a_full_card_omits_the_coverage_caveat(self):
        facts = strong_facts(institutional_holders=1200, institutional_holders_prior=1100)
        card = grade(facts)
        assert card.graded == 7
        assert "/7" not in card.one_liner()

    def test_a_strong_stock_reaches_buy_range(self):
        card = grade(strong_facts())
        assert card.verdict == "BUY-RANGE"
        assert "7-8%" in card.summary          # the framework's own stop rule

    def test_failing_earnings_is_avoid_with_the_letters_named(self):
        facts = strong_facts(quarters=quarters([1.0] * 6), years=years([1.0] * 4), roe=0.05)
        card = grade(facts)
        assert card.verdict == "AVOID"
        assert "C" in card.summary and "A" in card.summary

    def test_a_market_correction_forces_watch(self):
        facts = strong_facts(index_closes=[500 - n for n in range(220)])
        card = grade(facts)
        assert card.verdict == "WATCH"
        assert "correction" in card.summary

    def test_unmeasurable_earnings_produce_an_incomplete_verdict(self):
        facts = strong_facts(quarters=[], years=[])
        card = grade(facts)
        assert card.verdict == "INCOMPLETE"
        assert "can-slim-grader" in card.summary

    def test_the_letter_grade_tracks_the_percentage(self):
        card = grade(strong_facts())
        assert card.letter_grade in ("A", "A-", "B+", "B", "B-", "C", "D", "F")

    def test_get_finds_a_letter(self):
        assert grade(strong_facts()).get("C").key == "C"


class TestReport:
    def test_the_render_carries_verdict_evidence_and_disclaimer(self):
        text = render(grade(strong_facts()))
        assert "CAN SLIM" in text and "BUY-RANGE" in text
        assert "not investment advice" in text

    def test_partial_coverage_is_called_out(self):
        text = render(grade(strong_facts()))
        assert "letters were measurable" in text
        assert "can-slim-grader skill" in text

    def test_html_mode_escapes(self):
        text = render(grade(strong_facts(ticker="A<B")), as_html=True)
        assert "A&lt;B" in text

    def test_compact_is_a_letter_strip(self):
        strip = compact(grade(strong_facts()))
        assert strip.startswith("C") and len(strip.split()) == 7


class TestSkillHandoff:
    def test_the_skill_is_locatable_or_says_it_is_not(self):
        found = locate()
        assert (found is None) == ("not installed" in status())

    def test_the_brief_carries_the_measured_letters(self):
        text = brief(grade(strong_facts()), movement="18.35K calls opened at $200")
        assert "can-slim-grader skill" in text
        assert "18.35K calls opened" in text
        assert "C (Current quarterly earnings): PASS" in text

    def test_the_brief_names_what_it_could_not_measure(self):
        text = brief(grade(strong_facts()))
        assert "could not measure these" in text
        assert "I (Institutional sponsorship)" in text


class TestNarratorMerge:
    def _card(self):
        return grade(strong_facts())

    def test_an_unknown_soft_letter_can_be_filled(self):
        card = self._card()
        changes = merge(card, {"letters": {"I": {"grade": "pass", "comment": "Fidelity added"}},
                               "summary": "Looks strong."})
        assert card.get("I").grade is Grade.PASS
        assert changes == ["I: unknown → pass"]

    def test_a_computed_grade_can_be_lowered(self):
        card = self._card()
        merge(card, {"letters": {"C": {"grade": "partial", "comment": "one-off gain"}}})
        assert card.get("C").grade is Grade.PARTIAL

    def test_a_computed_grade_can_never_be_raised(self):
        """The asymmetry that keeps a scorecard from becoming a sales pitch."""
        card = grade(strong_facts(quarters=quarters([1.0] * 6)))
        assert card.get("C").grade is Grade.FAIL
        merge(card, {"letters": {"C": {"grade": "pass", "comment": "I like it"}}})
        assert card.get("C").grade is Grade.FAIL
        assert any("cannot raise" in item for item in card.get("C").evidence)

    def test_downgrades_can_be_switched_off(self):
        card = self._card()
        merge(card, {"letters": {"C": {"grade": "fail", "comment": "no"}}},
              allow_downgrade=False)
        assert card.get("C").grade is Grade.PASS

    def test_a_hard_letter_cannot_be_invented_from_prose(self):
        card = grade(strong_facts(quarters=[], years=[]))
        assert card.get("C").grade is Grade.UNKNOWN
        merge(card, {"letters": {"C": {"grade": "pass", "comment": "EPS looked fine"}}})
        assert card.get("C").grade is Grade.UNKNOWN
        assert any("computed letter" in item for item in card.get("C").evidence)

    def test_the_score_is_recomputed_after_a_change(self):
        card = self._card()
        before = card.score
        merge(card, {"letters": {"C": {"grade": "fail", "comment": "restated"}}})
        assert card.score < before

    def test_the_summary_is_recorded_as_a_note(self):
        card = self._card()
        merge(card, {"letters": {}, "summary": "A strong leader."})
        assert any("A strong leader." in note for note in card.notes)

    def test_a_nonsense_grade_is_ignored(self):
        card = self._card()
        merge(card, {"letters": {"C": {"grade": "brilliant", "comment": "x"}}})
        assert card.get("C").grade is Grade.PASS


class TestNarratorAvailability:
    def test_an_absent_narrator_is_reported_not_fatal(self, monkeypatch):
        """The optional dependency and the key are both soft failures.

        Whichever is missing first, the result is a NarratorUnavailable naming
        the remedy — never an exception that loses the computed scorecard.
        """
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(NarratorUnavailable) as caught:
            narrate(grade(strong_facts()))
        message = str(caught.value)
        assert "ANTHROPIC_API_KEY" in message or "requirements-narrator" in message

    def test_an_api_failure_is_reported_not_fatal(self):
        class Exploding:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("503")
        with pytest.raises(NarratorUnavailable, match="503"):
            narrate(grade(strong_facts()), client=Exploding())

    def test_a_refusal_is_reported(self):
        class Refusing:
            class messages:
                @staticmethod
                def create(**kwargs):
                    class R:
                        stop_reason = "refusal"
                        content = []
                    return R()
        with pytest.raises(NarratorUnavailable, match="declined"):
            narrate(grade(strong_facts()), client=Refusing())

    def test_the_request_uses_the_configured_model_and_adaptive_thinking(self):
        seen = {}

        class Recording:
            class messages:
                @staticmethod
                def create(**kwargs):
                    seen.update(kwargs)
                    class Block:
                        type = "text"
                        text = '{"letters": {}, "summary": "ok"}'
                    class R:
                        stop_reason = "end_turn"
                        content = [Block()]
                    return R()

        narrate(grade(strong_facts()), model="claude-opus-5", client=Recording())
        assert seen["model"] == "claude-opus-5"
        assert seen["thinking"] == {"type": "adaptive"}
        assert seen["output_config"]["format"]["type"] == "json_schema"


class TestService:
    class StubFmp:
        name = "fmp"

        def quote(self, ticker):
            return {"price": 205.0, "yearHigh": 207.0, "yearLow": 120.0,
                    "sharesOutstanding": 24e9}

        def income_statements(self, ticker, limit=12, quarterly=True):
            return quarters([2.0, 1.9, 1.8, 1.7, 1.2, 1.1]) if quarterly else years([6.0, 4.5, 3.4, 2.5])

        def key_metrics(self, ticker, limit=8):
            return [{"roe": 0.42, "debtToEquity": 0.4}]

        def daily_history(self, ticker, days=300):
            base = 400.0 if ticker == "SPY" else 100.0
            step = 0.1 if ticker == "SPY" else 0.5
            return [{"close": base + n * step, "volume": 1_000_000}
                    for n in range(219, -1, -1)]

    def test_a_card_is_produced_and_cached(self, config, store):
        service = CanSlim(config, store, fmp=self.StubFmp())
        first = service.card("NVDA")
        assert first is not None
        assert store.read_canslim("NVDA", 24) is not None
        second = service.card("NVDA")
        assert second.verdict == first.verdict
        assert [l.grade for l in second.letters] == [l.grade for l in first.letters]

    def test_fresh_bypasses_the_cache(self, config, store):
        store.cache_canslim("NVDA", {
            "ticker": "NVDA", "letters": [], "verdict": "STALE", "summary": "old",
            "score": 0, "max_score": 1, "graded": 0, "notes": [],
        })
        service = CanSlim(config, store, fmp=self.StubFmp())
        assert service.card("NVDA", fresh=True).verdict != "STALE"
        assert service.card("NVDA").verdict != "STALE"

    def test_grading_disabled_returns_nothing(self, config, store):
        config.values["canslim.enabled"] = False
        assert CanSlim(config, store, fmp=self.StubFmp()).card("NVDA") is None

    def test_no_fundamentals_source_returns_nothing(self, config, store):
        assert CanSlim(config, store, fmp=None).card("NVDA") is None

    def test_attachment_respects_the_minimum_severity(self, config, store):
        from monitor.models import Severity
        config.values["canslim.min_severity"] = "high"
        service = CanSlim(config, store, fmp=self.StubFmp())
        assert not service.should_attach(Severity.MEDIUM.rank)
        assert service.should_attach(Severity.HIGH.rank)

    def test_the_one_liner_is_short_enough_for_an_alert(self, config, store):
        line = CanSlim(config, store, fmp=self.StubFmp()).line("NVDA")
        assert line.startswith("CAN SLIM") and len(line) < 120


class TestGather:
    def test_an_unavailable_endpoint_costs_one_letter_not_the_card(self):
        class Broken(TestService.StubFmp):
            def key_metrics(self, ticker, limit=8):
                raise Unreachable("fmp", "plan does not include key metrics", ticker)

        facts = gather("NVDA", Broken())
        assert facts.roe is None
        assert any("key metrics" in note for note in facts.notes)
        assert facts.quarters, "the rest of the fetch should still have happened"

    def test_institutional_data_is_always_declared_missing(self):
        facts = gather("NVDA", TestService.StubFmp())
        assert any("13F" in note for note in facts.notes)
