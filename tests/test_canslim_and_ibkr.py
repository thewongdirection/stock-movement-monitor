"""CAN SLIM scoring and IBKR payload parsing.

The grader tests run against the real can-slim-grader skill when it's on disk
(its `relative_strength.py` is used verbatim), and skip cleanly when it isn't —
CI shouldn't fail because an optional sibling repo is absent.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_config, make_context

from monitor.canslim.fundamentals import AnnualYear, Fundamentals, Quarter, _growth
from monitor.canslim.grader import (
    FAIL,
    PARTIAL,
    PASS,
    UNKNOWN,
    grade_ticker,
)
from monitor.canslim.report import _replace_config, build_report
from monitor.canslim.skill import SkillNotAvailable, find_skill
from monitor.detectors import OptionVolumeDetector
from monitor.market_calendar import ET
from monitor.models import Bar, OptionVolumeSnapshot
from monitor.providers.ibkr import _loose_number, parse_history

try:
    SKILL = find_skill()
except SkillNotAvailable:
    SKILL = None

needs_skill = pytest.mark.skipif(SKILL is None, reason="can-slim-grader not checked out")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def price_series(n: int, start: float, drift: float, seed: int, vol: int = 3_000_000):
    rnd = random.Random(seed)
    bars, price = [], start
    day = datetime(2026, 7, 27, tzinfo=ET) - timedelta(days=n * 2)
    for _ in range(n):
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        price *= 1 + drift + rnd.uniform(-0.01, 0.01)
        bars.append(
            Bar(ts=day, open=price * 0.995, high=price * 1.008, low=price * 0.992,
                close=price, volume=int(vol * (1 + rnd.uniform(-0.2, 0.2))))
        )
    return bars


def strong_fundamentals(**kwargs) -> Fundamentals:
    f = Fundamentals(
        ticker="LEAD", company="Leader Corp", sector="Technology", industry="Software",
        latest_roe=28.0, debt_to_equity=0.4, institutional_holders=1400,
        institutional_change=85, market_cap=90e9,
    )
    f.quarters = [
        Quarter(f"Q{i}", eps=1.0, revenue=1.0, eps_growth_yoy=g, revenue_growth_yoy=g - 4)
        for i, g in enumerate([46, 38, 31, 27, 22], 1)
    ]
    f.years = [
        AnnualYear(str(2026 - i), eps=1.0, revenue=1.0, eps_growth=g, roe=28)
        for i, g in enumerate([41, 33, 28, 26])
    ]
    for key, value in kwargs.items():
        setattr(f, key, value)
    return f


def grade(daily, fundamentals, benchmark=None):
    return grade_ticker(
        "LEAD",
        skill=SKILL,
        daily=daily,
        weekly=None,
        benchmark_daily=benchmark or price_series(500, 400.0, 0.0004, 3, 80_000_000),
        fundamentals=fundamentals,
    )


# --------------------------------------------------------------------------
# Growth arithmetic
# --------------------------------------------------------------------------
def test_growth_from_a_negative_base_is_refused():
    """"EPS grew 400% from -0.05 to 0.15" is not a number anyone can act on."""
    assert _growth(0.15, -0.05) is None
    assert _growth(0.15, 0.0) is None


def test_growth_is_a_plain_percentage():
    assert _growth(1.25, 1.00) == pytest.approx(25.0)


# --------------------------------------------------------------------------
# Letter scoring
# --------------------------------------------------------------------------
@needs_skill
def test_a_leader_passes_the_earnings_and_leadership_letters():
    g = grade(price_series(500, 40.0, 0.0042, 7), strong_fundamentals())
    assert g.letter("C").score == PASS
    assert g.letter("A").score == PASS
    assert g.letter("L").score == PASS
    assert g.letter("I").score == PASS


@needs_skill
def test_a_laggard_is_avoided():
    f = Fundamentals(ticker="LAGG", latest_roe=6.0, institutional_holders=20)
    f.quarters = [Quarter("Q1", eps=1.0, revenue=1.0, eps_growth_yoy=-14, revenue_growth_yoy=-9)]
    f.years = [AnnualYear("2026", eps=1.0, revenue=1.0, eps_growth=-19, roe=6)]
    g = grade(price_series(500, 120.0, -0.0025, 11), f)
    assert g.verdict == "AVOID"
    assert g.letter("C").score == FAIL


@needs_skill
def test_missing_fundamentals_produce_unknown_not_fail():
    """Plan-gated data must not read as a failing grade."""
    g = grade(price_series(500, 40.0, 0.004, 7), Fundamentals(ticker="DARK"))
    assert g.letter("C").score == UNKNOWN
    assert g.letter("A").score == UNKNOWN
    assert g.letter("I").score == UNKNOWN
    assert g.verdict == "WATCH"
    assert "could not be graded" in g.summary


@needs_skill
def test_strong_eps_with_weak_sales_is_only_a_partial():
    """Margin-driven growth is weaker evidence than demand-driven growth."""
    f = strong_fundamentals()
    f.quarters = [
        Quarter("Q1", eps=1.0, revenue=1.0, eps_growth_yoy=40, revenue_growth_yoy=3),
        Quarter("Q2", eps=1.0, revenue=1.0, eps_growth_yoy=30, revenue_growth_yoy=2),
    ]
    g = grade(price_series(500, 40.0, 0.004, 7), f)
    assert g.letter("C").score == PARTIAL
    assert "sales" in g.letter("C").read


@needs_skill
def test_a_down_year_fails_the_annual_letter():
    f = strong_fundamentals()
    f.years = [
        AnnualYear("2026", eps=1.0, revenue=1.0, eps_growth=30, roe=28),
        AnnualYear("2025", eps=1.0, revenue=1.0, eps_growth=-12, roe=28),
        AnnualYear("2024", eps=1.0, revenue=1.0, eps_growth=28, roe=28),
    ]
    g = grade(price_series(500, 40.0, 0.004, 7), f)
    assert g.letter("A").score == FAIL
    assert "down year" in g.letter("A").read


@needs_skill
def test_low_roe_downgrades_the_annual_letter():
    f = strong_fundamentals(latest_roe=13.0)
    g = grade(price_series(500, 40.0, 0.004, 7), f)
    assert g.letter("A").score == PARTIAL


@needs_skill
def test_a_stock_at_highs_with_no_base_is_extended_not_a_breakout():
    """The methodology's core distinction: a base to break out *from*.

    A stock in a continuous run has no pivot, so buying it is chasing — N must
    not pass just because the price is at a high.
    """
    g = grade(price_series(500, 40.0, 0.005, 7), strong_fundamentals())
    n = g.letter("N")
    assert n.score == PARTIAL
    assert "no sound base" in n.read
    assert g.verdict != "BUY-RANGE"


@needs_skill
def test_the_qualitative_half_of_n_is_declared_not_invented():
    g = grade(price_series(500, 40.0, 0.004, 7), strong_fundamentals())
    assert "qualitative call not made here" in g.letter("N").read


@needs_skill
def test_no_price_history_leaves_the_technical_letters_unknown():
    g = grade([], strong_fundamentals())
    assert g.letter("N").score == UNKNOWN
    assert g.letter("L").score == UNKNOWN
    assert g.letter("S").score == UNKNOWN
    assert any("no price history" in w for w in g.warnings)


@needs_skill
def test_score_totals_pass_as_one_and_partial_as_half():
    g = grade(price_series(500, 40.0, 0.004, 7), strong_fundamentals())
    total = sum(
        {PASS: 1.0, PARTIAL: 0.5, UNKNOWN: 0.5, FAIL: 0.0}[L.score] for L in g.letters
    )
    assert g.score == pytest.approx(total)
    assert g.score_text.endswith("/ 7")


@needs_skill
def test_the_one_line_summary_is_compact_and_complete():
    g = grade(price_series(500, 40.0, 0.004, 7), strong_fundamentals())
    line = g.one_line()
    for key in "CANSLIM":
        assert key in line
    assert g.verdict in line


@needs_skill
def test_a_market_correction_tightens_the_stop_to_three_percent():
    """The methodology's own rule: 7-8% normally, 3% in a correction."""
    falling = price_series(500, 400.0, -0.003, 21, 80_000_000)
    g = grade(price_series(500, 40.0, 0.004, 7), strong_fundamentals(), benchmark=falling)
    assert g.letter("M").score == FAIL
    assert "-3%" in g.stop


