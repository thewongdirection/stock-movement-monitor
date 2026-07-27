"""Bounded parameter machinery.

Every tunable threshold is declared as a `Param` with a default, an allowed
range (or set of choices), and a one-line rationale. Overrides from
``config.yaml`` are checked against those bounds:

* ``monitor validate`` treats any problem as a hard error — run it in CI.
* ``monitor run`` clamps out-of-range values, keeps going, and surfaces the
  clamp as a warning in the run's Telegram footer, so a fat-fingered config
  degrades instead of silently killing your alerts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence


class ConfigError(ValueError):
    """Raised for a config problem that cannot be recovered from."""


@dataclass(frozen=True)
class Param:
    default: Any
    lo: float | None = None
    hi: float | None = None
    choices: Sequence[Any] | None = None
    kind: str = "number"  # number | int | bool | str | list[str]
    doc: str = ""

    def bounds_text(self) -> str:
        if self.choices is not None:
            return "one of " + ", ".join(repr(c) for c in self.choices)
        if self.lo is not None and self.hi is not None:
            return f"{_fmt(self.lo)} to {_fmt(self.hi)}"
        if self.lo is not None:
            return f">= {_fmt(self.lo)}"
        if self.hi is not None:
            return f"<= {_fmt(self.hi)}"
        return "unbounded"


def _fmt(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return f"{int(v):,}"
    return f"{v:,g}"


@dataclass
class Issue:
    path: str
    message: str


def resolve(
    specs: dict[str, Param],
    supplied: dict[str, Any] | None,
    path: str,
    issues: list[Issue],
) -> dict[str, Any]:
    """Merge `supplied` over `specs` defaults, clamping and recording issues."""
    supplied = dict(supplied or {})
    out: dict[str, Any] = {}

    for key in sorted(set(supplied) - set(specs)):
        issues.append(
            Issue(
                f"{path}.{key}",
                f"unknown setting {key!r}; valid settings here are: "
                + ", ".join(sorted(specs)),
            )
        )
        supplied.pop(key)

    for name, spec in specs.items():
        if name not in supplied or supplied[name] is None:
            out[name] = spec.default
            continue
        out[name] = _coerce(spec, supplied[name], f"{path}.{name}", issues)
    return out


def _coerce(spec: Param, value: Any, where: str, issues: list[Issue]) -> Any:
    if spec.kind == "bool":
        if isinstance(value, bool):
            return value
        issues.append(Issue(where, f"expected true/false, got {value!r}"))
        return spec.default

    if spec.kind == "list[str]":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, Iterable):
            issues.append(Issue(where, f"expected a list, got {value!r}"))
            return spec.default
        items = [str(v) for v in value]
        if spec.choices is not None:
            bad = [v for v in items if v not in spec.choices]
            if bad:
                issues.append(
                    Issue(
                        where,
                        f"unsupported value(s) {bad}; allowed: {spec.bounds_text()}",
                    )
                )
                items = [v for v in items if v in spec.choices]
        return items

    if spec.kind == "str":
        text = str(value)
        if spec.choices is not None and text not in spec.choices:
            issues.append(
                Issue(where, f"{text!r} is not allowed; must be {spec.bounds_text()}")
            )
            return spec.default
        return text

    # numeric
    try:
        num: float | int = int(value) if spec.kind == "int" else float(value)
    except (TypeError, ValueError):
        issues.append(Issue(where, f"expected a number, got {value!r}"))
        return spec.default

    if spec.lo is not None and num < spec.lo:
        issues.append(
            Issue(
                where,
                f"{_fmt(num)} is below the supported range ({spec.bounds_text()}); "
                f"clamped to {_fmt(spec.lo)}",
            )
        )
        num = int(spec.lo) if spec.kind == "int" else spec.lo
    elif spec.hi is not None and num > spec.hi:
        issues.append(
            Issue(
                where,
                f"{_fmt(num)} is above the supported range ({spec.bounds_text()}); "
                f"clamped to {_fmt(spec.hi)}",
            )
        )
        num = int(spec.hi) if spec.kind == "int" else spec.hi
    return num


def reference_table(specs: dict[str, Param]) -> list[tuple[str, str, str, str]]:
    """(name, default, allowed range, rationale) rows for docs and `--explain`."""
    rows = []
    for name, spec in specs.items():
        default = spec.default
        shown = ", ".join(map(str, default)) if isinstance(default, list) else str(default)
        rows.append((name, shown, spec.bounds_text(), spec.doc))
    return rows
