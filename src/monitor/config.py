"""Configuration: a bounded schema, per-ticker overrides, and a runtime overlay.

Three rules shape this module.

**Every tunable has a declared range.** A threshold is not a free number; it is a
number with a plausible band. `rvol_threshold: 0.1` would fire on every bar of
every day and `rvol_threshold: 500` would never fire at all, and both would look
like a working config. Bounds turn those into a message instead of a mystery.

**Bad config from a file is clamped and reported; bad config from the bot is
refused.** A file is edited once and read on every run — clamping keeps the
monitor alive and tells you what it did. A `/set` from chat is interactive, and
silently storing something other than what was typed is worse than an error.

**Per-ticker overrides are first class.** Measured on real 30-minute bars, MSFT
never once crossed `rvol_threshold: 2.0` over thirteen sessions while NVDA
crossed it twice. One global number cannot serve both; pretending otherwise just
means the quieter names are never monitored.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Configuration that cannot be used as written."""


# --------------------------------------------------------------------------- #
# the bounded parameter
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Param:
    default: Any
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] | None = None
    kind: str = "float"          # float | int | bool | str | list
    doc: str = ""
    #: False for things the chat bot must not rewrite — paths, credentials,
    #: anything where a typo breaks the next run silently.
    tunable: bool = True

    def coerce(self, value: Any) -> Any:
        """Turn a YAML/JSON scalar into the declared type, or raise."""
        if self.kind == "bool":
            return _as_bool(value)
        if self.kind == "int":
            if isinstance(value, bool):
                raise ConfigError(f"expected a number, got {value!r}")
            try:
                return int(value)
            except (TypeError, ValueError):
                raise ConfigError(f"expected an integer, got {value!r}") from None
        if self.kind == "float":
            if isinstance(value, bool):
                raise ConfigError(f"expected a number, got {value!r}")
            try:
                return float(value)
            except (TypeError, ValueError):
                raise ConfigError(f"expected a number, got {value!r}") from None
        if self.kind == "list":
            if isinstance(value, str):
                return [v.strip() for v in value.split(",") if v.strip()]
            if isinstance(value, (list, tuple)):
                return [str(v).strip() for v in value]
            raise ConfigError(f"expected a list, got {value!r}")
        # str
        text = _off_on(value)
        if self.choices and text not in self.choices:
            raise ConfigError(f"expected one of {', '.join(self.choices)}, got {text!r}")
        return text

    def check(self, value: Any) -> str | None:
        """Return a complaint if `value` is out of band, else None."""
        if self.kind in ("int", "float"):
            if self.lo is not None and value < self.lo:
                return f"{value:g} is below the minimum of {self.lo:g}"
            if self.hi is not None and value > self.hi:
                return f"{value:g} is above the maximum of {self.hi:g}"
        if self.kind == "str" and self.choices and value not in self.choices:
            return f"{value!r} is not one of {', '.join(self.choices)}"
        return None

    def clamp(self, value: Any) -> Any:
        if self.kind in ("int", "float"):
            if self.lo is not None:
                value = max(value, self.lo)
            if self.hi is not None:
                value = min(value, self.hi)
            return int(value) if self.kind == "int" else float(value)
        return value

    def describe(self) -> str:
        if self.choices:
            band = " | ".join(self.choices)
        elif self.lo is not None or self.hi is not None:
            band = f"{self.lo if self.lo is not None else '-inf':g}..{self.hi if self.hi is not None else 'inf':g}"
        else:
            band = self.kind
        return f"[{band}] default {self.default!r} — {self.doc}"


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    raise ConfigError(f"expected true/false, got {value!r}")


def _off_on(value: Any) -> str:
    """Undo YAML 1.1's habit of reading bare `off` and `on` as booleans.

    `narrator: off` is the natural way to write it and parses to `False`, which
    then fails a choices check with the baffling message "False is not one of
    off, llm, hybrid".
    """
    if value is False:
        return "off"
    if value is True:
        return "on"
    return str(value).strip()


