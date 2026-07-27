"""Configuration schema.

Defaults follow the conventions market analysts actually use — see the `doc`
string on each `Param` for the reasoning, and README.md for the full table.
Every one is overridable in ``config.yaml`` within the stated bounds.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .params import ConfigError, Issue, Param, resolve

# --------------------------------------------------------------------------
# L1 — volume / price anomaly on aggregated bars
# --------------------------------------------------------------------------
VOLUME_ANOMALY = {
    "enabled": Param(True, kind="bool", doc="Turn this detector on or off."),
    "bar_interval": Param(
        "5min",
        choices=("1min", "5min", "15min"),
        kind="str",
        doc="Bar size to test. 1min is twitchy; 5min is the usual compromise.",
    ),
    "rvol_threshold": Param(
        2.0,
        lo=1.2,
        hi=20.0,
        doc="Relative volume: bar volume divided by its normal volume for that "
        "same time of day. RVOL >= 2 is the standard 'unusual' line, >= 5 is "
        "extreme.",
    ),
    "zscore_threshold": Param(
        3.0,
        lo=1.5,
        hi=10.0,
        doc="Standard deviations above the same-time-of-day mean. 3 sigma is "
        "the conventional outlier cut.",
    ),
    "combine": Param(
        "all",
        choices=("any", "all"),
        kind="str",
        doc="Whether RVOL and z-score must both trip ('all') or either is "
        "enough ('any'). 'all' is much quieter.",
    ),
    "baseline_sessions": Param(
        20,
        lo=5,
        hi=60,
        kind="int",
        doc="Trailing sessions used for the baseline. 20 matches the standard "
        "20-day average volume analysts quote.",
    ),
    "min_bar_notional": Param(
        250_000,
        lo=10_000,
        hi=1_000_000_000,
        doc="Ignore bars trading less than this dollar value, so thin names "
        "don't fire on statistical noise.",
    ),
    "min_price_move_pct": Param(
        0.0,
        lo=0.0,
        hi=50.0,
        doc="Optional confirmation: require the bar to also move price by this "
        "%. 0 disables it.",
    ),
    "warmup_minutes": Param(
        5,
        lo=0,
        hi=60,
        kind="int",
        doc="Skip this many minutes after the open. The opening auction makes "
        "every first bar look like an outlier.",
    ),
    "skip_last_minutes": Param(
        0,
        lo=0,
        hi=60,
        kind="int",
        doc="Skip this many minutes before the close, for the same reason as "
        "warmup (closing auction).",
    ),
    "cooldown_minutes": Param(
        60,
        lo=0,
        hi=1440,
        kind="int",
        doc="Per-ticker silence window after a fire, so one busy session "
        "doesn't produce forty alerts.",
    ),
}

# --------------------------------------------------------------------------
# L2 — individual large prints
# --------------------------------------------------------------------------
# "Block trade" has a textbook definition (10,000 shares or $200,000, the old
# NYSE threshold) that is small by modern standards, so presets are offered.
BLOCK_PRESETS: dict[str, tuple[int, float]] = {
    # name: (min_shares, min_notional)
    "classic": (10_000, 200_000),
    "institutional": (25_000, 1_000_000),
    "mega": (100_000, 5_000_000),
}

BLOCK_TRADES = {
    # Off by default: with Unusual Whales as the trades provider this reads the
    # same off-exchange stream as `dark_pool`, which is the better-calibrated of
    # the two. Enable it when you want share-count thresholds instead.
    "enabled": Param(False, kind="bool", doc="Turn this detector on or off."),
    "preset": Param(
        "institutional",
        choices=("classic", "institutional", "mega", "custom"),
        kind="str",
        doc="Sizing preset. classic = the traditional 10k share / $200k NYSE "
        "block definition; institutional = 25k / $1M; mega = 100k / $5M. Any "
        "explicit min_shares or min_notional you set overrides the preset.",
    ),
    "min_shares": Param(
        25_000,
        lo=100,
        hi=100_000_000,
        kind="int",
        doc="Share-count floor for a single print.",
    ),
    "min_notional": Param(
        1_000_000,
        lo=10_000,
        hi=1_000_000_000,
        doc="Dollar-value floor for a single print.",
    ),
    "combine": Param(
        "any",
        choices=("any", "all"),
        kind="str",
        doc="Whether a print must clear both the share and dollar floors "
        "('all') or either one ('any').",
    ),
    "min_pct_of_adv": Param(
        0.25,
        lo=0.0,
        hi=25.0,
        doc="Also require the print to be at least this % of the name's "
        "average daily volume — the size that actually matters is size "
        "relative to normal liquidity. 0 disables it.",
    ),
    "off_exchange_only": Param(
        False,
        kind="bool",
        doc="Only alert on prints reported off-exchange (TRF / ATS).",
    ),
    "cooldown_minutes": Param(15, lo=0, hi=1440, kind="int", doc="Per-ticker silence window."),
    "max_alerts_per_run": Param(
        5, lo=1, hi=50, kind="int", doc="Cap per ticker per run; the largest prints win."
    ),
}

DARK_POOL = {
    "enabled": Param(True, kind="bool", doc="Turn this detector on or off."),
    "min_notional": Param(
        1_000_000,
        lo=50_000,
        hi=1_000_000_000,
        doc="Dollar floor for a dark pool print.",
    ),
    "min_pct_of_adv": Param(
        0.5,
        lo=0.0,
        hi=25.0,
        doc="Print size as a % of average daily volume. 0.5% off-exchange in "
        "one print is generally considered notable. 0 disables it.",
    ),
    "combine": Param(
        "any",
        choices=("any", "all"),
        kind="str",
        doc="Whether both the dollar and %-of-ADV tests must trip, or either.",
    ),
    "cooldown_minutes": Param(15, lo=0, hi=1440, kind="int", doc="Per-ticker silence window."),
    "max_alerts_per_run": Param(
        5, lo=1, hi=50, kind="int", doc="Cap per ticker per run; the largest prints win."
    ),
}

# --------------------------------------------------------------------------
# L3 — options flow
# --------------------------------------------------------------------------
OPTIONS_FLOW = {
    "enabled": Param(True, kind="bool", doc="Turn this detector on or off."),
    "min_premium": Param(
        100_000,
        lo=10_000,
        hi=1_000_000_000,
        doc="Total premium paid. $100k is the conventional floor for calling a "
        "trade 'whale' flow.",
    ),
    "require_volume_gt_oi": Param(
        True,
        kind="bool",
        doc="Require contract volume to exceed open interest, which implies "
        "the position is newly opened rather than an existing one changing "
        "hands. This is the standard unusual-flow filter.",
    ),
    "trade_types": Param(
        ["sweep", "block", "split"],
        choices=("sweep", "block", "split"),
        kind="list[str]",
        doc="Which execution styles to include. Sweeps (filled across several "
        "exchanges at once) read as the most urgent.",
    ),
    "min_dte": Param(
        0, lo=0, hi=2000, kind="int", doc="Minimum days to expiry."
    ),
    "max_dte": Param(
        365,
        lo=1,
        hi=2000,
        kind="int",
        doc="Maximum days to expiry. Long-dated LEAPS flow is usually hedging, "
        "not a directional signal.",
    ),
    "exclude_deep_itm_pct": Param(
        20.0,
        lo=0.0,
        hi=100.0,
        doc="Drop trades more than this % in the money — deep ITM size is "
        "often a stock substitute or assignment mechanic, not a bet. 0 "
        "disables the filter.",
    ),
    "cooldown_minutes": Param(15, lo=0, hi=1440, kind="int", doc="Per-ticker silence window."),
    "max_alerts_per_run": Param(
        5, lo=1, hi=50, kind="int", doc="Cap per ticker per run; the largest premium wins."
    ),
}

# --------------------------------------------------------------------------
# L3-lite — chain-level option volume (IBKR, no options-flow subscription)
# --------------------------------------------------------------------------
OPTION_VOLUME = {
    "enabled": Param(False, kind="bool", doc="Turn this detector on or off. Needs an IBKR gateway."),
    "min_ratio": Param(
        2.5,
        lo=1.2,
        hi=25.0,
        doc="Today's option volume divided by its average, pace-adjusted for how "
        "much of the session has elapsed. 2-3x is the usual 'unusual activity' line.",
    ),
    "min_contracts": Param(
        5_000,
        lo=100,
        hi=100_000_000,
        doc="Ignore names whose whole chain is too thin for a ratio to mean anything.",
    ),
    "min_session_pct": Param(
        20.0,
        lo=5.0,
        hi=100.0,
        doc="Wait until this much of the session has elapsed. These are "
        "day-cumulative figures, so an early-morning comparison against a "
        "full-day average is noise.",
    ),
    "cooldown_minutes": Param(240, lo=0, hi=1440, kind="int", doc="Per-ticker silence window."),
    "max_alerts_per_run": Param(
        1, lo=1, hi=5, kind="int", doc="It is one cumulative daily fact; once is enough."
    ),
}

# --------------------------------------------------------------------------
# Insider trades — SEC Form 4
# --------------------------------------------------------------------------
INSIDER_TRADES = {
    "enabled": Param(True, kind="bool", doc="Turn this detector on or off."),
    "alert_on": Param(
        ["purchase", "sale"],
        choices=("purchase", "sale", "exercise", "grant", "gift", "tax", "other"),
        kind="list[str]",
        doc="Which Form 4 transaction categories to report. Open-market "
        "purchases carry by far the most signal; grants and tax withholding "
        "are compensation mechanics, not decisions.",
    ),
    "min_notional_purchase": Param(
        100_000,
        lo=0,
        hi=1_000_000_000,
        doc="Dollar floor for reporting an insider purchase.",
    ),
    "min_notional_sale": Param(
        500_000,
        lo=0,
        hi=1_000_000_000,
        doc="Dollar floor for sales. Set higher than purchases because selling "
        "is far noisier — diversification, taxes and scheduled plans all look "
        "the same on the filing.",
    ),
    "exclude_10b5_1_sales": Param(
        True,
        kind="bool",
        doc="Skip sales flagged as made under a pre-arranged 10b5-1 plan. Those "
        "were scheduled months earlier and carry little information.",
    ),
    "include_derivative": Param(
        False,
        kind="bool",
        doc="Include derivative-table lines (option exercises and conversions). "
        "Mostly compensation noise.",
    ),
    "cluster_window_days": Param(
        30,
        lo=1,
        hi=180,
        kind="int",
        doc="Window for detecting a cluster buy — several insiders at the same "
        "company buying independently, which is the strongest documented "
        "insider signal. Clusters are escalated to high severity.",
    ),
    "cluster_min_insiders": Param(
        2,
        lo=2,
        hi=20,
        kind="int",
        doc="How many distinct insiders must buy inside the window to count as "
        "a cluster.",
    ),
    "lookback_days": Param(
        7,
        lo=1,
        hi=90,
        kind="int",
        doc="How far back to scan EDGAR for filings not yet seen. Form 4 is due "
        "within 2 business days of the trade, so a week covers late filers.",
    ),
    "max_alerts_per_run": Param(
        10, lo=1, hi=50, kind="int", doc="Cap per ticker per run."
    ),
}

DETECTOR_SPECS: dict[str, dict[str, Param]] = {
    "volume_anomaly": VOLUME_ANOMALY,
    "block_trades": BLOCK_TRADES,
    "dark_pool": DARK_POOL,
    "options_flow": OPTIONS_FLOW,
    "option_volume": OPTION_VOLUME,
    "insider_trades": INSIDER_TRADES,
}

# Which fidelity level each detector belongs to, for `monitor explain`.
DETECTOR_LEVEL = {
    "volume_anomaly": "L1",
    "block_trades": "L2",
    "dark_pool": "L2",
    "options_flow": "L3",
    "option_volume": "L3-lite",
    "insider_trades": "Form 4",
}

# --------------------------------------------------------------------------
# CAN SLIM grading — the deterministic rubric pass, and the optional LLM narrator
# --------------------------------------------------------------------------
CANSLIM_SPECS = {
    "narrator": Param(
        "off",
        choices=("off", "llm"),
        kind="str",
        doc="'off' scores every letter programmatically against the skill's "
        "rubric. 'llm' additionally has Claude write the per-letter prose and "
        "judge the two letters numbers cannot settle — N's 'new' driver and I's "
        "sponsorship quality. Needs an Anthropic API key and costs per grade.",
    ),
    "narrator_model": Param(
        "claude-opus-5",
        kind="str",
        doc="Which Claude model narrates. Leave at the default unless you have a "
        "specific reason to change tier.",
    ),
    "narrator_effort": Param(
        "medium",
        choices=("low", "medium", "high", "xhigh", "max"),
        kind="str",
        doc="Reasoning effort for the narration call. 'medium' is a good balance "
        "for prose over already-computed figures; raise it if the reads read thin.",
    ),
    "narrator_research": Param(
        True,
        kind="bool",
        doc="Let the narrator web-search for the 'new' driver and recent "
        "sponsorship news. This is what makes N genuinely gradeable rather than "
        "scored on its chart half alone. Adds a call per grade.",
    ),
    "narrator_max_tokens": Param(
        16000,
        lo=2_000,
        hi=64_000,
        kind="int",
        doc="Output ceiling for the narration call. Thinking and response text "
        "share this budget, so do not set it tight.",
    ),
    "narrator_fallbacks": Param(
        True,
        kind="bool",
        doc="Ask the API to re-run a safety-declined request on a fallback model "
        "automatically. Costs nothing when nothing is declined.",
    ),
}

RUN_SPECS = {
    "extended_hours": Param(
        False,
        kind="bool",
        doc="Also run during pre-market and after-hours sessions.",
    ),
    "cold_start_lookback_minutes": Param(
        30,
        lo=5,
        hi=1440,
        kind="int",
        doc="When no prior state exists, only consider events this recent. "
        "Stops a lost cache from replaying a whole day at you.",
    ),
    "max_alerts_per_run": Param(
        25,
        lo=1,
        hi=200,
        kind="int",
        doc="Global cap across all tickers and detectors for one run.",
    ),
    "min_severity": Param(
        "low",
        choices=("low", "medium", "high"),
        kind="str",
        doc="Suppress anything below this severity.",
    ),
    "state_retention_days": Param(
        30, lo=2, hi=365, kind="int", doc="How long dedup keys are kept."
    ),
    "unresponsive_after": Param(
        3,
        lo=1,
        hi=10,
        kind="int",
        doc="Consecutive failed calls to one data source before it is reported "
        "as down. One blip is not an outage.",
    ),
    "max_stale_intervals": Param(
        4,
        lo=2,
        hi=50,
        kind="int",
        doc="How many bar intervals behind the newest bar may fall during a "
        "session before the feed is called stale. A frozen feed looks healthy "
        "from the outside, so this is the check that catches it.",
    ),
    "max_feed_silence_minutes": Param(
        60,
        lo=10,
        hi=480,
        kind="int",
        doc="How long the print and flow feeds may go without a single new event "
        "across the whole watchlist before they are called frozen. Per-ticker "
        "silence proves nothing — a thin name really can have no dark-pool prints "
        "for an hour — but every ticker at once means the feed, not the market.",
    ),
    "attach_canslim": Param(
        True,
        kind="bool",
        doc="Attach a CAN SLIM scorecard PDF to alerts. Graded once per ticker "
        "per day and reused, so a busy name costs one grade, not one per alert.",
    ),
    "canslim_min_severity": Param(
        "medium",
        choices=("low", "medium", "high"),
        kind="str",
        doc="Only attach a scorecard to alerts at or above this severity — "
        "grading every low-severity alert is rarely worth the calls.",
    ),
    "canslim_max_age_minutes": Param(
        90,
        lo=0,
        hi=1440,
        kind="int",
        doc="How old an attached scorecard may be before it is re-graded. A "
        "verdict turns on quarterly earnings so it barely moves intraday, but the "
        "price and pivot on it do — a morning grade stapled to an afternoon alert "
        "quotes a stale price. 0 re-grades on every alert (expensive).",
    ),
}

# Unusual Whales REST paths. Kept in config because their published docs are
# not readable without an account — if a path 404s, `monitor verify` will say
# so and you can correct it here without touching code.
UW_DEFAULT_PATHS = {
    "dark_pool_ticker": "/api/darkpool/{ticker}",
    "dark_pool_recent": "/api/darkpool/recent",
    "flow_alerts": "/api/option-trades/flow-alerts",
    "ticker_flow_alerts": "/api/stock/{ticker}/flow-alerts",
    "stock_ohlc": "/api/stock/{ticker}/ohlc/{interval}",
    "stock_info": "/api/stock/{ticker}/info",
}

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


@dataclass
class ProviderConfig:
    bars: str = "fmp"
    trades: str = "unusual_whales"
    flow: str = "unusual_whales"
    option_volume: str = "ibkr"
    insider: str = "sec_edgar"
    uw_base_url: str = "https://api.unusualwhales.com"
    uw_paths: dict[str, str] = field(default_factory=lambda: dict(UW_DEFAULT_PATHS))
    fmp_base_url: str = "https://financialmodelingprep.com"
    sec_user_agent: str = ""
    request_timeout: int = 30
    #: Client Portal Gateway URL. Empty disables IBKR — it needs a running,
    #: interactively-authenticated gateway, so it cannot work on CI runners.
    ibkr_base_url: str = ""
    ibkr_fields: dict[str, str] = field(default_factory=dict)
    ibkr_volume_multiplier: int = 100


@dataclass
class Config:
    tickers: list[str]
    detectors: dict[str, dict[str, Any]]
    run: dict[str, Any]
    providers: ProviderConfig
    canslim: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    #: The watchlist as committed in config.yaml, before any runtime overlay.
    #: The bot needs this to tell "remove a YAML ticker" from "undo an add".
    baseline_tickers: list[str] = field(default_factory=list)

    def detector(self, name: str, ticker: str | None = None) -> dict[str, Any]:
        """Settings for a detector, with any per-ticker override applied."""
        base = dict(self.detectors[name])
        if ticker:
            base.update(self.overrides.get(ticker, {}).get(name, {}))
        return base

    def enabled_detectors(self) -> list[str]:
        return [n for n in DETECTOR_SPECS if self.detectors[n]["enabled"]]

    def needs(self, provider_role: str) -> bool:
        """Whether any enabled detector requires a given provider role."""
        enabled = set(self.enabled_detectors())
        return bool(enabled & {
            "bars": {"volume_anomaly"},
            "trades": {"block_trades", "dark_pool"},
            "flow": {"options_flow"},
            "option_volume": {"option_volume"},
            "insider": {"insider_trades"},
        }[provider_role])


def load(
    path: str | Path, strict: bool = False, overlay: Any | None = None
) -> Config:
    raw_path = Path(path)
    if not raw_path.exists():
        raise ConfigError(
            f"config file not found: {raw_path}. Copy config.example.yaml to "
            f"{raw_path.name} and add your tickers."
        )
    data = yaml.safe_load(raw_path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{raw_path} must contain a YAML mapping at the top level.")
    return from_dict(data, strict=strict, overlay=overlay)


def from_dict(
    data: dict[str, Any], strict: bool = False, overlay: Any | None = None
) -> Config:
    """Build a Config, optionally merging a runtime overlay over the baseline.

    The overlay is merged into the *raw* mapping before validation, so a
    bot-applied threshold is checked by exactly the same code path as one typed
    into the YAML. `overlay` is duck-typed (a `runtime.Overlay`) to keep this
    module free of a circular import.
    """
    issues: list[Issue] = []

    baseline_tickers = _clean_tickers(data.get("tickers"), issues)
    tickers = baseline_tickers
    if overlay is not None:
        data = _merge_overlay(data, overlay)
        tickers = _clean_tickers(
            overlay.effective_tickers(baseline_tickers), issues
        )

    detectors: dict[str, dict[str, Any]] = {}
    supplied_detectors = data.get("detectors") or {}
    if not isinstance(supplied_detectors, dict):
        raise ConfigError("`detectors` must be a mapping of detector name to settings.")
    for unknown in sorted(set(supplied_detectors) - set(DETECTOR_SPECS)):
        issues.append(
            Issue(
                f"detectors.{unknown}",
                f"unknown detector {unknown!r}; available: "
                + ", ".join(DETECTOR_SPECS),
            )
        )
    for name, specs in DETECTOR_SPECS.items():
        supplied = supplied_detectors.get(name) or {}
        if not isinstance(supplied, dict):
            issues.append(Issue(f"detectors.{name}", "expected a mapping of settings"))
            supplied = {}
        detectors[name] = resolve(
            _apply_presets(name, specs, supplied), supplied, f"detectors.{name}", issues
        )

    run = resolve(RUN_SPECS, data.get("run") or {}, "run", issues)
    canslim = resolve(
        CANSLIM_SPECS, _yaml_off_on(data.get("canslim") or {}), "canslim", issues
    )
    overrides = _clean_overrides(data.get("overrides") or {}, tickers, issues)
    providers = _provider_config(data.get("providers") or {}, issues)

    if strict and issues:
        raise ConfigError(
            "invalid configuration:\n"
            + "\n".join(f"  - {i.path}: {i.message}" for i in issues)
        )
    return Config(
        tickers=tickers,
        detectors=detectors,
        run=run,
        providers=providers,
        overrides=overrides,
        issues=issues,
        baseline_tickers=baseline_tickers,
        canslim=canslim,
    )


def _yaml_off_on(settings: Any) -> Any:
    """Read a bare `narrator: off` the way whoever typed it meant it.

    YAML 1.1 turns unquoted ``off``/``on`` into booleans, so the obvious thing to
    write in a config file arrives here as ``False``. Rejecting that would be
    technically correct and useless. ``off`` means off; ``on`` means the only
    backend there is.
    """
    if not isinstance(settings, dict) or not isinstance(settings.get("narrator"), bool):
        return settings
    return {**settings, "narrator": "llm" if settings["narrator"] else "off"}


def _merge_overlay(data: dict[str, Any], overlay: Any) -> dict[str, Any]:
    """Layer the overlay's detector, run and per-ticker edits over the raw YAML."""
    merged = dict(data)

    detectors = {k: dict(v or {}) for k, v in (merged.get("detectors") or {}).items()}
    for name, settings in (overlay.detectors or {}).items():
        detectors.setdefault(name, {}).update(settings)
    merged["detectors"] = detectors

    merged["run"] = {**(merged.get("run") or {}), **(overlay.run or {})}
    merged["canslim"] = {**(merged.get("canslim") or {}), **(getattr(overlay, "canslim", None) or {})}

    overrides = {
        k: {d: dict(s or {}) for d, s in (v or {}).items()}
        for k, v in (merged.get("overrides") or {}).items()
    }
    for ticker, per_detector in (overlay.per_ticker or {}).items():
        target = overrides.setdefault(ticker.upper(), {})
        for detector, settings in per_detector.items():
            target.setdefault(detector, {}).update(settings)
    merged["overrides"] = overrides
    return merged


