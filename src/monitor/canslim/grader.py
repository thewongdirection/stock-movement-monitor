"""The seven letters, scored against the skill's published rubric.

Thresholds come from ``references/data-and-scoring-guide.md`` in the
can-slim-grader skill. Where a letter genuinely needs judgement the score says
so instead of inventing one — see `UNKNOWN`.

Scoring: pass = 1, partial = 0.5, fail = 0, out of 7. The verdict is *not* that
score, though — per the guide it turns on the core letters (C, A, L) plus a
valid N, because a high total built on the easy letters is exactly the mistake
the methodology warns against.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..models import Bar
from .fundamentals import Fundamentals
from .skill import SkillPaths, load_relative_strength

log = logging.getLogger(__name__)

PASS, PARTIAL, FAIL, UNKNOWN = "pass", "partial", "fail", "unknown"

LETTER_NAMES = {
    "C": "Current quarterly earnings & sales",
    "A": "Annual earnings growth",
    "N": "New — and a new high off a base",
    "S": "Supply & demand",
    "L": "Leader, not laggard",
    "I": "Institutional sponsorship",
    "M": "Market direction",
}

#: An unknown letter is worth the same as a partial when totalling, because
#: treating missing data as a failure would systematically avoid every name
#: whose data happens to be plan-gated.
POINTS = {PASS: 1.0, PARTIAL: 0.5, UNKNOWN: 0.5, FAIL: 0.0}


@dataclass
class LetterScore:
    key: str
    score: str
    threshold: str
    actual: str
    read: str

    @property
    def name(self) -> str:
        return LETTER_NAMES[self.key]

    @property
    def template_score(self) -> str:
        """The template understands pass/partial/fail; unknown renders as partial."""
        return PARTIAL if self.score == UNKNOWN else self.score


@dataclass
class Grade:
    ticker: str
    company: str
    as_of: str
    price: float | None
    letters: list[LetterScore]
    verdict: str
    tone: str
    summary: str
    technicals: dict[str, Any] = field(default_factory=dict)
    entry: str = "None now"
    entry_note: str = ""
    stop: str = ""
    stop_note: str = ""
    essentials: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data_sources: str = "IBKR/FMP price + FMP fundamentals"

    @property
    def score(self) -> float:
        return sum(POINTS[letter.score] for letter in self.letters)

    @property
    def score_text(self) -> str:
        total = self.score
        return f"{total:g} / 7"

    def letter(self, key: str) -> LetterScore | None:
        return next((letter for letter in self.letters if letter.key == key), None)

    def one_line(self) -> str:
        parts = " ".join(
            f"{letter.key}{_mark(letter.score)}" for letter in self.letters
        )
        return f"{self.verdict} · {self.score_text} · {parts}"


def _mark(score: str) -> str:
    return {PASS: "✓", PARTIAL: "~", FAIL: "✗", UNKNOWN: "?"}[score]


# --------------------------------------------------------------------------
def grade_ticker(
    ticker: str,
    *,
    skill: SkillPaths,
    daily: list[Bar],
    weekly: list[Bar] | None,
    benchmark_daily: list[Bar],
    fundamentals: Fundamentals,
    now: datetime | None = None,
) -> Grade:
    """Grade one ticker. Never raises on thin data — letters go unknown instead."""
    now = now or datetime.now()
    warnings = list(fundamentals.warnings)

    weekly = weekly or _to_weekly(daily)
    technicals = _technicals(skill, ticker, daily, weekly, benchmark_daily, warnings)
    market = _market_direction(benchmark_daily)

    letters = [
        _score_c(fundamentals),
        _score_a(fundamentals),
        _score_n(technicals, fundamentals),
        _score_s(technicals, fundamentals, daily),
        _score_l(technicals),
        _score_i(fundamentals),
        _score_m(market),
    ]

    price = fundamentals.price or (daily[-1].close if daily else None)
    verdict, tone, summary = _verdict(letters, technicals)
    entry, entry_note, stop, stop_note = _entry_stop(verdict, technicals, market, price)

    return Grade(
        ticker=ticker.upper(),
        company=_company_line(fundamentals),
        as_of=(daily[-1].ts.date().isoformat() if daily else now.date().isoformat()),
        price=price,
        letters=letters,
        verdict=verdict,
        tone=tone,
        summary=summary,
        technicals={**technicals, "market": market},
        entry=entry,
        entry_note=entry_note,
        stop=stop,
        stop_note=stop_note,
        essentials=_essentials(fundamentals),
        warnings=warnings,
    )


# -- technicals -------------------------------------------------------------
def _technicals(
    skill: SkillPaths,
    ticker: str,
    daily: list[Bar],
    weekly: list[Bar],
    benchmark: list[Bar],
    warnings: list[str],
) -> dict[str, Any]:
    """Run the skill's own relative_strength.py rather than reimplementing it."""
    if not daily or not benchmark:
        warnings.append("no price history — every technical letter is unknown")
        return {}
    try:
        module = load_relative_strength(skill)
        payload = {
            "benchmark": {"symbol": "SPY", "daily": [_row(b) for b in benchmark]},
            "candidates": [
                {
                    "symbol": ticker.upper(),
                    "daily": [_row(b) for b in daily],
                    "weekly": [_row(b) for b in weekly],
                }
            ],
        }
        result = module.analyze(payload)
    except Exception as exc:  # noqa: BLE001 - a skill change must not break grading
        warnings.append(f"relative_strength.py failed ({type(exc).__name__}: {exc})")
        return {}

    rows = result.get("candidates") if isinstance(result, dict) else None
    if not rows:
        return {}
    row = rows[0] if isinstance(rows[0], dict) else {}
    if len(daily) < 130:
        warnings.append(
            f"only {len(daily)} daily bars — the 6- and 12-month relative-strength "
            "legs are incomplete, so RS is weaker evidence than usual"
        )
    return _normalise(row, daily)