@needs_skill
def test_thin_history_is_flagged_as_weaker_evidence():
    g = grade(price_series(60, 40.0, 0.004, 7), strong_fundamentals())
    assert any("relative-strength" in w for w in g.warnings)


# --------------------------------------------------------------------------
# Report rendering
# --------------------------------------------------------------------------
@needs_skill
def test_report_html_is_written_and_its_config_is_valid_json(tmp_path):
    import json
    import re

    g = grade(price_series(500, 40.0, 0.004, 7), strong_fundamentals())
    report = build_report(g, SKILL, tmp_path, want_pdf=False)
    assert report.html_path.exists()

    src = report.html_path.read_text()
    start = src.index("{", re.search(r"const CONFIG\s*=\s*", src).start())
    depth, end = 0, start
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    config = json.loads(src[start : end + 1])
    assert config["ticker"] == "LEAD"
    assert len(config["letters"]) == 7
    assert config["verdict"]["label"] == g.verdict
    assert "not investment advice" in config["disclaimer"]


@needs_skill
def test_the_report_declares_that_letters_were_scored_programmatically(tmp_path):
    g = grade(price_series(500, 40.0, 0.004, 7), strong_fundamentals())
    report = build_report(g, SKILL, tmp_path, want_pdf=False)
    assert "programmatically" in report.html_path.read_text()


