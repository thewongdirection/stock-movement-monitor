"""Locating the can-slim-grader skill and using its scripts.

The skill is a separate repository. Rather than vendoring a copy that silently
rots, it is located at runtime and its own scripts are executed, so a `git pull`
in the skill directory is enough to pick up methodology changes.

Search order: an explicit config path, then ``$CANSLIM_SKILL_PATH``, then a few
conventional locations. When it isn't found, grading is skipped with a clear
reason and alerts still go out — a missing report must never cost you the alert
it was attached to.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SKILL_REPO = "https://github.com/thewongdirection/can-slim-grader"

CANDIDATE_PATHS = (
    "vendor/can-slim-grader",
    "../can-slim-grader",
    "/workspace/can-slim-grader",
    "~/.claude/skills/can-slim-grader",
)


class SkillNotAvailable(RuntimeError):
    """The skill isn't on disk. Grading is skipped; alerts are unaffected."""


@dataclass(frozen=True)
class SkillPaths:
    root: Path
    template: Path
    relative_strength: Path
    html_to_pdf: Path

    @property
    def methodology(self) -> Path:
        return self.root / "references" / "canslim-methodology.md"


def find_skill(configured: str | None = None) -> SkillPaths:
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    env = os.environ.get("CANSLIM_SKILL_PATH")
    if env:
        candidates.append(env)
    candidates.extend(CANDIDATE_PATHS)

    tried: list[str] = []
    for candidate in candidates:
        root = Path(candidate).expanduser()
        tried.append(str(root))
        template = root / "assets" / "evaluation_template.html"
        rs = root / "scripts" / "relative_strength.py"
        if template.exists() and rs.exists():
            return SkillPaths(
                root=root,
                template=template,
                relative_strength=rs,
                html_to_pdf=root / "scripts" / "html_to_pdf.py",
            )

    raise SkillNotAvailable(
        "can-slim-grader not found. Clone it next to this repo or set "
        f"CANSLIM_SKILL_PATH:\n  git clone --depth 1 {SKILL_REPO} vendor/can-slim-grader\n"
        f"Looked in: {', '.join(tried)}"
    )


def load_relative_strength(paths: SkillPaths) -> Any:
    """Import the skill's own RS module rather than reimplementing its maths."""
    spec = importlib.util.spec_from_file_location(
        "canslim_relative_strength", paths.relative_strength
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise SkillNotAvailable(f"cannot import {paths.relative_strength}")
    module = importlib.util.module_from_spec(spec)
    # Registered so repeat grades in one process reuse the import.
    sys.modules.setdefault("canslim_relative_strength", module)
    spec.loader.exec_module(module)
    return module


def export_pdf(paths: SkillPaths, html_path: Path, pdf_path: Path) -> tuple[bool, str]:
    """Run the skill's PDF exporter. Returns (ok, detail).

    The skill tries headless Chrome, then Playwright, WeasyPrint and
    wkhtmltopdf. None of those is guaranteed present, and the skill itself calls
    the PDF a secondary convenience — so a failure here is reported, not raised.
    """
    if not paths.html_to_pdf.exists():
        return False, f"{paths.html_to_pdf} is missing from the skill checkout"
    try:
        result = subprocess.run(
            [sys.executable, str(paths.html_to_pdf), str(html_path), str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(paths.root),
        )
    except subprocess.TimeoutExpired:
        return False, "PDF export timed out after 180s"
    except OSError as exc:
        return False, f"could not run the PDF exporter: {exc}"

    if result.returncode != 0 or not pdf_path.exists():
        detail = (result.stderr or result.stdout or "no output").strip().splitlines()
        tail = detail[-1] if detail else "no output"
        if not _any_pdf_engine():
            tail += (
                " — no PDF engine found on this machine. Install Chromium, or "
                "`pip install weasyprint`. The HTML report is still produced."
            )
        return False, tail
    return True, (result.stdout or "").strip() or "exported"


def _any_pdf_engine() -> bool:
    if any(
        shutil.which(name)
        for name in ("chromium", "chromium-browser", "google-chrome", "chrome", "wkhtmltopdf")
    ):
        return True
    for module in ("playwright", "weasyprint"):
        if importlib.util.find_spec(module) is not None:
            return True
    return False
