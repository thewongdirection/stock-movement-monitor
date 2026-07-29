"""The chat command set, independent of how the chat arrives.

Two rules run through all of it.

**Every command reloads configuration and refetches data.** No command answers
from a cached scan. If you ask `/scan NVDA` at 11:04 you get an 11:04 fetch, and
if a source is unreachable you are told that rather than shown a stale answer
that looks current. This is the whole reason the class holds paths rather than
objects.

**A `/set` that cannot be honoured is refused, never rounded.** The file loader
clamps out-of-range values so a bad config still runs; an interactive edit gets
an error, because silently storing 20 when you typed 99 is how you end up
debugging a monitor that is not using the threshold you think it is.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .. import canslim as canslim_mod
from ..canslim import CanSlim
from ..clock import market_phase, now_et
from ..config import SCHEMA, Config, ConfigError, Overlay, load, tunable_paths
from ..engine import Engine
from ..models import ago
from ..notify.base import format_alert, format_digest, format_issues
from ..sources import build as build_sources
from ..sources import missing_credentials
from ..store import Store

HELP = """Commands

/status            — is the monitor alive, and what did it last see
/health            — probe every configured data source right now
/scan [TICKER]     — fetch fresh data and evaluate immediately
/grade TICKER      — CAN SLIM scorecard, refetched
/brief TICKER      — paste-ready request for the full can-slim-grader skill
/history [TICKER]  — recent alerts

/list              — watchlist and per-ticker overrides
/watch TICKER      — add to the watchlist
/unwatch TICKER    — remove from the watchlist

/params [filter]   — tunable settings with their ranges
/get PATH          — current value
/set PATH VALUE [TICKER]   — change it; per-ticker if a ticker is given
/unset PATH [TICKER]       — drop a runtime override
/reset             — clear every runtime change