def _normalise(row: dict[str, Any], daily: list[Bar]) -> dict[str, Any]:
    """Convert the skill's raw output into percentages, and derive the pivot.

    ``relative_strength.py`` reports fractions (0.20 = 20%) and does not emit a
    pivot price — it gives how far below the base peak the last close sits, from
    which the peak (and therefore the buy point) follows.
    """
    base = row.get("base") or {}
    below_peak = base.get("pct_below_base_peak")
    last_close = daily[-1].close if daily else None

    pivot = None
    if last_close and below_peak is not None and below_peak < 1:
        # below_peak = (peak - last) / peak  =>  peak = last / (1 - below_peak)
        pivot = last_close / (1 - below_peak)

    return {
        "symbol": row.get("symbol"),
        "rs_blend_pct": _to_pct(row.get("rs_blended")),
        "rs_legs_pct": {
            leg: _to_pct(value)
            for leg, value in (row.get("rs_relative_return") or {}).items()
        },
        "off_high_pct": _to_pct(row.get("pct_off_52w_high")),
        "breakout_vol_pct": _to_pct(row.get("breakout_vol_vs_avg")),
        "base_depth_pct": _to_pct(base.get("base_depth_pct")),
        "base_length_weeks": base.get("base_length_weeks"),
        "pct_below_base_peak": _to_pct(below_peak),
        "wide_loose": bool(base.get("wide_and_loose_flag")),
        "pivot": pivot,
        "last_close": last_close,
    }


def _to_pct(value: Any) -> float | None:
    """The RS script speaks in fractions; the report and thresholds use percent."""
    if value is None:
        return None
    try:
        return float(value) * 100.0
    except (TypeError, ValueError):
        return None


def _row(bar: Bar) -> list:
    return [bar.ts.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume]


def _to_weekly(daily: list[Bar]) -> list[Bar]:
    """Aggregate daily bars into weekly ones — saves a second API call."""
    buckets: dict[tuple[int, int], list[Bar]] = {}
    for bar in daily:
        iso = bar.ts.isocalendar()
        buckets.setdefault((iso[0], iso[1]), []).append(bar)
    out: list[Bar] = []
    for key in sorted(buckets):
        week = sorted(buckets[key], key=lambda b: b.ts)
        out.append(
            Bar(
                ts=week[0].ts,
                open=week[0].open,
                high=max(b.high for b in week),
                low=min(b.low for b in week),
                close=week[-1].close,
                volume=sum(b.volume for b in week),
            )
        )
    return out