def _apply_presets(
    name: str, specs: dict[str, Param], supplied: dict[str, Any]
) -> dict[str, Param]:
    """Let `preset` move the defaults for the sizing params it covers."""
    if name != "block_trades":
        return specs
    preset = str(supplied.get("preset", specs["preset"].default))
    if preset not in BLOCK_PRESETS:
        return specs
    shares, notional = BLOCK_PRESETS[preset]
    out = dict(specs)
    out["min_shares"] = replace(specs["min_shares"], default=shares)
    out["min_notional"] = replace(specs["min_notional"], default=notional)
    return out


def _clean_tickers(value: Any, issues: list[Issue]) -> list[str]:
    if not value:
        raise ConfigError("`tickers` is empty — list at least one symbol to watch.")
    if isinstance(value, str):
        value = [value]
    out: list[str] = []
    for item in value:
        sym = str(item).strip().upper()
        if not TICKER_RE.match(sym):
            issues.append(Issue("tickers", f"{item!r} does not look like a ticker; skipped"))
            continue
        if sym not in out:
            out.append(sym)
    if not out:
        raise ConfigError("no valid tickers left after validation.")
    return out


def _clean_overrides(
    value: Any, tickers: list[str], issues: list[Issue]
) -> dict[str, dict[str, dict[str, Any]]]:
    if not isinstance(value, dict):
        issues.append(Issue("overrides", "expected a mapping of ticker to settings"))
        return {}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for ticker, per_detector in value.items():
        sym = str(ticker).strip().upper()
        if sym not in tickers:
            issues.append(
                Issue(f"overrides.{ticker}", f"{sym} is not in `tickers`; override ignored")
            )
            continue
        if not isinstance(per_detector, dict):
            issues.append(Issue(f"overrides.{sym}", "expected a mapping of detector settings"))
            continue
        resolved: dict[str, dict[str, Any]] = {}
        for det, settings in per_detector.items():
            if det not in DETECTOR_SPECS:
                issues.append(
                    Issue(f"overrides.{sym}.{det}", f"unknown detector {det!r}")
                )
                continue
            if not isinstance(settings, dict):
                issues.append(Issue(f"overrides.{sym}.{det}", "expected a mapping"))
                continue
            specs = _apply_presets(det, DETECTOR_SPECS[det], settings)
            full = resolve(specs, settings, f"overrides.{sym}.{det}", issues)
            # Keep only the keys actually overridden, so detector() merges cleanly.
            resolved[det] = {k: v for k, v in full.items() if k in settings}
        if resolved:
            out[sym] = resolved
    return out