Settings changed here live in the runtime overlay and apply to the next
scheduled run. config.yaml is never rewritten."""


@dataclass
class Reply:
    text: str
    html: bool = False


class Commands:
    """Holds paths, not state. Everything is rebuilt per command, on purpose."""

    def __init__(self, config_path: str | Path, overlay_path: str | Path,
                 env: Mapping[str, str] | None = None):
        self.config_path = Path(config_path)
        self.overlay_path = Path(overlay_path)
        self.env = env if env is not None else os.environ

    # -- plumbing -----------------------------------------------------------
    def config(self) -> Config:
        return load(self.config_path, overlay=self.overlay_path)

    def overlay(self) -> Overlay:
        return Overlay.load(self.overlay_path)

    def store(self, config: Config) -> Store:
        return Store(config.get("state.path"))

    def dispatch(self, line: str) -> Reply:
        parts = line.strip().split()
        if not parts:
            return Reply("")
        name = parts[0].lstrip("/").lower()
        args = parts[1:]
        handler = getattr(self, f"cmd_{name}", None)
        if handler is None:
            return Reply(f"Unknown command '{name}'. Try /help.")
        try:
            return handler(args)
        except ConfigError as exc:
            return Reply(f"✗ {exc}")
        except Exception as exc:                    # noqa: BLE001 - shown, not hidden
            return Reply(f"✗ {type(exc).__name__}: {exc}")

    # -- information --------------------------------------------------------
    def cmd_help(self, args: list[str]) -> Reply:
        return Reply(HELP)

    def cmd_status(self, args: list[str]) -> Reply:
        config = self.config()
        store = self.store(config)
        try:
            runs = store.last_runs(5)
            lines = [
                f"Watchlist: {', '.join(config.watchlist) or '(empty)'}",
                f"Market: {market_phase()} · {now_et():%Y-%m-%d %H:%M %Z}",
                f"Signals: {', '.join(self._enabled(config)) or 'none enabled'}",
                "",
            ]
            if runs:
                lines.append("Recent runs")
                for run in runs:
                    when = ago(now_et() - run.started_at.astimezone(now_et().tzinfo))
                    mark = "✓" if run.ok else "⚠"
                    lines.append(
                        f"  {mark} {when} ago — {run.scanned} scanned, "
                        f"{run.alerts} alerts, {run.issues} source issues"
                    )
            else:
                lines.append("No runs recorded yet.")

            counts = store.alert_counts(7)
            if counts:
                lines += ["", "Alerts in the last 7 days: " + ", ".join(
                    f"{signal} {n}" for signal, n in sorted(counts.items())
                )]

            overlay = self.overlay()
            if not overlay.is_empty():
                lines += ["", f"Runtime overrides active ({len(overlay.values)} global, "
                              f"{sum(len(v) for v in overlay.overrides.values())} per-ticker) "
                              f"— /list to see them"]
            if config.warnings:
                lines += ["", "Config warnings:"] + [f"  · {w}" for w in config.warnings]
            return Reply("\n".join(lines))
        finally:
            store.close()

    def cmd_health(self, args: list[str]) -> Reply:
        """Probe every source now. Never answers from the last run's result."""
        config = self.config()
        lines = [f"Probing sources at {now_et():%H:%M:%S %Z}", ""]

        missing = missing_credentials(config, self.env)
        if missing:
            lines.append("Missing credentials: " + ", ".join(missing))
            lines.append("")

        with build_sources(config, self.env) as sources:
            for issue in sources.issues:
                lines.append(issue.line())
            ticker = (args[0].upper() if args else
                      (config.watchlist[0] if config.watchlist else None))
            if ticker is None:
                lines.append("No ticker to probe with — add one with /watch.")
                return Reply("\n".join(lines))

            for role, probe in (
                ("bars", lambda s: f"{len(s.bars(ticker, config.get('poll.bar_minutes'), 5))} bars"),
                ("options", lambda s: f"{len(s.chain(ticker, 120).contracts)} contracts"),
                ("insider", lambda s: f"{len(s.filings(ticker, now_et().date().replace(day=1)))} filings"),
            ):
                source = getattr(sources, role)
                if source is None:
                    lines.append(f"— {role}: not configured")
                    continue
                started = time.monotonic()
                try:
                    detail = probe(source)
                except Exception as exc:            # noqa: BLE001
                    lines.append(f"✗ {role} ({source.name}): {exc}")
                else:
                    lines.append(f"✓ {role} ({source.name}): {detail} "
                                 f"in {time.monotonic() - started:.1f}s")

        lines += ["", canslim_mod.status()]
        return Reply("\n".join(lines))

    def cmd_scan(self, args: list[str]) -> Reply:
        config = self.config()
        tickers = [t.upper() for t in args] or config.watchlist
        if not tickers:
            return Reply("Nothing to scan — add a ticker with /watch.")

        store = self.store(config)
        try:
            with build_sources(config, self.env) as sources:
                engine = Engine(config, store, sources,
                                canslim=CanSlim(config, store, fmp=_fmp(sources)))
                result = engine.run(tickers)

            blocks = [f"Scanned {', '.join(tickers)} at {now_et():%H:%M %Z}"]
            if result.health.issues:
                blocks.append(format_issues(result.health.issues))
            for note in result.health.notes:
                blocks.append(f"· {note}")
            if result.alerts:
                for alert in result.alerts:
                    blocks.append(format_alert(alert, canslim=result.canslim.get(alert.ticker)))
            else:
                blocks.append(
                    "No alerts. "
                    + (f"{result.duplicates} already sent earlier. "
                       if result.duplicates else "")
                    + "Nothing crossed the thresholds."
                )
            return Reply("\n\n".join(b for b in blocks if b))
        finally:
            store.close()

    def cmd_grade(self, args: list[str]) -> Reply:
        if not args:
            return Reply("Usage: /grade TICKER")
        ticker = args[0].upper()
        config = self.config()
        store = self.store(config)
        try:
            with build_sources(config, self.env) as sources:
                fmp = _fmp(sources)
                if fmp is None:
                    return Reply(
                        "CAN SLIM grading needs FMP fundamentals, and no FMP source is "
                        "configured. Set FMP_API_KEY and sources.bars: fmp, or run the "
                        "can-slim-grader skill interactively."
                    )
                card = CanSlim(config, store, fmp=fmp).card(ticker, fresh=True)
            if card is None:
                return Reply("CAN SLIM grading is disabled (canslim.enabled: false).")
            return Reply(canslim_mod.render(card))
        finally:
            store.close()

    def cmd_brief(self, args: list[str]) -> Reply:
        if not args:
            return Reply("Usage: /brief TICKER")
        ticker = args[0].upper()
        config = self.config()
        store = self.store(config)
        try:
            with build_sources(config, self.env) as sources:
                fmp = _fmp(sources)
                card = CanSlim(config, store, fmp=fmp).card(ticker, fresh=True) if fmp else None
            if card is None:
                return Reply(f"Could not grade {ticker} — /health will say why.")
            return Reply(canslim_mod.brief(card))
        finally:
            store.close()

    def cmd_history(self, args: list[str]) -> Reply:
        config = self.config()
        store = self.store(config)
        try:
            rows = store.recent_alerts(15, args[0].upper() if args else None)
            if not rows:
                return Reply("No alerts recorded yet.")
            return Reply("\n".join(
                f"{row['sent_at'][:16].replace('T', ' ')}  {row['severity']:6}  "
                f"{row['signal']:14}  {row['headline']}"
                for row in rows
            ))
        finally:
            store.close()

    # -- watchlist ----------------------------------------------------------
    def cmd_list(self, args: list[str]) -> Reply:
        config = self.config()
        lines = [f"Watchlist ({len(config.watchlist)}): {', '.join(config.watchlist) or '—'}"]
        if config.overrides:
            lines.append("")
            lines.append("Per-ticker overrides")
            for ticker, paths in sorted(config.overrides.items()):
                for path, value in sorted(paths.items()):
                    lines.append(f"  {ticker}  {path} = {value}")
        overlay = self.overlay()
        if overlay.values:
            lines += ["", "Runtime (global)"]
            lines += [f"  {k} = {v}" for k, v in sorted(overlay.values.items())]
        return Reply("\n".join(lines))

    def cmd_watch(self, args: list[str]) -> Reply:
        if not args:
            return Reply("Usage: /watch TICKER")
        config = self.config()
        watchlist = list(config.watchlist)
        added = [t.upper() for t in args if t.upper() not in watchlist]
        if not added:
            return Reply("Already on the watchlist.")
        overlay = self.overlay()
        overlay.set("watchlist", watchlist + added)
        return Reply(f"Watching {', '.join(added)}. Watchlist is now "
                     f"{', '.join(watchlist + added)}.")

    def cmd_unwatch(self, args: list[str]) -> Reply:
        if not args:
            return Reply("Usage: /unwatch TICKER")
        config = self.config()
        drop = {t.upper() for t in args}
        remaining = [t for t in config.watchlist if t not in drop]
        if len(remaining) == len(config.watchlist):
            return Reply("Not on the watchlist.")
        overlay = self.overlay()
        overlay.set("watchlist", remaining)
        return Reply(f"Removed {', '.join(sorted(drop))}. Watchlist is now "
                     f"{', '.join(remaining) or '(empty)'}.")

    # -- settings -----------------------------------------------------------
    def cmd_params(self, args: list[str]) -> Reply:
        needle = args[0].lower() if args else ""
        rows = [
            f"{path}\n    {SCHEMA[path].describe()}"
            for path in tunable_paths() if needle in path.lower()
        ]
        if not rows:
            return Reply(f"No tunable settings match '{needle}'.")
        return Reply("\n".join(rows))

    def cmd_get(self, args: list[str]) -> Reply:
        if not args:
            return Reply("Usage: /get PATH [TICKER]")
        config = self.config()
        path = args[0]
        ticker = args[1].upper() if len(args) > 1 else None
        value = config.get(path, ticker)
        param = SCHEMA.get(path)
        scope = f" for {ticker}" if ticker else ""
        detail = f"\n    {param.describe()}" if param else ""
        return Reply(f"{path}{scope} = {value}{detail}")

    def cmd_set(self, args: list[str]) -> Reply:
        if len(args) < 2:
            return Reply("Usage: /set PATH VALUE [TICKER]")
        path, raw = args[0], args[1]
        ticker = args[2].upper() if len(args) > 2 else None
        overlay = self.overlay()
        value = overlay.set(path, raw, ticker)
        scope = f" for {ticker}" if ticker else ""
        return Reply(f"✓ {path}{scope} = {value}\nApplies from the next run.")

    def cmd_unset(self, args: list[str]) -> Reply:
        if not args:
            return Reply("Usage: /unset PATH [TICKER]")
        overlay = self.overlay()
        ticker = args[1].upper() if len(args) > 1 else None
        if overlay.unset(args[0], ticker):
            return Reply(f"✓ Removed the runtime override for {args[0]}"
                         + (f" on {ticker}" if ticker else "") + ".")
        return Reply("No runtime override was set for that.")

    def cmd_reset(self, args: list[str]) -> Reply:
        overlay = self.overlay()
        if overlay.is_empty():
            return Reply("No runtime changes to clear.")
        overlay.clear()
        return Reply("✓ Cleared every runtime change. config.yaml values are back in force.")

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _enabled(config: Config) -> list[str]:
        return [
            name for name in ("open_interest", "insider", "blocks", "volume")
            if config.get(f"signals.{name}.enabled")
        ]


def _fmp(sources):
    """The fundamentals client, or None when CAN SLIM cannot be graded."""
    return getattr(sources, "fundamentals", None)