def _market_direction(benchmark: list[Bar]) -> dict[str, Any]:
    """Classify M: confirmed uptrend / under pressure / correction.

    Distribution day, per the standard definition: the index closes down at
    least 0.2% on volume higher than the prior session. Five or more inside a
    rolling 25-session window is the conventional warning threshold.
    """
    if len(benchmark) < 60:
        return {"state": UNKNOWN, "label": "unknown", "detail": "not enough index history"}

    closes = [b.close for b in benchmark]
    ma50 = statistics.fmean(closes[-50:])
    ma200 = statistics.fmean(closes[-200:]) if len(closes) >= 200 else None
    last = closes[-1]

    window = benchmark[-25:]
    distribution = 0
    for previous, current in zip(window, window[1:]):
        if previous.close <= 0:
            continue
        drop = (current.close - previous.close) / previous.close * 100
        if drop <= -0.2 and current.volume > previous.volume:
            distribution += 1

    above_50 = last > ma50
    above_200 = ma200 is None or last > ma200

    # Five distribution days in a 25-session window is the conventional warning
    # level, six or more the serious one — so a trend is only "under pressure"
    # from five, not from four.
    if above_50 and above_200 and distribution <= 4:
        state, label = PASS, "Confirmed uptrend"
    elif above_200 and distribution <= 6:
        state, label = PARTIAL, "Uptrend under pressure"
    else:
        state, label = FAIL, "Correction / downtrend"

    return {
        "state": state,
        "label": label,
        "detail": (
            f"SPY {last:,.2f} vs 50-day {ma50:,.2f}"
            + (f" / 200-day {ma200:,.2f}" if ma200 else "")
            + f"; {distribution} distribution day(s) in 25 sessions"
        ),
        "distribution_days": distribution,
    }


# -- letters ----------------------------------------------------------------
def _score_c(f: Fundamentals) -> LetterScore:
    threshold = "EPS & sales up >=25% YoY, accelerating"
    if not f.has_quarterly:
        return LetterScore(
            "C", UNKNOWN, threshold, "no quarterly data",
            "Quarterly earnings could not be retrieved, so the most important "
            "letter in the model is ungraded. Treat the whole verdict as "
            "provisional until this is filled in.",
        )

    latest = next(q for q in f.quarters if q.eps_growth_yoy is not None)
    eps_growth = latest.eps_growth_yoy
    sales_growth = latest.revenue_growth_yoy

    prior = [q for q in f.quarters if q.eps_growth_yoy is not None][1:2]
    accelerating = bool(prior and eps_growth is not None and prior[0].eps_growth_yoy is not None
                        and eps_growth > prior[0].eps_growth_yoy)

    actual = f"EPS {_pct_text(eps_growth)}" + (
        f", sales {_pct_text(sales_growth)}" if sales_growth is not None else ""
    )

    if eps_growth is None:
        return LetterScore(
            "C", UNKNOWN, threshold, actual,
            "EPS grew from a negative or zero base, so a percentage would be "
            "meaningless. Judge the absolute figures directly.",
        )

    sales_ok = sales_growth is not None and sales_growth >= 25
    if eps_growth >= 25 and (sales_ok or (sales_growth or 0) >= 20):
        score = PASS
        read = (
            f"Latest quarter EPS {_pct_text(eps_growth)} on sales "
            f"{_pct_text(sales_growth)} — clears the 25% bar on both"
            + (" and accelerating from the prior quarter." if accelerating else ".")
        )
    elif eps_growth >= 25:
        score = PARTIAL
        read = (
            f"EPS {_pct_text(eps_growth)} clears the bar but sales "
            f"{_pct_text(sales_growth)} lag it — margin-driven growth is weaker "
            "evidence than demand-driven growth."
        )
    elif eps_growth >= 10:
        score = PARTIAL
        read = (
            f"EPS {_pct_text(eps_growth)} is positive but short of the 25% the "
            "model wants."
        )
    else:
        score = FAIL
        read = (
            f"EPS {_pct_text(eps_growth)} — below 10%. This is the core earnings "
            "letter, and it fails."
        )
    if score == PASS and not accelerating and prior:
        read += " Growth is not accelerating quarter on quarter, which is the softer half of C."
    return LetterScore("C", score, threshold, actual, read)