def test_replace_config_handles_braces_inside_strings():
    """A CONFIG containing '{' in a string must not break brace matching."""
    template = 'x const CONFIG = {\n  a: "has { brace",\n  b: 1\n}\ny'
    out = _replace_config(template, {"a": 2})
    assert out.startswith("x const CONFIG = {")
    assert out.endswith("y")
    assert '"a": 2' in out
    assert "has { brace" not in out


def test_replace_config_rejects_a_restructured_template():
    with pytest.raises(ValueError, match="no `const CONFIG"):
        _replace_config("<html>nothing here</html>", {})


def test_missing_skill_reports_the_clone_command(monkeypatch):
    """With nowhere left to look, the error must be actionable, not just 'not found'."""
    from monitor.canslim import skill as skill_mod

    monkeypatch.delenv("CANSLIM_SKILL_PATH", raising=False)
    monkeypatch.setattr(skill_mod, "CANDIDATE_PATHS", ())
    with pytest.raises(SkillNotAvailable, match="git clone"):
        find_skill("/nonexistent/path/for/sure")


def test_an_explicit_path_does_not_disable_the_fallbacks(monkeypatch, tmp_path):
    """A wrong configured path should still find a skill that is present."""
    from monitor.canslim import skill as skill_mod

    if SKILL is None:
        pytest.skip("no skill checked out to fall back to")
    monkeypatch.setattr(skill_mod, "CANDIDATE_PATHS", (str(SKILL.root),))
    found = find_skill(str(tmp_path / "not-here"))
    assert found.root == SKILL.root


# --------------------------------------------------------------------------
# IBKR parsing
# --------------------------------------------------------------------------
def test_parses_the_row_shaped_history():
    payload = {
        "data": [
            {"t": 1785189600000, "o": 335.0, "h": 336.0, "l": 334.5, "c": 335.8, "v": 1200},
            {"t": 1785189300000, "o": 334.0, "h": 335.5, "l": 333.9, "c": 335.0, "v": 900},
        ]
    }
    bars = parse_history(payload, volume_multiplier=100)
    assert len(bars) == 2
    assert bars[0].ts < bars[1].ts
    # Row-shaped history quotes volume in lots of 100.
    assert bars[0].volume == 90_000


def test_parses_the_column_shaped_history():
    payload = {
        "time": ["2026-07-27T13:30:00Z", "2026-07-27T13:30:30Z"],
        "open": [334.99, 335.67],
        "high": [336.87, 336.11],
        "low": [334.10, 335.33],
        "close": [335.63, 335.34],
        "volume": [881737, 84733],
    }
    bars = parse_history(payload, volume_multiplier=100)
    assert len(bars) == 2
    # Column-shaped responses report actual shares — the multiplier must not apply.
    assert bars[0].volume == 881_737
    assert bars[0].ts.tzinfo is not None


def test_ragged_columns_are_truncated_not_crashed():
    payload = {
        "time": ["2026-07-27T13:30:00Z", "2026-07-27T13:30:30Z", "2026-07-27T13:31:00Z"],
        "open": [1.0, 2.0],
        "high": [1.0, 2.0],
        "low": [1.0, 2.0],
        "close": [1.0, 2.0],
        "volume": [10, 20],
    }
    assert len(parse_history(payload, 1)) == 2