# --------------------------------------------------------------------------- #
# the schema
# --------------------------------------------------------------------------- #

P = Param

SCHEMA: dict[str, Param] = {
    # -- what to watch -------------------------------------------------------
    "watchlist": P([], kind="list", tunable=True,
                   doc="Tickers to monitor. Every source is polled per ticker."),

    # -- where data comes from ----------------------------------------------
    "sources.bars": P("fmp", choices=("fmp", "ibkr", "replay"), kind="str", tunable=False,
                      doc="Intraday OHLCV. FMP needs a paid plan; IBKR needs a running gateway."),
    "sources.options": P("ibkr", choices=("ibkr", "replay", "off"), kind="str", tunable=False,
                         doc="Option chains with open interest. 'off' disables the OI signal."),
    "sources.insider": P("sec", choices=("sec", "replay", "off"), kind="str", tunable=False,
                         doc="Form 4 filings. SEC EDGAR is free but requires a contact user-agent."),
    "sources.trades": P("off", choices=("replay", "off"), kind="str", tunable=False,
                        doc="Individual prints, for block detection. No retail-priced feed "
                            "exposes these, so this is 'off' unless you have captured data."),
    "sources.replay_dir": P("state/replay", kind="str", tunable=False,
                            doc="Captured snapshots used when a source is set to 'replay'."),

    # -- cadence and baselines ----------------------------------------------
    "poll.bar_minutes": P(30, lo=1, hi=390, kind="int", tunable=False,
                          doc="Bar size requested from the bars source."),
    "poll.baseline_sessions": P(10, lo=3, hi=60, kind="int",
                                doc="Prior sessions used to build the same-slot volume baseline."),
    "poll.min_baseline_samples": P(5, lo=2, hi=60, kind="int",
                                   doc="Below this many samples the baseline is refused, not guessed."),
    "poll.max_bar_age_minutes": P(120, lo=5, hi=1440, kind="int",
                                  doc="A bar older than this is stale data, not a quiet market."),

    # -- signal: volume ------------------------------------------------------
    "signals.volume.enabled": P(True, kind="bool"),
    "signals.volume.rvol_threshold": P(2.0, lo=1.1, hi=20.0,
                                       doc="Volume vs the median of the same clock slot. The binding constraint in practice."),
    "signals.volume.zscore_threshold": P(2.0, lo=0.5, hi=8.0,
                                         doc="Standard deviations above the same-slot mean."),
    "signals.volume.min_price_move_pct": P(0.5, lo=0.0, hi=25.0,
                                           doc="Absolute move within the bar. Cuts churn that goes nowhere."),
    "signals.volume.min_notional": P(5_000_000.0, lo=0.0, hi=1e11,
                                     doc="Dollar floor, so a heavy bar in a thin name is not a headline."),
    "signals.volume.combine": P("all", choices=("all", "any"), kind="str",
                                doc="Whether every threshold must trip, or any one of them."),

    # -- signal: blocks ------------------------------------------------------
    # Off by default, and not because it is unimportant. Block detection needs
    # individual trade prints, and no feed at this price point publishes them —
    # FMP and the IBKR gateway both stop at bars. Turn this on when you have a
    # tick source captured into the replay directory.
    "signals.blocks.enabled": P(False, kind="bool"),
    "signals.blocks.min_shares": P(10_000, lo=100, hi=10_000_000, kind="int",
                                   doc="Classic block definition. Below this it is ordinary flow."),
    "signals.blocks.min_notional": P(200_000.0, lo=1000.0, hi=1e10,
                                     doc="Classic block definition, dollar leg."),
    "signals.blocks.institutional_shares": P(25_000, lo=1000, hi=20_000_000, kind="int",
                                             doc="Escalates severity one step."),
    "signals.blocks.institutional_notional": P(1_000_000.0, lo=10_000.0, hi=1e10),
    "signals.blocks.mega_shares": P(100_000, lo=5000, hi=50_000_000, kind="int",
                                    doc="Escalates severity a second step."),
    "signals.blocks.mega_notional": P(5_000_000.0, lo=50_000.0, hi=1e11),
    "signals.blocks.off_exchange_only": P(False, kind="bool",
                                          doc="Restrict to prints reported by a TRF, i.e. dark-pool style."),
    "signals.blocks.max_per_ticker": P(5, lo=1, hi=50, kind="int",
                                       doc="A busy tape can print dozens; report the largest few."),

    # -- signal: open interest ----------------------------------------------
    "signals.open_interest.enabled": P(True, kind="bool"),
    "signals.open_interest.min_oi_change": P(500, lo=1, hi=1_000_000, kind="int",
                                             doc="Contracts opened overnight. The only proof a position was taken."),
    "signals.open_interest.min_oi_change_pct": P(20.0, lo=1.0, hi=1000.0,
                                                 doc="Change relative to the contract's existing open interest."),
    "signals.open_interest.min_notional": P(250_000.0, lo=0.0, hi=1e10,
                                            doc="Contracts x 100 x strike. Filters cheap far-out lottery tickets."),
    "signals.open_interest.max_days_to_expiry": P(120, lo=1, hi=1500, kind="int",
                                                  doc="Ignore LEAPS; they move on hedging, not conviction."),
    "signals.open_interest.top_n": P(5, lo=1, hi=50, kind="int",
                                     doc="Largest contracts reported per ticker."),

    # -- how much of the chain to pull --------------------------------------
    "options.strike_window_pct": P(10.0, lo=1.0, hi=100.0,
                                   doc="Only strikes within this percent of spot. Bounds a very expensive fetch."),
    "options.expiries": P(3, lo=1, hi=12, kind="int",
                          doc="Nearest expiry months to pull."),
    "options.max_contracts": P(240, lo=10, hi=2000, kind="int",
                               doc="Hard cap per ticker, so a liquid name cannot run for an hour."),

    # -- signal: insider -----------------------------------------------------
    "signals.insider.enabled": P(True, kind="bool"),
    "signals.insider.min_value": P(100_000.0, lo=0.0, hi=1e10,
                                   doc="Dollar floor on a single Form 4 line."),
    "signals.insider.cluster_window_days": P(30, lo=2, hi=180, kind="int",
                                             doc="Window in which separate buyers count as a cluster."),
    "signals.insider.cluster_min_buyers": P(2, lo=2, hi=20, kind="int",
                                            doc="Distinct insiders buying before it is called a cluster."),
    "signals.insider.include_planned_sales": P(False, kind="bool",
                                               doc="10b5-1 sales are scheduled months ahead and carry no signal."),
    "signals.insider.lookback_days": P(7, lo=1, hi=90, kind="int",
                                       doc="How far back each poll asks EDGAR for filings."),

    # -- CAN SLIM ------------------------------------------------------------
    "canslim.enabled": P(True, kind="bool"),
    "canslim.attach_to_alerts": P(True, kind="bool",
                                  doc="Append a grade to alerts at or above min_severity."),
    "canslim.min_severity": P("medium", choices=("low", "medium", "high"), kind="str"),
    "canslim.narrator": P("off", choices=("off", "llm", "hybrid"), kind="str",
                          doc="'off' is deterministic scoring only. 'llm' adds prose. 'hybrid' lets prose adjust soft factors."),
    "canslim.model": P("claude-opus-5", kind="str", tunable=False),
    "canslim.cache_hours": P(24, lo=1, hi=720, kind="int",
                             doc="Fundamentals change quarterly; re-fetching hourly is waste."),

    # -- notification --------------------------------------------------------
    "notify.channel": P("console", choices=("telegram", "console"), kind="str", tunable=False),
    "notify.min_severity": P("low", choices=("low", "medium", "high"), kind="str",
                             doc="Alerts below this are stored but not sent."),
    "notify.max_per_run": P(12, lo=1, hi=100, kind="int",
                            doc="A cap, so one chaotic hour cannot flood the chat."),
    "notify.include_source_issues": P(True, kind="bool",
                                      doc="Report unreachable or stale sources. Silence from a dead feed looks like calm."),

    # -- health --------------------------------------------------------------
    "health.max_feed_silence_minutes": P(180, lo=15, hi=1440, kind="int",
                                         doc="Watchlist-wide non-advancement for this long means a frozen feed."),
    "health.fail_on_unreachable": P(False, kind="bool",
                                    doc="Exit non-zero when a required source is down, for systemd to notice."),

    # -- plumbing ------------------------------------------------------------
    "state.path": P("state/monitor.db", kind="str", tunable=False),
    "state.retention_days": P(45, lo=7, hi=3650, kind="int",
                              doc="Dedup and history pruning. Kept above the longest signal window."),
    "http.timeout_seconds": P(20.0, lo=2.0, hi=180.0, tunable=False),
    "http.retries": P(3, lo=0, hi=10, kind="int", tunable=False),
    "http.backoff_seconds": P(1.5, lo=0.1, hi=60.0, tunable=False),
    "ibkr.base_url": P("https://localhost:5000/v1/api", kind="str", tunable=False,
                       doc="Client Portal Gateway. Local, so TLS is self-signed."),
    "ibkr.verify_tls": P(False, kind="bool", tunable=False,
                         doc="False only because the gateway ships a self-signed localhost certificate."),
    "ibkr.volume_multiplier": P(100.0, lo=0.01, hi=10_000.0, tunable=False,
                                doc="The gateway reports bar volume in hundreds. Affects notional "
                                    "floors only — RVOL is a ratio, so the factor cancels."),
    "ibkr.oi_field": P("7638", kind="str", tunable=False,
                       doc="Snapshot field id for option open interest. IBKR has renumbered fields "
                           "between gateway builds; `monitor verify --raw` prints what yours returns."),
    "ibkr.option_volume_field": P("7089", kind="str", tunable=False,
                                  doc="Snapshot field id for per-contract option volume."),
    "ibkr.snapshot_batch": P(50, lo=1, hi=500, kind="int", tunable=False,
                             doc="Contracts per snapshot request."),
}


