"""Deterministic CAN SLIM scoring, against the rubric the grader skill publishes.

The thresholds here are not invented. They come from
`can-slim-grader/references/data-and-scoring-guide.md` — 25% quarterly EPS and
sales growth, three years of 25%+ annual growth, ROE 17%, relative strength
beating the index, and so on — so a scorecard produced unattended on a server
lines up with one produced by asking Claude to run the skill interactively.

The important design choice is the fourth grade. A letter is PASS, PARTIAL,
FAIL **or UNKNOWN**, and UNKNOWN is not a zero. Institutional sponsorship needs
13F data; the "new product or management" half of N needs somebody to read the
news. Scoring those as failures would quietly mark every stock down for the
monitor's blind spots, and a 4/7 that is really "3 of 7 measured" is a lie with
a decimal point on it. Unknown letters are excluded from the denominator and the
coverage is stated on the card.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import Enum


class Grade(str, Enum):
    PASS = "pass"
    PARTIAL = "partial"
    FAIL = "fail"
    UNKNOWN = "unknown"

    @property
    def points(self) -> float:
        return {"pass": 1.0, "partial": 0.5, "fail": 0.0, "unknown": 0.0}[self.value]

    @property
    def icon(self) -> str:
        return {"pass": "✅", "partial": "🟡", "fail": "❌", "unknown": "❔"}[self.value]


#: C, A and L carry more weight because they were the most predictive traits.
WEIGHTS = {"C": 1.5, "A": 1.5, "L": 1.5, "N": 1.0, "S": 1.0, "I": 1.0, "M": 1.0}

NAMES = {
    "C": "Current quarterly earnings",
    "A": "Annual earnings growth",
    "N": "New high from a sound base",
    "S": "Supply and demand",
    "L": "Leader, not laggard",
    "I": "Institutional sponsorship",
    "M": "Market direction",
}


@dataclass
class Letter:
    key: str
    grade: Grade
    evidence: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return NAMES[self.key]


@dataclass
class Facts:
    """Everything the grader can use. Every field is optional on purpose.

    A partially available fact set produces a partially graded card that says so,
    which is far more useful than refusing to grade or than guessing.
    """

    ticker: str
    price: float | None = None
    #: Newest first, each {"eps": float, "revenue": float, "date": "YYYY-MM-DD"}
    quarters: list[dict] = field(default_factory=list)
    years: list[dict] = field(default_factory=list)
    roe: float | None = None                     # fraction, e.g. 0.21
    debt_to_equity: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    #: Daily closes, oldest first, for the stock and the index.
    closes: list[float] = field(default_factory=list)
    volumes: list[int] = field(default_factory=list)
    index_closes: list[float] = field(default_factory=list)
    shares_outstanding: float | None = None
    institutional_holders: int | None = None
    institutional_holders_prior: int | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Scorecard:
    ticker: str
    letters: list[Letter]
    verdict: str
    summary: str
    score: float                 # weighted, over graded letters only
    max_score: float
    graded: int
    notes: list[str] = field(default_factory=list)

    @property
    def percent(self) -> float:
        return self.score / self.max_score * 100 if self.max_score else 0.0

    @property
    def letter_grade(self) -> str:
        pct = self.percent
        for cut, mark in ((90, "A"), (80, "A-"), (70, "B+"), (60, "B"),
                          (50, "B-"), (40, "C"), (25, "D")):
            if pct >= cut:
                return mark
        return "F"

    def get(self, key: str) -> Letter | None:
        return next((letter for letter in self.letters if letter.key == key), None)

    def one_liner(self) -> str:
        """The single line an alert attaches."""
        coverage = "" if self.graded == 7 else f", {self.graded}/7 letters measurable"
        return (f"CAN SLIM {self.letter_grade} ({self.percent:.0f}%{coverage}) — "
                f"{self.verdict}")


# --------------------------------------------------------------------------- #
# per-letter scoring
# --------------------------------------------------------------------------- #

def _pct_change(now: float, then: float) -> float | None:
    """Year-over-year growth. Undefined when the base is zero or negative.

    A company going from a $2 loss to a $1 profit is not "150% growth", and
    treating it as such is how loss-makers score like leaders.
    """
    if then is None or now is None or then <= 0:
        return None
    return (now - then) / then * 100


def score_c(facts: Facts) -> Letter:
    """Latest quarter versus the *same quarter a year earlier*.

    Four rows back, never the previous quarter. Sequential comparison makes
    every seasonal business look like it is collapsing in Q1 and exploding in Q4.
    """
    quarters = facts.quarters
    if len(quarters) < 5:
        return Letter("C", Grade.UNKNOWN, ["fewer than 5 quarters of data available"])

    eps_growth = _pct_change(quarters[0].get("eps"), quarters[4].get("eps"))
    rev_growth = _pct_change(quarters[0].get("revenue"), quarters[4].get("revenue"))
    if eps_growth is None and rev_growth is None:
        return Letter("C", Grade.UNKNOWN, ["no usable earnings base a year ago"])

    evidence = []
    if eps_growth is not None:
        evidence.append(f"EPS {eps_growth:+.0f}% YoY ({quarters[0].get('date', '?')})")
    if rev_growth is not None:
        evidence.append(f"Sales {rev_growth:+.0f}% YoY")

    accelerating = None
    if len(quarters) >= 6:
        prior = _pct_change(quarters[1].get("eps"), quarters[5].get("eps"))
        if prior is not None and eps_growth is not None:
            accelerating = eps_growth > prior
            evidence.append(
                f"Growth {'accelerating' if accelerating else 'decelerating'} "
                f"(prior quarter {prior:+.0f}%)"
            )

    strong = (eps_growth or 0) >= 25 and (rev_growth is None or rev_growth >= 25)
    if strong and accelerating is not False:
        return Letter("C", Grade.PASS, evidence)
    if (eps_growth or 0) >= 10 or ((eps_growth or 0) >= 25 and accelerating is False):
        return Letter("C", Grade.PARTIAL, evidence)
    return Letter("C", Grade.FAIL, evidence)


def score_a(facts: Facts) -> Letter:
    years = facts.years
    if len(years) < 4:
        return Letter("A", Grade.UNKNOWN, ["fewer than 4 annual periods available"])

    growth = [
        _pct_change(years[i].get("eps"), years[i + 1].get("eps"))
        for i in range(min(3, len(years) - 1))
    ]
    known = [g for g in growth if g is not None]
    if not known:
        return Letter("A", Grade.UNKNOWN, ["annual EPS base is zero or negative"])

    evidence = [f"Annual EPS growth: {', '.join(f'{g:+.0f}%' for g in known)}"]
    if facts.roe is not None:
        evidence.append(f"ROE {facts.roe * 100:.0f}%")

    all_strong = len(known) >= 3 and all(g >= 25 for g in known)
    roe_strong = facts.roe is not None and facts.roe >= 0.17
    roe_ok = facts.roe is not None and facts.roe >= 0.12

    if all_strong and roe_strong:
        return Letter("A", Grade.PASS, evidence)
    if all(g >= 10 for g in known) or roe_ok:
        return Letter("A", Grade.PARTIAL, evidence)
    return Letter("A", Grade.FAIL, evidence)


def score_n(facts: Facts) -> Letter:
    """The technical half only.

    CAN SLIM's N is "a new product, management or industry condition **and** a
    breakout to new highs from a sound base". Nothing here can read a press
    release, so this scores the price half and says so — the narrator, or the
    grader skill run interactively, supplies the other half.
    """
    if not facts.price or not facts.high_52w:
        return Letter("N", Grade.UNKNOWN, ["no 52-week range available"])

    off_high = (facts.high_52w - facts.price) / facts.high_52w * 100
    evidence = [f"{off_high:.1f}% below the 52-week high of ${facts.high_52w:,.2f}"]

    depth = _base_depth(facts.closes)
    if depth is not None:
        evidence.append(f"Recent base depth {depth:.0f}% "
                        f"({'sound' if depth <= 33 else 'wide and loose'})")
    evidence.append("The 'new product/management' half is not machine-readable — "
                    "run the can-slim-grader skill for it")

    if off_high <= 5 and (depth is None or depth <= 33):
        return Letter("N", Grade.PASS, evidence)
    if off_high <= 15:
        return Letter("N", Grade.PARTIAL, evidence)
    return Letter("N", Grade.FAIL, evidence)


def score_s(facts: Facts) -> Letter:
    """Is volume arriving on up days or down days?

    Sessions are split by index, pairing each close with the volume printed the
    same day. An earlier version filtered by value membership, which silently
    collapsed any two sessions that happened to trade the same number of shares
    and could report a stock with no down days at all.
    """
    if len(facts.volumes) < 40 or len(facts.closes) < 40:
        return Letter("S", Grade.UNKNOWN, ["not enough daily history to judge volume"])

    sessions = list(zip(facts.closes[-11:-1], facts.closes[-10:], facts.volumes[-10:]))
    up = [volume for before, close, volume in sessions if close > before]
    down = [volume for before, close, volume in sessions if close <= before]

    base = facts.volumes[-60:-10] or facts.volumes[:-10]
    median_base = statistics.median(base) if base else 0
    if not median_base:
        return Letter("S", Grade.UNKNOWN, ["no baseline volume to compare against"])

    evidence = []
    up_ratio = statistics.fmean(up) / median_base if up else None
    down_ratio = statistics.fmean(down) / median_base if down else None
    if up_ratio is not None:
        evidence.append(f"Up-day volume {up_ratio:.2f}x the 3-month median ({len(up)} sessions)")
    if down_ratio is not None:
        evidence.append(f"Down-day volume {down_ratio:.2f}x ({len(down)} sessions)")
    if facts.debt_to_equity is not None:
        evidence.append(f"Debt/equity {facts.debt_to_equity:.2f}")
    if facts.shares_outstanding:
        evidence.append(f"{facts.shares_outstanding / 1e6:,.0f}M shares outstanding")

    leveraged = facts.debt_to_equity is not None and facts.debt_to_equity >= 1.5

    if not down:
        evidence.append("No down day in the last 10 sessions")
        return Letter("S", Grade.PARTIAL if leveraged else Grade.PASS, evidence)
    if not up:
        evidence.append("Every one of the last 10 sessions closed lower — this is distribution")
        return Letter("S", Grade.FAIL, evidence)

    if up_ratio > down_ratio * 1.2 and not leveraged:
        return Letter("S", Grade.PASS, evidence)
    if up_ratio >= down_ratio * 0.9:
        return Letter("S", Grade.PARTIAL, evidence)
    return Letter("S", Grade.FAIL, evidence)


def score_l(facts: Facts) -> Letter:
    """Relative strength against the index over the same window."""
    rs = relative_strength(facts.closes, facts.index_closes)
    if rs is None:
        return Letter("L", Grade.UNKNOWN, ["no index series to compare against"])

    stock, index, ratio = rs
    evidence = [
        f"Stock {stock:+.1f}% vs index {index:+.1f}% over the measured window",
        f"Relative strength proxy {ratio:+.1f} points",
    ]
    if facts.low_52w and facts.price:
        above_low = (facts.price - facts.low_52w) / facts.low_52w * 100
        evidence.append(f"{above_low:.0f}% above the 52-week low")

    if ratio >= 10:
        return Letter("L", Grade.PASS, evidence)
    if ratio >= -2:
        return Letter("L", Grade.PARTIAL, evidence)
    return Letter("L", Grade.FAIL, evidence)


def score_i(facts: Facts) -> Letter:
    if facts.institutional_holders is None:
        return Letter("I", Grade.UNKNOWN,
                      ["13F holder counts not available to the monitor"])
    evidence = [f"{facts.institutional_holders:,} institutional holders"]
    if facts.institutional_holders_prior:
        change = facts.institutional_holders - facts.institutional_holders_prior
        evidence.append(f"{change:+,} versus the prior quarter")
        if change > 0:
            return Letter("I", Grade.PASS, evidence)
        if change == 0:
            return Letter("I", Grade.PARTIAL, evidence)
        return Letter("I", Grade.FAIL, evidence)
    return Letter("I", Grade.PARTIAL, evidence)


def score_m(facts: Facts) -> Letter:
    """Market direction, from the index series alone.

    Distribution-day counting needs index volume, which is not always fetched;
    the 50/200-day structure is the part that is always computable.
    """
    closes = facts.index_closes
    if len(closes) < 200:
        return Letter("M", Grade.UNKNOWN, ["fewer than 200 index sessions available"])

    last = closes[-1]
    ma50 = statistics.fmean(closes[-50:])
    ma200 = statistics.fmean(closes[-200:])
    evidence = [
        f"Index {last:,.2f}, 50-day {ma50:,.2f}, 200-day {ma200:,.2f}",
    ]
    if last > ma50 > ma200:
        evidence.append("Confirmed uptrend — price above a rising 50 above the 200")
        return Letter("M", Grade.PASS, evidence)
    if last > ma200:
        evidence.append("Under pressure — above the 200-day but not leading it")
        return Letter("M", Grade.PARTIAL, evidence)
    evidence.append("Correction — index below its 200-day average")
    return Letter("M", Grade.FAIL, evidence)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def relative_strength(closes: list[float], index_closes: list[float]
                      ) -> tuple[float, float, float] | None:
    """Percentage gain of the stock and index over the same number of sessions.

    Both series are truncated to the shorter one *from the right*, so the window
    ends today for both. Aligning from the left would compare the stock's last
    six months against the index's last twelve.
    """
    span = min(len(closes), len(index_closes))
    if span < 20:
        return None
    stock = closes[-span:]
    index = index_closes[-span:]
    if stock[0] <= 0 or index[0] <= 0:
        return None
    stock_pct = (stock[-1] - stock[0]) / stock[0] * 100
    index_pct = (index[-1] - index[0]) / index[0] * 100
    return stock_pct, index_pct, stock_pct - index_pct


def _base_depth(closes: list[float], window: int = 60) -> float | None:
    """Peak-to-trough drawdown of the recent consolidation, as a percent."""
    if len(closes) < window:
        return None
    recent = closes[-window:]
    peak = max(recent)
    trough = min(recent[recent.index(peak):]) if recent.index(peak) < len(recent) - 1 else min(recent)
    return (peak - trough) / peak * 100 if peak else None


# --------------------------------------------------------------------------- #
# the card
# --------------------------------------------------------------------------- #

def grade(facts: Facts) -> Scorecard:
    letters = [
        score_c(facts), score_a(facts), score_n(facts), score_s(facts),
        score_l(facts), score_i(facts), score_m(facts),
    ]
    known = [letter for letter in letters if letter.grade is not Grade.UNKNOWN]
    score = sum(WEIGHTS[letter.key] * letter.grade.points for letter in known)
    max_score = sum(WEIGHTS[letter.key] for letter in known)

    verdict, summary = _verdict(letters, facts)
    return Scorecard(
        ticker=facts.ticker,
        letters=letters,
        verdict=verdict,
        summary=summary,
        score=score,
        max_score=max_score,
        graded=len(known),
        notes=list(facts.notes),
    )


def _verdict(letters: list[Letter], facts: Facts) -> tuple[str, str]:
    by_key = {letter.key: letter for letter in letters}

    def is_(key: str, *grades: Grade) -> bool:
        return by_key[key].grade in grades

    if is_("C", Grade.FAIL) or is_("A", Grade.FAIL) or is_("L", Grade.FAIL):
        failing = [k for k in ("C", "A", "L") if is_(k, Grade.FAIL)]
        return ("AVOID", (
            f"Fails {' and '.join(failing)} — the letters that mattered most. "
            "Strong price action without earnings behind it is not a CAN SLIM setup, "
            "and a cheap laggard is exactly what the method avoids."
        ))

    core_ok = is_("C", Grade.PASS) and is_("A", Grade.PASS) and is_("L", Grade.PASS)
    if core_ok and is_("N", Grade.PASS) and not is_("M", Grade.FAIL):
        return ("BUY-RANGE", (
            "Passes the core earnings letters and leadership with a valid breakout. "
            "The framework's own rules: do not chase more than 5% past the pivot, and "
            "cut the loss at 7-8% below entry — 3% while the market is in correction."
        ))
    if is_("M", Grade.FAIL):
        return ("WATCH", (
            "The stock's own letters may hold up, but the general market is in a "
            "correction, and three in four stocks follow the market down. Wait for a "
            "follow-through day before acting."
        ))
    if is_("C", Grade.UNKNOWN) or is_("A", Grade.UNKNOWN):
        return ("INCOMPLETE", (
            "The earnings letters could not be measured from the available data, and "
            "they are the ones that matter most. Run the can-slim-grader skill for a "
            "full read before drawing a conclusion."
        ))
    return ("WATCH", (
        "Fundamentals are respectable but there is no valid buy point right now — "
        "either extended from a base or still repairing one. What needs to happen is "
        "a new base and a breakout on volume."
    ))