def _provider_config(value: dict[str, Any], issues: list[Issue]) -> ProviderConfig:
    cfg = ProviderConfig()
    roles = {
        "bars": ("fmp", "ibkr"),
        "trades": ("unusual_whales",),
        "flow": ("unusual_whales",),
        "option_volume": ("ibkr",),
        "insider": ("sec_edgar",),
    }
    for role, allowed in roles.items():
        if role in value:
            choice = str(value[role])
            if choice not in allowed:
                issues.append(
                    Issue(
                        f"providers.{role}",
                        f"{choice!r} is not supported; allowed: {', '.join(allowed)}",
                    )
                )
            else:
                setattr(cfg, role, choice)

    uw = value.get("unusual_whales") or {}
    if isinstance(uw, dict):
        cfg.uw_base_url = str(uw.get("base_url", cfg.uw_base_url)).rstrip("/")
        supplied_paths = uw.get("paths") or {}
        if isinstance(supplied_paths, dict):
            for key, path in supplied_paths.items():
                if key not in UW_DEFAULT_PATHS:
                    issues.append(
                        Issue(
                            f"providers.unusual_whales.paths.{key}",
                            "unknown path key; available: "
                            + ", ".join(UW_DEFAULT_PATHS),
                        )
                    )
                    continue
                cfg.uw_paths[key] = str(path)

    fmp = value.get("fmp") or {}
    if isinstance(fmp, dict):
        cfg.fmp_base_url = str(fmp.get("base_url", cfg.fmp_base_url)).rstrip("/")

    ibkr = value.get("ibkr") or {}
    if isinstance(ibkr, dict):
        cfg.ibkr_base_url = str(ibkr.get("base_url", "") or "").rstrip("/")
        fields = ibkr.get("fields") or {}
        if isinstance(fields, dict):
            cfg.ibkr_fields = {str(k): str(v) for k, v in fields.items()}
        multiplier = ibkr.get("history_volume_multiplier")
        if multiplier is not None:
            try:
                cfg.ibkr_volume_multiplier = max(1, int(multiplier))
            except (TypeError, ValueError):
                issues.append(
                    Issue(
                        "providers.ibkr.history_volume_multiplier",
                        "expected an integer (100 for lot-quoted history, 1 for shares)",
                    )
                )

    sec = value.get("sec") or {}
    if isinstance(sec, dict):
        cfg.sec_user_agent = str(sec.get("user_agent", "") or "")
    cfg.sec_user_agent = os.environ.get("SEC_USER_AGENT", cfg.sec_user_agent)

    timeout = value.get("request_timeout")
    if timeout is not None:
        try:
            cfg.request_timeout = max(5, min(120, int(timeout)))
        except (TypeError, ValueError):
            issues.append(Issue("providers.request_timeout", "expected a number of seconds"))
    return cfg