def _score_a(f: Fundamentals) -> LetterScore:
    threshold = "EPS up >=25%/yr for 3 yrs, ROE >=17%"
    if not f.has_annual:
        return LetterScore(
            "A", UNKNOWN, threshold, "no annual data",
            "Annual earnings history was unavailable, so multi-year consistency "
            "is ungraded.",
        )

    growths = [y.eps_growth for y in f.years if y.eps_growth is not None][:3]
    roe = f.latest_roe
    actual = (
        "EPS " + ", ".join(_pct_text(g) for g in growths) if growths else "EPS history thin"
    ) + (f"; ROE {roe:.0f}%" if roe is not None else "; ROE n/a")

    strong_years = [g for g in growths if g >= 25]
    down_years = [g for g in growths if g < 0]

    if len(growths) >= 3 and len(strong_years) == 3 and (roe or 0) >= 17:
        score = PASS
        read = (
            "Three consecutive years of 25%+ EPS growth with ROE at "
            f"{roe:.0f}% — the durable-growth profile the model is built on."
        )
    elif len(growths) >= 2 and all(g >= 10 for g in growths[:2]) and (roe is None or roe >= 12):
        score = PARTIAL
        read = (
            "Annual growth is in the 10-25% band or ROE is between 12% and 17% — "
            "respectable, short of the model's threshold."
        )
    elif down_years:
        score = FAIL
        read = (
            f"{len(down_years)} down year(s) in the last three — erratic annual "
            "earnings are a fail for A."
        )
    else:
        score = FAIL
        read = (
            "Annual growth and/or ROE are below the model's floor "
            f"({actual})."
        )
    return LetterScore("A", score, threshold, actual, read)


def _score_n(technicals: dict[str, Any], f: Fundamentals) -> LetterScore:
    threshold = "New driver + breakout to a new high from a sound base"
    off_high = technicals.get("off_high_pct")
    depth = technicals.get("base_depth_pct")
    length = technicals.get("base_length_weeks")
    loose = technicals.get("wide_loose")

    if off_high is None:
        return LetterScore(
            "N", UNKNOWN, threshold, "no price history",
            "Without price history neither the new-high test nor the base can be "
            "assessed.",
        )

    actual = f"{off_high:.1f}% off 52-wk high"
    if depth is not None:
        actual += f", base {depth:.0f}% deep"
    if length:
        actual += f" over ~{length} wks"

    # The "new product / management / condition" half of N is a qualitative
    # judgement this deterministic pass cannot make. Say so rather than imply it.
    caveat = (
        " The 'new' half of N — a new product, management or industry condition — "
        "is a qualitative call not made here; only the chart half is graded."
    )

    # N is "a new high *out of a sound base*". A stock at its high with no base
    # behind it is extended, not breaking out — buying that is chasing, which is
    # exactly what the methodology warns against. A usable base needs some
    # length and some depth: roughly 4+ weeks and 8%+, and not wide and loose.
    has_base = (
        depth is not None
        and length is not None
        and length >= 4
        and depth >= 8
        and not loose
    )

    if off_high <= 5 and has_base:
        score = PASS
        read = (
            f"Within {off_high:.1f}% of its 52-week high out of a base roughly "
            f"{depth:.0f}% deep and {length} weeks long. The base is measured "
            "heuristically, not pattern-classified." + caveat
        )
    elif off_high <= 5:
        score = PARTIAL
        read = (
            f"At its highs ({off_high:.1f}% off) but with no sound base behind it"
            + (
                f" — the consolidation measures only {depth:.0f}% deep over "
                f"{length} week(s)."
                if depth is not None and length
                else "."
            )
            + " That is an extended stock in a continuous run, not a breakout "
            "from a base, so there is no proper pivot to buy against." + caveat
        )
    elif off_high <= 15:
        score = PARTIAL
        read = (
            f"{off_high:.1f}% off the high — either extended from a pivot or "
            "still repairing the base, so there is no clean buy point right now."
            + caveat
        )
    else:
        score = FAIL
        read = (
            f"{off_high:.1f}% below its 52-week high"
            + (" in a wide, loose base." if loose else ".")
            + " Not a breakout candidate."
            + caveat
        )
    return LetterScore("N", score, threshold, actual, read)


