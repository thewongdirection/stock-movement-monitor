"""CAN SLIM grading, wired to the `can-slim-grader` skill.

**What this is, precisely** — because the distinction matters for how much you
trust the output:

`can-slim-grader` is an *agent* skill. Its intended operator is an LLM that
reads the methodology, gathers data from whatever sources are connected, and
exercises judgement on each letter. A five-minute cron on a GitHub runner has
no LLM in the loop, so this package implements the skill's **published
pass/partial/fail rubric deterministically** — the thresholds out of
``references/data-and-scoring-guide.md``, applied in code.

What that buys: free, fast, reproducible, and it can run on every alert.
What it costs: the letters that genuinely need judgement — chiefly **N**'s "new
product/management/condition" story and the quality half of **I** — cannot be
assessed from numbers alone. Those are scored on their measurable proxies and
the report says so, per letter, rather than implying a judgement it didn't make.

Three pieces of the skill are used *verbatim*, not reimplemented:
``scripts/relative_strength.py`` for the technicals, ``assets/evaluation_template.html``
for the report, and ``scripts/html_to_pdf.py`` for the PDF export. So the
numbers and the document are the skill's own; only the letter-scoring loop is
local.

Set ``canslim.narrator: llm`` and an ``ANTHROPIC_API_KEY`` to hand the prose and
the judgement letters to a model instead — see `narrate.py`.
"""

from .grader import Grade, LetterScore, grade_ticker  # noqa: F401
from .report import build_report  # noqa: F401
from .skill import SkillNotAvailable, SkillPaths, find_skill  # noqa: F401
