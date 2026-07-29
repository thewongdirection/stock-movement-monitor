"""Building the set of sources a run will use.

A source that cannot be constructed — a missing key, an absent replay directory
— does not abort the run. It becomes a visible `SourceIssue` and its role is
left empty, so a broken Form 4 feed still leaves volume monitoring working and
still tells you that insider coverage is dark. Failing the whole run instead
would trade partial information for none.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from ..config import Config
from ..models import SourceIssue
from .base import (BadResponse, EmptyResponse, HttpClient, NotConfigured, SourceError,
                   StaleData, Unreachable, redact)
from .fmp import FmpSource
from .ibkr import IbkrSource
from .replay import ReplaySource, write_bars, write_chain
from .sec import SecSource

__all__ = [
    "SourceSet", "build", "HttpClient", "SourceError", "Unreachable", "BadResponse",
    "EmptyResponse", "StaleData", "NotConfigured", "redact",
    "FmpSource", "IbkrSource", "SecSource", "ReplaySource", "write_bars", "write_chain",
]

#: Every credential the project reads, and what it unlocks.
ENV_VARS = {
    "FMP_API_KEY": "Financial Modeling Prep — intraday bars and CAN SLIM fundamentals (paid plan)",
    "SEC_USER_AGENT": "Contact address sent to SEC EDGAR, e.g. 'monitor you@example.com'",
    "TELEGRAM_BOT_TOKEN": "Telegram delivery and the chat bot",
    "TELEGRAM_CHAT_ID": "Where alerts are sent",
    "ANTHROPIC_API_KEY": "Optional — only for canslim.narrator: llm/hybrid",
    "IBKR_BASE_URL": "Optional — overrides ibkr.base_url if the gateway is not on localhost:5000",
}


@dataclass
class SourceSet:
    bars: Any = None
    options: Any = None
    insider: Any = None
    trades: Any = None
    #: Fundamentals for the CAN SLIM grader. A separate role because it is
    #: needed regardless of where bars come from — an IBKR bars setup still
    #: needs FMP to know what last quarter's EPS was.
    fundamentals: Any = None
    issues: list[SourceIssue] = field(default_factory=list)
    clients: list[HttpClient] = field(default_factory=list)

    def close(self) -> None:
        for client in self.clients:
            client.close()

    def __enter__(self) -> SourceSet:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def missing(self) -> list[str]:
        return [
            role for role in ("bars", "options", "insider", "trades")
            if getattr(self, role) is None
        ]


def build(config: Config, env: Mapping[str, str] | None = None,
          as_of: datetime | None = None) -> SourceSet:
    """Construct every configured source, recording rather than raising failures."""
    env = env if env is not None else os.environ
    result = SourceSet()

    def client(name: str, **kwargs) -> HttpClient:
        made = HttpClient(
            name,
            timeout=config.get("http.timeout_seconds"),
            retries=config.get("http.retries"),
            backoff=config.get("http.backoff_seconds"),
            **kwargs,
        )
        result.clients.append(made)
        return made

    def replay() -> ReplaySource:
        return ReplaySource(config.get("sources.replay_dir"), as_of=as_of)

    def ibkr() -> IbkrSource:
        return IbkrSource(
            client("ibkr", verify=config.get("ibkr.verify_tls")),
            base_url=env.get("IBKR_BASE_URL") or config.get("ibkr.base_url"),
            volume_multiplier=config.get("ibkr.volume_multiplier"),
            oi_field=config.get("ibkr.oi_field"),
            option_volume_field=config.get("ibkr.option_volume_field"),
            snapshot_batch=config.get("ibkr.snapshot_batch"),
            strike_window_pct=config.get("options.strike_window_pct"),
            max_contracts=config.get("options.max_contracts"),
            expiries=config.get("options.expiries"),
        )

    builders = {
        "bars": {
            "fmp": lambda: FmpSource(env.get("FMP_API_KEY"), client("fmp")),
            "ibkr": ibkr,
            "replay": replay,
        },
        "options": {"ibkr": ibkr, "replay": replay},
        "insider": {
            "sec": lambda: SecSource(client("sec"), env.get("SEC_USER_AGENT")),
            "replay": replay,
        },
        "trades": {"replay": replay},
    }

    for role, options in builders.items():
        choice = config.get(f"sources.{role}")
        if choice == "off":
            continue
        factory = options.get(choice)
        if factory is None:
            result.issues.append(SourceIssue(
                source=choice, ticker="*", kind="unconfigured",
                detail=f"'{choice}' cannot supply {role}; valid choices are "
                       f"{', '.join(sorted(options))} or off",
            ))
            continue
        try:
            setattr(result, role, factory())
        except SourceError as exc:
            result.issues.append(exc.issue())
        except Exception as exc:                      # noqa: BLE001 - reported, not swallowed
            result.issues.append(SourceIssue(
                source=choice, ticker="*", kind="unconfigured",
                detail=redact(f"{type(exc).__name__}: {exc}"),
            ))

    if config.get("canslim.enabled"):
        if isinstance(result.bars, FmpSource):
            result.fundamentals = result.bars          # reuse the client and its session
        elif env.get("FMP_API_KEY"):
            result.fundamentals = FmpSource(env["FMP_API_KEY"], client("fmp"))
        else:
            result.issues.append(SourceIssue(
                source="fmp", ticker="*", kind="unconfigured",
                detail="CAN SLIM is enabled but FMP_API_KEY is not set — "
                       "scorecards will not be attached to alerts",
            ))

    return result


def missing_credentials(config: Config, env: Mapping[str, str] | None = None) -> list[str]:
    """Which env vars this particular configuration needs but does not have.

    Deliberately configuration-aware: demanding FMP_API_KEY when bars come from
    IBKR is noise, and noise in a preflight check trains people to ignore it.
    """
    env = env if env is not None else os.environ
    needed: list[str] = []
    if config.get("sources.bars") == "fmp":
        needed.append("FMP_API_KEY")
    if config.get("sources.insider") == "sec":
        needed.append("SEC_USER_AGENT")
    if config.get("notify.channel") == "telegram":
        needed += ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    if config.get("canslim.enabled") and config.get("canslim.narrator") != "off":
        needed.append("ANTHROPIC_API_KEY")
    if config.get("canslim.enabled") and "FMP_API_KEY" not in needed:
        # The grader reads fundamentals from FMP regardless of the bars source.
        needed.append("FMP_API_KEY")
    return [name for name in dict.fromkeys(needed) if not env.get(name)]