def _score_s(technicals: dict[str, Any], f: Fundamentals, daily: list[Bar]) -> LetterScore:
    threshold = "Volume surging on up-moves, sane float, low debt"
    breakout = technicals.get("breakout_vol_pct")
    debt = f.debt_to_equity
    bits = []
    if breakout is not None:
        bits.append(f"latest volume {_pct_text(breakout)} vs avg")
    if debt is not None:
        bits.append(f"debt/equity {debt:.2f}")
    actual = "; ".join(bits) or "volume/float data thin"

    if breakout is None:
        return LetterScore(
            "S", UNKNOWN, threshold, actual,
            "Volume history was unavailable, so accumulation could not be judged.",
        )

    heavy_debt = debt is not None and debt > 2.0
    if breakout >= 40 and not heavy_debt:
        score = PASS
        read = (
            f"Latest session traded {_pct_text(breakout)} against its average — "
            "the volume confirmation the model wants on a move."
        )
    elif breakout >= 0 and not heavy_debt:
        score = PARTIAL
        read = (
            f"Volume {_pct_text(breakout)} versus average — present but not the "
            "40-50% surge that marks real accumulation."
        )
    else:
        score = FAIL
        read = (
            f"Volume {_pct_text(breakout)} versus average"
            + (f" with debt/equity at {debt:.2f}." if heavy_debt else ".")
            + " No sign of accumulation."
        )
    return LetterScore("S", score, threshold, actual, read)


def _score_l(technicals: dict[str, Any]) -> LetterScore:
    threshold = "RS clearly ahead of SPY; top of a strong group"
    rs = technicals.get("rs_blend_pct")
    if rs is None:
        return LetterScore(
            "L", UNKNOWN, threshold, "no RS available",
            "Relative strength could not be computed, so leadership is ungraded.",
        )

    actual = f"RS proxy {rs:+.1f}pp vs SPY"
    if rs >= 20:
        score = PASS
        read = (
            f"Outperforming SPY by {rs:+.1f} percentage points on the blended "
            "3/6/12-month proxy — "
            "leadership, which is one of the three letters that carry the verdict."
        )
    elif rs >= -5:
        score = PARTIAL
        read = (
            f"Roughly in line with SPY ({rs:+.1f}pp). The model wants leaders, not "
            "market performers."
        )
    else:
        score = FAIL
        read = (
            f"Lagging SPY by {rs:+.1f}pp. A laggard is what this methodology "
            "explicitly avoids, however cheap it looks."
        )
    return LetterScore("L", score, threshold, actual, read)


def _score_i(f: Fundamentals) -> LetterScore:
    threshold = "Several quality funds, holder count increasing"
    holders = f.institutional_holders
    change = f.institutional_change
    if holders is None:
        return LetterScore(
            "I", UNKNOWN, threshold, "ownership data unavailable",
            "Institutional holder counts were not retrievable. The quality half "
            "of I — whether the sponsors are funds worth following — needs "
            "judgement this pass does not attempt.",
        )
    actual = f"{holders:,} holders" + (f" ({change:+,} QoQ)" if change is not None else "")
    if change is not None and change > 0 and holders >= 100:
        score = PASS
        read = f"{holders:,} institutional holders and rising ({change:+,}) — sponsorship is building."
    elif holders >= 50:
        score = PARTIAL
        read = (
            f"{holders:,} holders but the count is flat or falling"
            + (f" ({change:+,})." if change is not None else ".")
        )
    else:
        score = FAIL
        read = f"Only {holders:,} institutional holders — thin sponsorship."
    return LetterScore("I", score, threshold, actual, read)


def _score_m(market: dict[str, Any]) -> LetterScore:
    threshold = "Confirmed uptrend in the general market"
    state = market.get("state", UNKNOWN)
    return LetterScore(
        "M",
        state,
        threshold,
        str(market.get("label", "unknown")),
        f"{market.get('label', 'Unknown')} — {market.get('detail', 'no index data')}. "
        "M is market-wide context; three quarters of stocks follow it.",
    )


