"""Runtime overlay — the mutable half of the configuration.

``config.yaml`` is the git-tracked baseline. Anything changed from the bot
(adding a ticker, moving a threshold, switching a detector off) lands in
``state/runtime.json`` and is merged over the baseline at load time.

Two reasons for the split rather than rewriting the YAML:

* The YAML stays readable and reviewable, and a bad live change is one
  ``reset`` away from the committed defaults.
* On GitHub Actions the overlay rides in the same cache as the state database,
  so a bot edit survives to the next cron tick without a commit.

Every write goes through the same bounded-parameter validation the YAML does,
so the bot cannot set a threshold the engine would reject.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CANSLIM_SPECS, DETECTOR_SPECS, RUN_SPECS, TICKER_RE, _apply_presets
from .params import Issue, Param, resolve


class OverlayError(ValueError):
    """A rejected bot edit, with a message meant to be shown to the user."""


@dataclass
class Change:
    at: str
    what: str


@dataclass
class Overlay:
    """Bot-applied changes on top of config.yaml."""

    path: Path
    added_tickers: list[str] = field(default_factory=list)
    removed_tickers: list[str] = field(default_factory=list)
    detectors: dict[str, dict[str, Any]] = field(default_factory=dict)
    run: dict[str, Any] = field(default_factory=dict)
    canslim: dict[str, Any] = field(default_factory=dict)
    per_ticker: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    log: list[Change] = field(default_factory=list)

    # -- persistence ------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "Overlay":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        try:
            raw = json.loads(p.read_text() or "{}")
        except json.JSONDecodeError:
            # A corrupt overlay must not take the monitor down; the committed
            # baseline is always a valid fallback.
            return cls(path=p, log=[Change(_now(), f"discarded corrupt overlay at {p}")])
        return cls(
            path=p,
            added_tickers=[str(t).upper() for t in raw.get("added_tickers", [])],
            removed_tickers=[str(t).upper() for t in raw.get("removed_tickers", [])],
            detectors=raw.get("detectors", {}) or {},
            run=raw.get("run", {}) or {},
            canslim=raw.get("canslim", {}) or {},
            per_ticker=raw.get("per_ticker", {}) or {},
            log=[Change(c.get("at", ""), c.get("what", "")) for c in raw.get("log", [])],
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "added_tickers": self.added_tickers,
            "removed_tickers": self.removed_tickers,
            "detectors": self.detectors,
            "run": self.run,
            "canslim": self.canslim,
            "per_ticker": self.per_ticker,
            # Keep the tail only; this is an audit trail, not a database.
            "log": [{"at": c.at, "what": c.what} for c in self.log[-100:]],
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def _record(self, what: str) -> None:
        self.log.append(Change(_now(), what))

    # -- watchlist --------------------------------------------------------
    def add_ticker(self, ticker: str) -> str:
        sym = str(ticker).strip().upper()
        if not TICKER_RE.match(sym):
            raise OverlayError(
                f"{ticker!r} does not look like a ticker symbol (letters, digits, "
                "dot or dash; up to 10 characters)."
            )
        if sym in self.removed_tickers:
            self.removed_tickers.remove(sym)
            self._record(f"re-added {sym}")
            return f"{sym} is back on the watchlist."
        if sym in self.added_tickers:
            return f"{sym} is already on the watchlist."
        self.added_tickers.append(sym)
        self._record(f"added {sym}")
        return f"Added {sym}."

    def remove_ticker(self, ticker: str, baseline: list[str]) -> str:
        sym = str(ticker).strip().upper()
        if sym in self.added_tickers:
            self.added_tickers.remove(sym)
            self._record(f"removed {sym}")
            return f"Removed {sym}."
        if sym in baseline:
            if sym in self.removed_tickers:
                return f"{sym} is already off the watchlist."
            self.removed_tickers.append(sym)
            self._record(f"removed {sym}")
            return f"Removed {sym}."
        raise OverlayError(f"{sym} is not on the watchlist.")

    def effective_tickers(self, baseline: list[str]) -> list[str]:
        out = [t for t in baseline if t not in self.removed_tickers]
        out.extend(t for t in self.added_tickers if t not in out)
        return out

    # -- thresholds -------------------------------------------------------
    def set_detector(
        self, detector: str, setting: str, value: Any, ticker: str | None = None
    ) -> str:
        if detector not in DETECTOR_SPECS:
            raise OverlayError(
                f"unknown detector {detector!r}. Available: "
                + ", ".join(DETECTOR_SPECS)
            )
        specs = DETECTOR_SPECS[detector]
        if setting not in specs:
            raise OverlayError(
                f"{detector} has no setting {setting!r}. Settings: "
                + ", ".join(sorted(specs))
            )

        parsed = _parse_scalar(value, specs[setting])
        issues: list[Issue] = []
        candidate = {setting: parsed}
        resolved = resolve(
            _apply_presets(detector, specs, candidate), candidate, detector, issues
        )
        # A clamp is a rejection here: the bot user asked for a specific number
        # and should be told it's out of range, not silently given another one.
        blocking = [i for i in issues if setting in i.path or i.path == detector]
        if blocking:
            raise OverlayError(
                f"{setting} rejected — {blocking[0].message.split(';')[0]}. "
                f"Allowed: {specs[setting].bounds_text()}."
            )

        final = resolved[setting]
        if ticker:
            sym = ticker.upper()
            self.per_ticker.setdefault(sym, {}).setdefault(detector, {})[setting] = final
            self._record(f"{sym}.{detector}.{setting} = {final}")
            return f"{sym}: {detector}.{setting} set to {final}."
        self.detectors.setdefault(detector, {})[setting] = final
        self._record(f"{detector}.{setting} = {final}")
        return f"{detector}.{setting} set to {final}."

    def set_run(self, setting: str, value: Any) -> str:
        if setting not in RUN_SPECS:
            raise OverlayError(
                f"unknown run setting {setting!r}. Settings: " + ", ".join(sorted(RUN_SPECS))
            )
        parsed = _parse_scalar(value, RUN_SPECS[setting])
        issues: list[Issue] = []
        candidate = {setting: parsed}
        resolved = resolve(RUN_SPECS, candidate, "run", issues)
        if issues:
            raise OverlayError(
                f"{setting} rejected — {issues[0].message.split(';')[0]}. "
                f"Allowed: {RUN_SPECS[setting].bounds_text()}."
            )
        self.run[setting] = resolved[setting]
        self._record(f"run.{setting} = {resolved[setting]}")
        return f"run.{setting} set to {resolved[setting]}."

    def set_canslim(self, setting: str, value: Any) -> str:
        if setting not in CANSLIM_SPECS:
            raise OverlayError(
                f"unknown canslim setting {setting!r}. Settings: "
                + ", ".join(sorted(CANSLIM_SPECS))
            )
        parsed = _parse_scalar(value, CANSLIM_SPECS[setting])
        issues: list[Issue] = []
        candidate = {setting: parsed}
        resolved = resolve(CANSLIM_SPECS, candidate, "canslim", issues)
        if issues:
            raise OverlayError(
                f"{setting} rejected — {issues[0].message.split(';')[0]}. "
                f"Allowed: {CANSLIM_SPECS[setting].bounds_text()}."
            )
        self.canslim[setting] = resolved[setting]
        self._record(f"canslim.{setting} = {resolved[setting]}")
        return f"canslim.{setting} set to {resolved[setting]}."

    def set_enabled(self, detector: str, enabled: bool) -> str:
        if detector not in DETECTOR_SPECS:
            raise OverlayError(
                f"unknown detector {detector!r}. Available: " + ", ".join(DETECTOR_SPECS)
            )
        self.detectors.setdefault(detector, {})["enabled"] = bool(enabled)
        self._record(f"{detector}.enabled = {enabled}")
        return f"{detector} {'enabled' if enabled else 'disabled'}."

    def reset(self, detector: str | None = None) -> str:
        if detector is None:
            self.detectors.clear()
            self.run.clear()
            self.canslim.clear()
            self.per_ticker.clear()
            self._record("reset all thresholds to config.yaml")
            return "All thresholds reset to the committed defaults (watchlist untouched)."
        if detector not in DETECTOR_SPECS:
            raise OverlayError(f"unknown detector {detector!r}.")
        self.detectors.pop(detector, None)
        for per in self.per_ticker.values():
            per.pop(detector, None)
        self._record(f"reset {detector}")
        return f"{detector} reset to the committed defaults."

    def is_empty(self) -> bool:
        return not any(
            (
                self.added_tickers,
                self.removed_tickers,
                self.detectors,
                self.run,
                self.canslim,
                self.per_ticker,
            )
        )

    def describe(self) -> list[str]:
        out: list[str] = []
        if self.added_tickers:
            out.append("added tickers: " + ", ".join(self.added_tickers))
        if self.removed_tickers:
            out.append("removed tickers: " + ", ".join(self.removed_tickers))
        for detector, settings in sorted(self.detectors.items()):
            for key, value in sorted(settings.items()):
                out.append(f"{detector}.{key} = {value}")
        for key, value in sorted(self.run.items()):
            out.append(f"run.{key} = {value}")
        for key, value in sorted(self.canslim.items()):
            out.append(f"canslim.{key} = {value}")
        for ticker, per in sorted(self.per_ticker.items()):
            for detector, settings in sorted(per.items()):
                for key, value in sorted(settings.items()):
                    out.append(f"{ticker}.{detector}.{key} = {value}")
        return out


def _parse_scalar(value: Any, spec: Param) -> Any:
    """Turn bot text into the type the spec wants, with a clear failure."""
    if isinstance(value, str):
        text = value.strip()
        if spec.kind == "bool":
            lowered = text.lower()
            if lowered in {"on", "true", "yes", "1", "enable", "enabled"}:
                return True
            if lowered in {"off", "false", "no", "0", "disable", "disabled"}:
                return False
            raise OverlayError(f"expected on/off, got {value!r}")
        if spec.kind == "list[str]":
            return [p.strip() for p in text.replace(",", " ").split() if p.strip()]
        if spec.kind == "str":
            return text
        cleaned = text.replace(",", "").replace("$", "").replace("_", "")
        # Accept 2M / 500k shorthand — natural to type on a phone.
        multipliers = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
        if cleaned and cleaned[-1].lower() in multipliers:
            try:
                return float(cleaned[:-1]) * multipliers[cleaned[-1].lower()]
            except ValueError as exc:
                raise OverlayError(f"expected a number, got {value!r}") from exc
        try:
            return float(cleaned)
        except ValueError as exc:
            raise OverlayError(f"expected a number, got {value!r}") from exc
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