def test_unrecognised_history_shape_yields_nothing():
    assert parse_history({"unexpected": True}, 100) == []
    assert parse_history("not a mapping", 100) == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("336.80", 336.80),
        ("1.2M", 1_200_000.0),
        ("4.95K", 4_950.0),
        ("2.5B", 2_500_000_000.0),
        ("C336.80", 336.80),
        ("1,059,794", 1_059_794.0),
        (49_554_255.0, 49_554_255.0),
        ("", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_ibkr_display_strings_are_parsed(raw, expected):
    assert _loose_number(raw) == expected


# --------------------------------------------------------------------------
# Option-volume detector
# --------------------------------------------------------------------------
def option_settings(**overrides):
    return make_config(option_volume=overrides).detector("option_volume")


MIDDAY = datetime(2026, 7, 27, 13, 0, tzinfo=ET)  # ~54% through the session


def snapshot(today=3_000_000, average=1_000_000, calls=2_500_000, puts=500_000):
    return OptionVolumeSnapshot(
        ticker="TEST", today_volume=today, average_volume=average,
        call_volume=calls, put_volume=puts,
    )


def test_elevated_chain_volume_fires(state):
    ctx = make_context(state, option_settings(), now=MIDDAY, option_volume=snapshot())
    alerts = OptionVolumeDetector().run(ctx)
    assert len(alerts) == 1
    assert "option volume" in alerts[0].headline.lower()


def test_the_ratio_is_pace_adjusted_for_the_session(state):
    """3x by 1pm is a bigger day than 3x by the close; the alert must say so."""
    ctx = make_context(state, option_settings(), now=MIDDAY, option_volume=snapshot())
    alerts = OptionVolumeDetector().run(ctx)
    assert "pace-adjusted" in alerts[0].lines[0]
    # 3.0 raw / ~0.54 elapsed is well above 3.
    assert "5." in alerts[0].lines[0] or "6." in alerts[0].lines[0]


def test_early_morning_is_held_back(state):
    """Day-cumulative volume compared to a full-day average is noise at 09:35."""
    early = datetime(2026, 7, 27, 9, 35, tzinfo=ET)
    ctx = make_context(state, option_settings(), now=early, option_volume=snapshot())
    assert OptionVolumeDetector().run(ctx) == []
    assert any("min_session_pct" in n for n in ctx.notes)


def test_a_normal_day_is_silent(state):
    ctx = make_context(
        state, option_settings(), now=MIDDAY,
        option_volume=snapshot(today=600_000, average=1_000_000),
    )
    assert OptionVolumeDetector().run(ctx) == []


def test_a_thin_chain_is_ignored(state):
    ctx = make_context(
        state, option_settings(min_contracts=5_000), now=MIDDAY,
        option_volume=snapshot(today=900, average=100),
    )
    assert OptionVolumeDetector().run(ctx) == []


def test_a_one_sided_skew_escalates(state):
    balanced = OptionVolumeDetector().run(
        make_context(state, option_settings(), now=MIDDAY,
                     option_volume=snapshot(calls=1_500_000, puts=1_500_000))
    )
    state.flush_progress()
    skewed = OptionVolumeDetector().run(
        make_context(state, option_settings(cooldown_minutes=0), now=MIDDAY + timedelta(days=1),
                     option_volume=snapshot(calls=2_800_000, puts=200_000))
    )
    assert balanced and skewed
    assert skewed[0].severity.rank > balanced[0].severity.rank


def test_missing_option_data_is_silent(state):
    ctx = make_context(state, option_settings(), now=MIDDAY, option_volume=None)
    assert OptionVolumeDetector().run(ctx) == []

    ctx2 = make_context(
        state, option_settings(), now=MIDDAY,
        option_volume=OptionVolumeSnapshot("TEST", None, None),
    )
    assert OptionVolumeDetector().run(ctx2) == []


def test_zero_average_does_not_divide_by_zero(state):
    ctx = make_context(
        state, option_settings(), now=MIDDAY,
        option_volume=OptionVolumeSnapshot("TEST", 1_000_000, 0),
    )
    assert OptionVolumeDetector().run(ctx) == []


def test_it_alerts_once_per_session(state):
    """A cumulative daily figure re-alerting as it climbs is the same fact twice."""
    detector = OptionVolumeDetector()
    first = detector.run(
        make_context(state, option_settings(cooldown_minutes=0), now=MIDDAY,
                     option_volume=snapshot())
    )
    assert len(first) == 1
    later = detector.run(
        make_context(state, option_settings(cooldown_minutes=0),
                     now=MIDDAY + timedelta(hours=2),
                     option_volume=snapshot(today=4_000_000))
    )
    assert later[0].dedup_id == first[0].dedup_id


def test_the_alert_states_its_own_limits(state):
    ctx = make_context(state, option_settings(), now=MIDDAY, option_volume=snapshot())
    joined = " ".join(OptionVolumeDetector().run(ctx)[0].lines)
    assert "no strike, premium or direction" in joined
    assert "earnings" in joined