# --------------------------------------------------------------------------- #
# flattening
# --------------------------------------------------------------------------- #

def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                out.update(_flatten(value, path))
            else:
                out[path] = value
    elif prefix:
        out[prefix] = node
    return out


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, value in sorted(flat.items()):
        parts = path.split(".")
        cursor = out
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return out


# --------------------------------------------------------------------------- #
# the resolved config
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    values: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    source_path: Path | None = None

    # -- reading ------------------------------------------------------------
    def get(self, path: str, ticker: str | None = None) -> Any:
        if ticker:
            per = self.overrides.get(ticker.upper(), {})
            if path in per:
                return per[path]
        if path in self.values:
            return self.values[path]
        if path in SCHEMA:
            return SCHEMA[path].default
        raise ConfigError(f"unknown setting {path!r}")

    def __getitem__(self, path: str) -> Any:
        return self.get(path)

    @property
    def watchlist(self) -> list[str]:
        return [t.upper() for t in self.get("watchlist")]

    def section(self, prefix: str, ticker: str | None = None) -> dict[str, Any]:
        """Every setting under a dotted prefix, keyed by its final segment."""
        return {
            path[len(prefix) + 1:]: self.get(path, ticker)
            for path in SCHEMA
            if path.startswith(prefix + ".")
        }

    def overrides_for(self, ticker: str) -> dict[str, Any]:
        return dict(self.overrides.get(ticker.upper(), {}))

    # -- writing ------------------------------------------------------------
    def set(self, path: str, raw: Any, ticker: str | None = None) -> Any:
        """Apply an interactive edit. Refuses anything out of band.

        Deliberately does *not* clamp: a `/set` typed in chat gets an answer,
        not a quietly different number.
        """
        param = SCHEMA.get(path)
        if param is None:
            raise ConfigError(f"unknown setting {path!r}")
        if not param.tunable:
            raise ConfigError(f"{path} is not adjustable at runtime — edit config.yaml and restart")
        value = param.coerce(raw)
        complaint = param.check(value)
        if complaint:
            raise ConfigError(f"{path}: {complaint}")
        if ticker:
            self.overrides.setdefault(ticker.upper(), {})[path] = value
        else:
            self.values[path] = value
        return value

    def unset(self, path: str, ticker: str) -> bool:
        per = self.overrides.get(ticker.upper(), {})
        return per.pop(path, _MISSING) is not _MISSING

    # -- checking -----------------------------------------------------------
    def validate(self) -> list[str]:
        """Hard errors — things that make a run pointless rather than merely odd."""
        problems: list[str] = []
        if not self.watchlist:
            problems.append("watchlist is empty — nothing to monitor")
        if len(self.watchlist) != len(set(self.watchlist)):
            dupes = sorted({t for t in self.watchlist if self.watchlist.count(t) > 1})
            problems.append(f"watchlist has duplicates: {', '.join(dupes)}")

        for path, value in self.values.items():
            param = SCHEMA.get(path)
            if param is None:
                continue
            complaint = param.check(value)
            if complaint:
                problems.append(f"{path}: {complaint}")

        enabled = [
            name for name in ("volume", "blocks", "open_interest", "insider")
            if self.get(f"signals.{name}.enabled")
        ]
        if not enabled:
            problems.append("every signal is disabled — the monitor would have nothing to report")

        if self.get("sources.options") == "off" and self.get("signals.open_interest.enabled"):
            problems.append(
                "signals.open_interest is enabled but sources.options is 'off' — "
                "open interest is the only signal that shows a position was actually taken"
            )
        if self.get("sources.insider") == "off" and self.get("signals.insider.enabled"):
            problems.append("signals.insider is enabled but sources.insider is 'off'")
        if self.get("sources.trades") == "off" and self.get("signals.blocks.enabled"):
            problems.append(
                "signals.blocks is enabled but sources.trades is 'off' — block detection "
                "needs individual prints, which bars cannot supply"
            )

        if self.get("poll.min_baseline_samples") > self.get("poll.baseline_sessions"):
            problems.append(
                "poll.min_baseline_samples exceeds poll.baseline_sessions — "
                "the baseline could never gather enough samples"
            )
        if self.get("state.retention_days") < self.get("signals.insider.cluster_window_days"):
            problems.append(
                "state.retention_days is shorter than signals.insider.cluster_window_days — "
                "insider history would be pruned before a cluster could form"
            )
        return problems

    # -- serialising --------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        out = _nest(self.values)
        if self.overrides:
            out["overrides"] = {
                ticker: _nest(paths) for ticker, paths in sorted(self.overrides.items())
            }
        return out

    def dump_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, default_flow_style=False)


