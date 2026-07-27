"""Grading as a service: gather, grade, render, cache.

Caching is the load-bearing part. A CAN SLIM verdict turns on quarterly
earnings and a multi-month base — it does not change between two cron ticks
five minutes apart. So a grade is computed **once per ticker per day** and every
alert that day attaches the same report. Without that, ten alerts on one busy
name would mean ten identical multi-call grading runs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..models import Bar
from ..providers.base import ProviderError
from ..providers.fmp import FMPProvider
from .fundamentals import FMPFundamentals, Fundamentals
from .grader import Grade, grade_ticker, today_key
from .narrate import NarratorConfig, NarratorResult, apply as narrate_apply
from .report import Report, build_report
from .skill import SkillNotAvailable, SkillPaths, find_skill

log = logging.getLogger(__name__)

BENCHMARK = "SPY"


@dataclass
class GradeOutcome:
    """Either a report, or the reason there isn't one. Never an exception."""

    ticker: str
    report: Report | None = None
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return self.report is not None


class CanSlimService:
    def __init__(
        self,
        *,
        fmp_api_key: str,
        fmp_base_url: str,
        cache_dir: str | Path,
        skill_path: str | None = None,
        want_pdf: bool = True,
        timeout: int = 30,
        narrator: NarratorConfig | None = None,
    ):
        self.cache_dir = Path(cache_dir)
        self.want_pdf = want_pdf
        self.narrator = narrator or NarratorConfig()
        self._skill_path = skill_path
        self._skill: SkillPaths | None = None
        self._skill_error: str | None = None
        self._bars = FMPProvider(
            api_key=fmp_api_key or "unset", base_url=fmp_base_url, timeout=timeout
        ) if fmp_api_key else None
        self._fundamentals = FMPFundamentals(
            api_key=fmp_api_key, base_url=fmp_base_url, timeout=timeout
        )
        self._benchmark_cache: tuple[str, list[Bar]] | None = None

    # -- skill resolution -------------------------------------------------
    def skill(self) -> SkillPaths | None:
        if self._skill is not None:
            return self._skill
        if self._skill_error is not None:
            return None
        try:
            self._skill = find_skill(self._skill_path)
        except SkillNotAvailable as exc:
            self._skill_error = str(exc)
            log.warning("CAN SLIM grading disabled: %s", exc)
            return None
        return self._skill

    @property
    def unavailable_reason(self) -> str | None:
        return self._skill_error

    # -- grading ----------------------------------------------------------
    def grade(self, ticker: str, now: datetime | None = None) -> GradeOutcome:
        ticker = ticker.upper()
        now = now or datetime.now()
        skill = self.skill()
        if skill is None:
            return GradeOutcome(ticker, skipped=self._skill_error or "skill unavailable")

        cached = self._from_cache(ticker, now)
        if cached is not None:
            return GradeOutcome(ticker, report=cached)

        if self._bars is None:
            return GradeOutcome(
                ticker,
                skipped="no FMP_API_KEY — CAN SLIM needs price history and fundamentals",
            )

        try:
            daily = self._bars.daily_bars(ticker, years=2.0)
        except ProviderError as exc:
            return GradeOutcome(ticker, skipped=f"price history unavailable ({exc})")
        if not daily:
            return GradeOutcome(ticker, skipped="no daily price history returned")

        benchmark = self._benchmark(now)
        if not benchmark:
            return GradeOutcome(
                ticker, skipped="benchmark (SPY) history unavailable — RS and M need it"
            )

        fundamentals = self._fundamentals.fetch(ticker)
        grade = grade_ticker(
            ticker,
            skill=skill,
            daily=daily,
            weekly=None,  # derived from the dailies
            benchmark_daily=benchmark,
            fundamentals=fundamentals,
            now=now,
        )

        # The narrator writes the per-letter prose and judges N and I. A failure
        # here leaves the deterministic scorecard intact — it is an upgrade to
        # the grade, never a prerequisite for having one.
        if self.narrator.enabled:
            grade, narration = narrate_apply(
                grade, self.narrator, skill, today=today_key(now)
            )
            if not narration.ok:
                grade.warnings.append(f"narration skipped: {narration.skipped}")
                log.info("narration skipped for %s: %s", ticker, narration.skipped)
            else:
                log.info(
                    "narrated %s (%d applied, %d rejected score change(s))",
                    ticker,
                    len(narration.applied_scores),
                    len(narration.rejected_scores),
                )

        report = build_report(
            grade, skill, self._report_dir(ticker, now), want_pdf=self.want_pdf
        )
        self._to_cache(ticker, now, report)
        return GradeOutcome(ticker, report=report)

    def _benchmark(self, now: datetime) -> list[Bar]:
        """SPY bars, fetched once per process — every grade needs the same ones."""
        key = today_key(now)
        if self._benchmark_cache and self._benchmark_cache[0] == key:
            return self._benchmark_cache[1]
        if self._bars is None:
            return []
        try:
            bars = self._bars.daily_bars(BENCHMARK, years=2.0)
        except ProviderError as exc:
            log.warning("benchmark history unavailable: %s", exc)
            bars = []
        self._benchmark_cache = (key, bars)
        return bars

    # -- cache ------------------------------------------------------------
    def _report_dir(self, ticker: str, now: datetime) -> Path:
        return self.cache_dir / today_key(now)

    def _cache_file(self, ticker: str, now: datetime) -> Path:
        return self._report_dir(ticker, now) / f"{ticker}.json"

    def _from_cache(self, ticker: str, now: datetime) -> Report | None:
        path = self._cache_file(ticker, now)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        html = Path(payload.get("html", ""))
        if not html.exists():
            return None
        pdf = Path(payload["pdf"]) if payload.get("pdf") else None
        if pdf is not None and not pdf.exists():
            pdf = None
        summary = payload.get("summary") or {}
        grade = _thin_grade(ticker, summary)
        return Report(
            grade=grade,
            html_path=html,
            pdf_path=pdf,
            pdf_error=payload.get("pdf_error"),
        )

    def _to_cache(self, ticker: str, now: datetime, report: Report) -> None:
        path = self._cache_file(ticker, now)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "html": str(report.html_path),
                    "pdf": str(report.pdf_path) if report.pdf_path else None,
                    "pdf_error": report.pdf_error,
                    "summary": {
                        "verdict": report.grade.verdict,
                        "tone": report.grade.tone,
                        "score_text": report.grade.score_text,
                        "summary": report.grade.summary,
                        "one_line": report.grade.one_line(),
                        "company": report.grade.company,
                        "as_of": report.grade.as_of,
                        "price": report.grade.price,
                        "letters": [
                            {"key": letter.key, "score": letter.score, "actual": letter.actual}
                            for letter in report.grade.letters
                        ],
                        "warnings": report.grade.warnings,
                        "narrated": report.grade.narrated,
                        "narrator_notes": report.grade.narrator_notes,
                        "narrator_sources": report.grade.narrator_sources,
                    },
                },
                indent=2,
            )
        )

    def close(self) -> None:
        if self._bars is not None:
            self._bars.close()
        self._fundamentals.close()


def _thin_grade(ticker: str, summary: dict) -> Grade:
    """Rebuild just enough Grade from cache to render a message."""
    from .grader import LetterScore

    letters = [
        LetterScore(
            key=item.get("key", "?"),
            score=item.get("score", "unknown"),
            threshold="",
            actual=item.get("actual", ""),
            read="",
        )
        for item in summary.get("letters", [])
    ]
    return Grade(
        ticker=ticker,
        company=summary.get("company", ""),
        as_of=summary.get("as_of", ""),
        price=summary.get("price"),
        letters=letters,
        verdict=summary.get("verdict", "WATCH"),
        tone=summary.get("tone", "pressure"),
        summary=summary.get("summary", ""),
        warnings=summary.get("warnings", []),
        narrated=bool(summary.get("narrated")),
        narrator_notes=summary.get("narrator_notes", []),
        narrator_sources=summary.get("narrator_sources", []),
    )