# -- verdict ----------------------------------------------------------------
def _verdict(letters: list[LetterScore], technicals: dict[str, Any]) -> tuple[str, str, str]:
    by_key = {letter.key: letter for letter in letters}
    core = [by_key[k].score for k in ("C", "A")]
    leadership = by_key["L"].score
    new_high = by_key["N"].score
    market = by_key["M"].score

    if FAIL in core:
        failing = [k for k in ("C", "A") if by_key[k].score == FAIL]
        return (
            "AVOID",
            "down",
            f"Fails the core earnings letter{'s' if len(failing) > 1 else ''} "
            f"{', '.join(failing)}. Strong price action alone is not enough "
            "without earnings behind it.",
        )
    if leadership == FAIL:
        return (
            "AVOID",
            "down",
            "A laggard on relative strength. The method avoids beaten-down names "
            "however cheap they look.",
        )
    if core.count(PASS) == 2 and leadership == PASS and new_high == PASS and market != FAIL:
        return (
            "BUY-RANGE",
            "up",
            "Passes C, A and L with a valid breakout setup. Do not chase more "
            "than 5% past the pivot.",
        )
    if UNKNOWN in core:
        return (
            "WATCH",
            "pressure",
            "The earnings letters could not be graded on data, so no buy verdict "
            "is possible. Fill in C and A before acting on this.",
        )
    missing = []
    if new_high != PASS:
        off = technicals.get("off_high_pct")
        missing.append(
            f"no valid buy point ({off:.1f}% off the high)" if off is not None
            else "no valid buy point"
        )
    if market == FAIL:
        missing.append("the general market is in a correction")
    if leadership != PASS:
        missing.append("relative strength is only in line with the market")
    return (
        "WATCH",
        "pressure",
        "Fundamentals hold up but " + "; ".join(missing) + "."
        if missing
        else "Fundamentals hold up but there is no actionable setup right now.",
    )


def _entry_stop(
    verdict: str, technicals: dict[str, Any], market: dict[str, Any], price: float | None
) -> tuple[str, str, str, str]:
    """The framework's proposed entry and stop — a rule, not a recommendation."""
    correction = market.get("state") == FAIL
    stop_pct = 3.0 if correction else 8.0
    stop_note = (
        f"Cut {stop_pct:.0f}% below your buy"
        + (" (tightened because the general market is in a correction)." if correction
           else "; 3% in a market correction.")
        + " No exceptions."
    )

    pivot = technicals.get("pivot")
    if verdict == "BUY-RANGE" and pivot:
        chase = pivot * 1.05
        stop = pivot * (1 - stop_pct / 100)
        return (
            f"${pivot:,.2f} (pivot); buy up to +5% (${chase:,.2f})",
            "Breakout above the base high needs volume +40-50%",
            f"${stop:,.2f} (-{stop_pct:.0f}% from pivot)",
            stop_note,
        )

    condition = (
        "a follow-through day to confirm a new market uptrend"
        if correction
        else "a fresh base and a breakout on volume"
    )
    return (
        "None now",
        f"Needs {condition} before any entry is valid",
        f"-{stop_pct:.0f}% from whatever your entry turns out to be",
        stop_note,
    )


# -- presentation helpers ---------------------------------------------------
def _company_line(f: Fundamentals) -> str:
    bits = [f.company or f.ticker]
    group = " / ".join(x for x in (f.sector, f.industry) if x)
    if group:
        bits.append(group)
    return " - ".join(bits)


def _essentials(f: Fundamentals) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    if f.market_cap:
        rows.append(("Market cap", _big(f.market_cap)))
    if f.pe_ratio:
        rows.append(("P/E", f"{f.pe_ratio:,.1f}"))
    if f.latest_roe is not None:
        rows.append(("ROE", f"{f.latest_roe:,.0f}%"))
    if f.debt_to_equity is not None:
        rows.append(("Debt / equity", f"{f.debt_to_equity:,.2f}"))
    if f.beta:
        rows.append(("Beta", f"{f.beta:,.2f}"))
    if f.institutional_holders:
        rows.append(("Institutional holders", f"{f.institutional_holders:,}"))
    if f.next_earnings:
        rows.append(("Next earnings", f.next_earnings))
    return rows


def _pct_text(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.0f}%"


def _big(value: float) -> str:
    for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(value) >= size:
            return f"${value / size:,.2f}{unit}"
    return f"${value:,.0f}"


def today_key(now: datetime | date | None = None) -> str:
    """Cache key: a CAN SLIM grade is a slow-moving, once-a-day fact."""
    moment = now or datetime.now()
    return (moment.date() if isinstance(moment, datetime) else moment).isoformat()