_MISSING = object()


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

def _resolve(flat: dict[str, Any], where: str, warnings: list[str]) -> dict[str, Any]:
    """Coerce and clamp a flat mapping against the schema, collecting complaints."""
    out: dict[str, Any] = {}
    for path, raw in flat.items():
        param = SCHEMA.get(path)
        if param is None:
            warnings.append(f"{where}: ignoring unknown setting {path!r}")
            continue
        try:
            value = param.coerce(raw)
        except ConfigError as exc:
            warnings.append(f"{where}: {path} — {exc}; using default {param.default!r}")
            out[path] = param.default
            continue
        complaint = param.check(value)
        if complaint:
            clamped = param.clamp(value)
            warnings.append(f"{where}: {path} — {complaint}; clamped to {clamped!r}")
            value = clamped
        out[path] = value
    return out


def load(path: str | Path, overlay: str | Path | None = None) -> Config:
    """Read config.yaml, apply the runtime overlay, and resolve against the schema.

    Never raises for an out-of-band number — those are clamped and recorded in
    `warnings` so the run still happens and the operator still hears about it.
    Structural failures (missing file, unparsable YAML, wrong top-level shape)
    do raise, because there is nothing sensible to fall back to.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")

    warnings: list[str] = []
    raw = copy.deepcopy(raw)
    raw_overrides = raw.pop("overrides", {}) or {}

    values = _resolve(_flatten(raw), str(path), warnings)

    overrides: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_overrides, dict):
        warnings.append(f"{path}: 'overrides' must map tickers to settings; ignoring")
        raw_overrides = {}
    for ticker, block in raw_overrides.items():
        key = str(ticker).upper()
        if not isinstance(block, dict):
            warnings.append(f"{path}: overrides.{key} must be a mapping; ignoring")
            continue
        resolved = _resolve(_flatten(block), f"{path} overrides.{key}", warnings)
        if resolved:
            overrides[key] = resolved

    config = Config(values=values, overrides=overrides, warnings=warnings, source_path=path)

    if overlay:
        _apply_overlay(config, Path(overlay))

    unknown = set(overrides) - set(config.watchlist)
    for ticker in sorted(unknown):
        warnings.append(f"overrides.{ticker} has no matching watchlist entry")

    return config


def _apply_overlay(config: Config, overlay_path: Path) -> None:
    """Merge runtime edits made from chat over the file-based config.

    The overlay exists so a `/set` survives until the next scheduled run without
    rewriting the user's YAML. It is machine-written, so a parse failure here is
    a bug worth surfacing rather than a user typo worth tolerating.
    """
    if not overlay_path.exists():
        return
    try:
        payload = json.loads(overlay_path.read_text() or "{}")
    except (json.JSONDecodeError, OSError) as exc:
        config.warnings.append(f"runtime overlay {overlay_path} is unreadable ({exc}); ignoring")
        return
    if not isinstance(payload, dict):
        config.warnings.append(f"runtime overlay {overlay_path} is not an object; ignoring")
        return

    where = f"overlay {overlay_path}"
    config.values.update(_resolve(payload.get("values", {}) or {}, where, config.warnings))
    for ticker, block in (payload.get("overrides", {}) or {}).items():
        key = str(ticker).upper()
        resolved = _resolve(block or {}, f"{where} {key}", config.warnings)
        if resolved:
            config.overrides.setdefault(key, {}).update(resolved)


# --------------------------------------------------------------------------- #
# the runtime overlay
# --------------------------------------------------------------------------- #

@dataclass
class Overlay:
    """Runtime edits, stored flat so a schema rename shows up as an unknown key.

    Written by the bot, read by the scheduled run. Keeping it separate from
    config.yaml means the file the operator hand-edits is never rewritten by a
    machine, and clearing runtime state is one `rm`.
    """

    path: Path
    values: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Overlay:
        path = Path(path)
        if not path.exists():
            return cls(path=path)
        try:
            payload = json.loads(path.read_text() or "{}")
        except (json.JSONDecodeError, OSError):
            return cls(path=path)
        return cls(
            path=path,
            values=payload.get("values", {}) or {},
            overrides={k.upper(): v for k, v in (payload.get("overrides", {}) or {}).items()},
        )

    def set(self, path: str, raw: Any, ticker: str | None = None) -> Any:
        """Validate through the same gate as Config.set, then persist."""
        probe = Config()
        value = probe.set(path, raw, ticker)
        if ticker:
            self.overrides.setdefault(ticker.upper(), {})[path] = value
        else:
            self.values[path] = value
        self.save()
        return value

    def unset(self, path: str, ticker: str | None = None) -> bool:
        target = self.overrides.get(ticker.upper(), {}) if ticker else self.values
        removed = target.pop(path, _MISSING) is not _MISSING
        if removed:
            self.save()
        return removed

    def clear(self) -> None:
        self.values.clear()
        self.overrides.clear()
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "values": self.values,
            "overrides": {k: v for k, v in self.overrides.items() if v},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def is_empty(self) -> bool:
        return not self.values and not any(self.overrides.values())


def tunable_paths() -> list[str]:
    return sorted(path for path, param in SCHEMA.items() if param.tunable)
