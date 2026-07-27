"""Command handling, shared by the console and Telegram front ends.

Replies are returned as a `Reply` — text plus optional buttons and file
attachments — rather than sent directly, so the same handler serves a terminal
and a chat window. Buttons carry callback strings that are themselves valid
commands, which means the console can offer numbered choices and Telegram can
offer an inline keyboard from one definition.
"""

from __future__ import annotations

import html
import logging
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import config as config_mod
from ..canslim.service import CanSlimService
from ..config import CANSLIM_SPECS, DETECTOR_LEVEL, DETECTOR_SPECS, RUN_SPECS, Config
from ..params import reference_table
from ..runtime import Overlay, OverlayError
from ..state import State

log = logging.getLogger(__name__)

SEVERITY_ICON = {"low": "🔵", "medium": "🟠", "high": "🔴"}
SCORE_MARK = {"pass": "✓", "partial": "~", "fail": "✗", "unknown": "?"}


@dataclass
class Button:
    label: str
    command: str


@dataclass
class Reply:
    text: str
    buttons: list[Button] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    #: Set when the command changed configuration, so the shell can persist it.
    dirty: bool = False


@dataclass
class BotContext:
    config_path: Path
    state_path: Path
    overlay: Overlay
    config: Config
    canslim: CanSlimService | None = None
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


class CommandRouter:
    """Parses a line of input and dispatches it."""

    def __init__(self, ctx: BotContext):
        self.ctx = ctx

    # -- entry point ------------------------------------------------------
    def handle(self, line: str) -> Reply:
        line = (line or "").strip()
        if not line:
            return self.cmd_help([])
        if line.startswith("/"):
            line = line[1:]
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        if not parts:
            return self.cmd_help([])

        name, args = parts[0].lower(), parts[1:]
        handler = HANDLERS.get(name)
        if handler is None:
            close = _closest(name)
            hint = f" Did you mean /{close}?" if close else ""
            return Reply(
                f"Unknown command <b>/{html.escape(name)}</b>.{hint}\n"
                "Send /help for the full list."
            )
        try:
            return getattr(self, handler)(args)
        except OverlayError as exc:
            return Reply(f"⚠️ {html.escape(str(exc))}")
        except Exception as exc:  # noqa: BLE001 - a bot must not die on bad input
            log.exception("command %s failed", name)
            return Reply(f"⚠️ {type(exc).__name__}: {html.escape(str(exc))}")

    # -- reload -----------------------------------------------------------
    def _reload(self) -> None:
        self.ctx.config = config_mod.load(
            self.ctx.config_path, strict=False, overlay=self.ctx.overlay
        )

    def _persist(self) -> None:
        self.ctx.overlay.save()
        self._reload()

    # -- help -------------------------------------------------------------
    def cmd_help(self, args: list[str]) -> Reply:
        return Reply(
            "<b>Watchlist</b>\n"
            "/list — watched tickers with 14-day signal counts\n"
            "/add NVDA [AAPL …] — start watching\n"
            "/remove NVDA — stop watching\n"
            "\n<b>Per ticker</b>\n"
            "/history NVDA [days] — signals in the last 14 days\n"
            "/grade NVDA — CAN SLIM scorecard + PDF\n"
            "\n<b>Tuning</b>\n"
            "/levels — fidelity levels L1-L3 and what each costs\n"
            "/on DETECTOR · /off DETECTOR — switch a level\n"
            "/config [DETECTOR] — current thresholds\n"
            "/set DETECTOR SETTING VALUE — change one\n"
            "/set NVDA DETECTOR SETTING VALUE — for one ticker only\n"
            "/run SETTING VALUE — global run settings\n"
            "/narrator [SETTING VALUE] — who writes the CAN SLIM letters\n"
            "/explain DETECTOR — every setting, its range and why\n"
            "/reset [DETECTOR] — back to the committed defaults\n"
            "/changes — what has been changed from config.yaml\n"
            "\n<b>Status</b>\n"
            "/status — health, last run, what's enabled",
            buttons=[
                Button("Watchlist", "list"),
                Button("Levels", "levels"),
                Button("Status", "status"),
            ],
        )

    # -- watchlist --------------------------------------------------------
    def _last_run(self, state: State) -> datetime | None:
        row = state.db.execute(
            "SELECT started_at FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        try:
            stamp = datetime.fromisoformat(str(row[0]))
        except ValueError:
            return None
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

    @staticmethod
    def _last_run_from(row: tuple | None) -> datetime | None:
        if not row:
            return None
        try:
            stamp = datetime.fromisoformat(str(row[0]))
        except ValueError:
            return None
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)

    def _trading_now(self) -> bool:
        from .. import market_calendar as cal

        running, _ = cal.should_run(
            self.ctx.now().astimezone(cal.ET),
            extended_hours=bool(self.ctx.config.run["extended_hours"]),
        )
        return running

    def cmd_list(self, args: list[str]) -> Reply:
        cfg = self.ctx.config
        now = self.ctx.now()
        with State(self.ctx.state_path) as state:
            counts = state.signal_counts(now, days=14)
            warning = _staleness_warning(self._last_run(state), now, self._trading_now())

        if not cfg.tickers:
            return Reply("The watchlist is empty. Add one with <code>/add NVDA</code>.")

        lines = ["<b>Watchlist</b> — signals in the last 14 days\n"]
        if warning:
            lines.append(warning + "\n")
        for ticker in cfg.tickers:
            count = counts.get(ticker, 0)
            marker = "•" if count == 0 else "▸"
            tail = "no signals" if count == 0 else f"<b>{count}</b> signal{'s' if count != 1 else ''}"
            source = "" if ticker in cfg.baseline_tickers else "  <i>(added via bot)</i>"
            lines.append(f"{marker} <code>{ticker}</code> — {tail}{source}")

        lines.append(
            "\nTap a ticker for its 14-day history, or use "
            "<code>/history TICKER</code> / <code>/grade TICKER</code>."
        )
        buttons = [Button(t, f"history {t}") for t in cfg.tickers[:12]]
        return Reply("\n".join(lines), buttons=buttons)

    def cmd_add(self, args: list[str]) -> Reply:
        if not args:
            return Reply("Usage: <code>/add NVDA [AAPL …]</code>")
        messages = [self.ctx.overlay.add_ticker(a) for a in args]
        self._persist()
        return Reply(
            "\n".join(f"✅ {html.escape(m)}" for m in messages)
            + f"\n\nNow watching {len(self.ctx.config.tickers)} ticker(s).",
            buttons=[Button("Show watchlist", "list")],
            dirty=True,
        )

    def cmd_remove(self, args: list[str]) -> Reply:
        if not args:
            return Reply("Usage: <code>/remove NVDA</code>")
        messages = [
            self.ctx.overlay.remove_ticker(a, self.ctx.config.baseline_tickers)
            for a in args
        ]
        self._persist()
        return Reply(
            "\n".join(f"✅ {html.escape(m)}" for m in messages)
            + f"\n\nNow watching {len(self.ctx.config.tickers)} ticker(s).",
            buttons=[Button("Show watchlist", "list")],
            dirty=True,
        )

    # -- history ----------------------------------------------------------
    def cmd_history(self, args: list[str]) -> Reply:
        if not args:
            return Reply("Usage: <code>/history NVDA [days]</code>")
        ticker = args[0].upper()
        days = 14
        if len(args) > 1:
            try:
                days = max(1, min(90, int(args[1])))
            except ValueError:
                return Reply(f"'{html.escape(args[1])}' is not a number of days.")

        now = self.ctx.now()
        with State(self.ctx.state_path) as state:
            signals = state.signals_for(ticker, now, days=days)
            warning = _staleness_warning(self._last_run(state), now, self._trading_now())

        watched = ticker in self.ctx.config.tickers
        header = f"<b>{ticker}</b> — last {days} days"
        if not watched:
            header += "\n<i>Not currently on the watchlist.</i>"
        if warning:
            header += "\n\n" + warning

        if not signals:
            return Reply(
                f"{header}\n\nNo signals recorded."
                + (
                    ""
                    if watched
                    else f" Add it with <code>/add {ticker}</code> to start monitoring."
                ),
                buttons=[
                    Button(f"CAN SLIM {ticker}", f"grade {ticker}"),
                    Button("Watchlist", "list"),
                ],
            )

        by_detector: dict[str, int] = {}
        for signal in signals:
            by_detector[signal["detector"]] = by_detector.get(signal["detector"], 0) + 1

        lines = [
            header,
            f"\n<b>{len(signals)}</b> signal(s): "
            + ", ".join(f"{k} ×{v}" for k, v in sorted(by_detector.items())),
            "",
        ]
        for signal in signals[:15]:
            icon = SEVERITY_ICON.get(signal["severity"], "•")
            when = signal["occurred_at"].astimezone().strftime("%b %d %H:%M")
            lines.append(
                f"{icon} <code>{when}</code> {html.escape(signal['headline'])}"
            )
            if signal["detail"]:
                lines.append(f"    <i>{html.escape(signal['detail'][:160])}</i>")
        if len(signals) > 15:
            lines.append(f"\n…and {len(signals) - 15} more.")

        return Reply(
            "\n".join(lines),
            buttons=[
                Button(f"CAN SLIM {ticker}", f"grade {ticker}"),
                Button("Watchlist", "list"),
            ],
        )

    # -- CAN SLIM ---------------------------------------------------------
    def cmd_grade(self, args: list[str]) -> Reply:
        if not args:
            return Reply("Usage: <code>/grade NVDA</code> · add <code>cached</code> to reuse today's")
        ticker = args[0].upper()
        # Interactive means now. Someone who types /grade is asking about the
        # market as it stands, so this re-fetches by default and the cached grade
        # is the thing you have to ask for — not the other way round.
        reuse = any(a.lower() in {"cached", "cache", "reuse"} for a in args[1:])
        service = self.ctx.canslim
        if service is None:
            return Reply(
                "CAN SLIM grading is not configured. It needs the can-slim-grader "
                "skill on disk and an FMP key:\n"
                "<code>git clone --depth 1 "
                "https://github.com/thewongdirection/can-slim-grader "
                "vendor/can-slim-grader</code>"
            )

        now = self.ctx.now()
        outcome = service.grade(ticker, now, force=not reuse)
        if not outcome.ok:
            return Reply(
                f"Could not grade <b>{ticker}</b>: {html.escape(outcome.skipped or 'unknown reason')}"
                "\n\n<i>Nothing was graded — no stale figures are being shown "
                "instead.</i>"
            )

        report = outcome.report
        assert report is not None
        grade = report.grade
        lines = [
            f"<b>{ticker}</b> — CAN SLIM {html.escape(grade.verdict)}",
            f"Score <b>{html.escape(grade.score_text)}</b>",
            "",
            " ".join(f"{L.key}{SCORE_MARK.get(L.score, '?')}" for L in grade.letters),
            "",
            html.escape(grade.summary),
        ]
        if grade.warnings:
            lines.append(
                "\n<i>Data gaps: " + html.escape("; ".join(grade.warnings[:3])) + "</i>"
            )
        if report.pdf_error:
            lines.append(
                f"\n<i>PDF unavailable ({html.escape(report.pdf_error[:120])}); "
                "the HTML report is attached instead.</i>"
            )

        # Say where the letters came from — the two passes are not equivalent and
        # the message must not imply otherwise, same as the PDF's disclaimer.
        if grade.narrated:
            lines.append(
                "\n<i>Measurable letters scored programmatically; the commentary "
                "and the judgement letters (N, I) were written by Claude and may "
                "contain errors. Decision support, not advice.</i>"
            )
        else:
            lines.append(
                "\n<i>Letters scored programmatically against the can-slim-grader "
                "rubric. Decision support, not advice.</i>"
            )

        # And how old the figures are. Silence here would read as "current".
        age = report.age_minutes(now)
        if report.from_cache and age is not None and age >= 1:
            lines.append(
                f"<i>♻️ Reused a grade from {_ago(age)} — figures are as of then. "
                f"<code>/grade {ticker}</code> re-runs it.</i>"
            )
        else:
            lines.append(f"<i>🕒 Graded just now, as of {html.escape(grade.as_of)}.</i>")

        files = [report.pdf_path] if report.has_pdf else [report.html_path]
        buttons = [
            Button(f"History {ticker}", f"history {ticker}"),
            Button("Watchlist", "list"),
        ]
        if report.from_cache:
            buttons.insert(0, Button("Re-grade now", f"grade {ticker}"))
        return Reply("\n".join(lines), files=[f for f in files if f], buttons=buttons)

    # -- levels & tuning --------------------------------------------------
    def cmd_levels(self, args: list[str]) -> Reply:
        cfg = self.ctx.config
        lines = ["<b>Detection levels</b>\n"]
        blurbs = {
            "volume_anomaly": "abnormal volume on bars, time-of-day normalised",
            "block_trades": "single large prints, sized in shares",
            "dark_pool": "off-exchange prints, sized in $ and % of ADV",
            "options_flow": "whale premium, sweeps, volume>OI (needs Unusual Whales)",
            "option_volume": "whole-chain option volume vs average (needs IBKR)",
            "insider_trades": "SEC Form 4 — free, no key",
        }
        for name in DETECTOR_SPECS:
            on = cfg.detectors[name]["enabled"]
            lines.append(
                f"{'🟢' if on else '⚪'} <code>{name}</code> "
                f"[{DETECTOR_LEVEL[name]}] — {blurbs.get(name, '')}"
            )
        lines.append(
            "\n<code>/on NAME</code> or <code>/off NAME</code> to switch one.\n"
            "<code>/config NAME</code> to see its thresholds."
        )
        buttons = [
            Button(
                f"{'Disable' if cfg.detectors[n]['enabled'] else 'Enable'} {n}",
                f"{'off' if cfg.detectors[n]['enabled'] else 'on'} {n}",
            )
            for n in DETECTOR_SPECS
        ]
        return Reply("\n".join(lines), buttons=buttons)

    def cmd_on(self, args: list[str]) -> Reply:
        return self._switch(args, True)

    def cmd_off(self, args: list[str]) -> Reply:
        return self._switch(args, False)

    def _switch(self, args: list[str], enabled: bool) -> Reply:
        if not args:
            return Reply(
                f"Usage: <code>/{'on' if enabled else 'off'} DETECTOR</code>\n"
                "Detectors: " + ", ".join(f"<code>{d}</code>" for d in DETECTOR_SPECS)
            )
        message = self.ctx.overlay.set_enabled(args[0], enabled)
        self._persist()
        note = ""
        if enabled and args[0] in {"options_flow", "dark_pool", "block_trades"}:
            note = "\n<i>Needs UW_API_KEY to produce anything.</i>"
        elif enabled and args[0] == "option_volume":
            note = "\n<i>Needs a running IBKR gateway.</i>"
        return Reply(
            f"✅ {html.escape(message)}{note}",
            buttons=[Button("Show levels", "levels")],
            dirty=True,
        )

    def cmd_config(self, args: list[str]) -> Reply:
        cfg = self.ctx.config
        if args:
            name = args[0].lower()
            if name not in DETECTOR_SPECS:
                return Reply(
                    f"Unknown detector <b>{html.escape(name)}</b>. Try: "
                    + ", ".join(f"<code>{d}</code>" for d in DETECTOR_SPECS)
                )
            settings = cfg.detector(name)
            lines = [
                f"<b>{name}</b> [{DETECTOR_LEVEL[name]}] — "
                f"{'enabled' if settings['enabled'] else 'disabled'}\n"
            ]
            for key, value in settings.items():
                if key == "enabled":
                    continue
                spec = DETECTOR_SPECS[name][key]
                changed = key in (self.ctx.overlay.detectors.get(name) or {})
                lines.append(
                    f"<code>{key}</code> = <b>{_fmt(value)}</b>"
                    + (" ✏️" if changed else "")
                    + f"\n    <i>{spec.bounds_text()}</i>"
                )
            lines.append(f"\n<code>/set {name} SETTING VALUE</code> to change one.")
            lines.append(f"<code>/explain {name}</code> for the reasoning.")
            return Reply("\n".join(lines))

        lines = ["<b>Run settings</b>\n"]
        for key, value in cfg.run.items():
            changed = key in self.ctx.overlay.run
            lines.append(
                f"<code>{key}</code> = <b>{_fmt(value)}</b>" + (" ✏️" if changed else "")
            )
        lines.append("\n<b>Detectors</b> — /config NAME for thresholds\n")
        for name in DETECTOR_SPECS:
            lines.append(
                f"{'🟢' if cfg.detectors[name]['enabled'] else '⚪'} <code>{name}</code>"
            )
        return Reply(
            "\n".join(lines),
            buttons=[Button(n, f"config {n}") for n in DETECTOR_SPECS],
        )

    def cmd_set(self, args: list[str]) -> Reply:
        usage = (
            "Usage:\n"
            "<code>/set DETECTOR SETTING VALUE</code>\n"
            "<code>/set TICKER DETECTOR SETTING VALUE</code> — one ticker only\n\n"
            "e.g. <code>/set volume_anomaly rvol_threshold 3</code>\n"
            "     <code>/set dark_pool min_notional 2.5M</code>\n"
            "     <code>/set TSLA volume_anomaly rvol_threshold 4</code>"
        )
        if len(args) < 3:
            return Reply(usage)

        # Four arguments with a known detector second means it's ticker-scoped.
        if len(args) >= 4 and args[1].lower() in DETECTOR_SPECS:
            ticker, detector, setting, value = args[0].upper(), args[1].lower(), args[2], " ".join(args[3:])
            if ticker not in self.ctx.config.tickers:
                return Reply(
                    f"{ticker} is not on the watchlist — add it first with "
                    f"<code>/add {ticker}</code>."
                )
            message = self.ctx.overlay.set_detector(detector, setting, value, ticker=ticker)
        else:
            detector, setting, value = args[0].lower(), args[1], " ".join(args[2:])
            if detector not in DETECTOR_SPECS:
                return Reply(
                    f"Unknown detector <b>{html.escape(detector)}</b>.\n\n" + usage
                )
            message = self.ctx.overlay.set_detector(detector, setting, value)

        self._persist()
        return Reply(
            f"✅ {html.escape(message)}",
            buttons=[Button(f"Show {detector}", f"config {detector}")],
            dirty=True,
        )

    def cmd_run(self, args: list[str]) -> Reply:
        if len(args) < 2:
            return Reply(
                "Usage: <code>/run SETTING VALUE</code>\nSettings: "
                + ", ".join(f"<code>{k}</code>" for k in RUN_SPECS)
            )
        message = self.ctx.overlay.set_run(args[0], " ".join(args[1:]))
        self._persist()
        return Reply(f"✅ {html.escape(message)}", dirty=True)

    def cmd_narrator(self, args: list[str]) -> Reply:
        """Show or change who writes the CAN SLIM letters.

        Its own command rather than a `/set` scope: turning the narrator on spends
        money per grade, so it should be hard to do by accident.
        """
        if not args:
            return self._narrator_status()
        if len(args) == 1:
            # `/narrator llm` and `/narrator off` are what anyone will type first.
            setting, value = "narrator", args[0]
        else:
            setting, value = args[0].lower(), " ".join(args[1:])
            # Every key already starts with `narrator_`; typing it twice after
            # /narrator is nobody's intention.
            if setting not in CANSLIM_SPECS and f"narrator_{setting}" in CANSLIM_SPECS:
                setting = f"narrator_{setting}"
        message = self.ctx.overlay.set_canslim(setting, value)
        self._persist()
        return Reply(
            f"✅ {html.escape(message)}",
            buttons=[Button("Show narrator", "narrator")],
            dirty=True,
        )

    def _narrator_status(self) -> Reply:
        settings = self.ctx.config.canslim
        on = settings.get("narrator") == "llm"
        lines = [
            f"<b>CAN SLIM narrator</b> — {'🟢 Claude' if on else '⚪ off (computed only)'}\n",
            "The measurable letters (C, A, S, L, M) are always computed from the "
            "figures and the narrator <i>cannot</i> change them. What it adds is the "
            "per-letter commentary and a judgement on N's new driver and I's "
            "sponsorship quality.\n"
            if on
            else "With it off, N's \"new\" story and I's sponsorship quality are "
            "reported as ungraded rather than guessed at.\n",
        ]
        for key, value in settings.items():
            spec = CANSLIM_SPECS[key]
            changed = key in self.ctx.overlay.canslim
            lines.append(
                f"<code>{key}</code> = <b>{_fmt(value)}</b>"
                + (" ✏️" if changed else "")
                + f"\n    <i>{spec.bounds_text()}</i>"
            )
        lines.append(
            "\n<code>/narrator llm</code> to turn it on, <code>/narrator off</code> "
            "to turn it off.\n<code>/narrator SETTING VALUE</code> for the rest."
        )
        if on:
            lines.append(
                "\n<i>Costs roughly $0.10-0.40 per ticker per day; grades are cached "
                "daily, so a busy name is charged once. Needs ANTHROPIC_API_KEY.</i>"
            )
        return Reply(
            "\n".join(lines),
            buttons=[Button("Turn off" if on else "Turn on", f"narrator {'off' if on else 'llm'}")],
        )

    def cmd_explain(self, args: list[str]) -> Reply:
        if not args:
            return Reply(
                "Usage: <code>/explain DETECTOR</code>\nDetectors: "
                + ", ".join(f"<code>{d}</code>" for d in DETECTOR_SPECS)
            )
        name = args[0].lower()
        if name not in DETECTOR_SPECS:
            return Reply(f"Unknown detector <b>{html.escape(name)}</b>.")
        rows = reference_table(DETECTOR_SPECS[name])
        lines = [f"<b>{name}</b> [{DETECTOR_LEVEL[name]}]\n"]
        for setting, default, bounds, doc in rows:
            lines.append(
                f"<code>{setting}</code> — default <b>{html.escape(default)}</b>, "
                f"allowed {html.escape(bounds)}\n<i>{html.escape(doc)}</i>\n"
            )
        return Reply("\n".join(lines))

    def cmd_reset(self, args: list[str]) -> Reply:
        message = self.ctx.overlay.reset(args[0].lower() if args else None)
        self._persist()
        return Reply(f"✅ {html.escape(message)}", dirty=True)

    def cmd_changes(self, args: list[str]) -> Reply:
        changes = self.ctx.overlay.describe()
        if not changes:
            return Reply(
                "No live changes — everything matches <code>config.yaml</code> as committed."
            )
        lines = ["<b>Changed from config.yaml</b>\n"]
        lines.extend(f"• <code>{html.escape(c)}</code>" for c in changes)
        lines.append("\n<code>/reset</code> to drop all threshold changes.")
        return Reply("\n".join(lines))

    # -- status -----------------------------------------------------------
    def cmd_status(self, args: list[str]) -> Reply:
        cfg = self.ctx.config
        now = self.ctx.now()
        from .. import market_calendar as cal

        running, why = cal.should_run(
            now.astimezone(cal.ET), extended_hours=bool(cfg.run["extended_hours"])
        )
        with State(self.ctx.state_path) as state:
            stats = state.stats()
            counts = state.signal_counts(now, days=14)
            last = state.db.execute(
                "SELECT started_at, alerts, note FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            health = state.db.execute(
                "SELECT key, value FROM counters WHERE key LIKE 'health:%' AND value > 0 "
                "ORDER BY value DESC LIMIT 8"
            ).fetchall()

        enabled = cfg.enabled_detectors()
        lines = [
            "<b>Status</b>\n",
            f"Market: {'🟢 open' if running else '⚪ closed'} — {html.escape(why)}",
            f"Watching <b>{len(cfg.tickers)}</b> ticker(s), "
            f"<b>{len(enabled)}</b> detector(s) on",
            f"Signals in 14 days: <b>{sum(counts.values())}</b>",
        ]
        if last:
            lines.append(
                f"Last run: <code>{html.escape(str(last[0])[:19])}</code> — "
                f"{html.escape(str(last[2] or ''))}"
            )
        else:
            lines.append("Last run: <i>never (no runs recorded yet)</i>")

        warning = _staleness_warning(self._last_run_from(last), now, running)
        if warning:
            lines.append("\n" + warning)

        missing = _missing_credentials(cfg)
        if missing:
            lines.append("\n<b>🔑 Data sources that cannot be reached</b>")
            for name, why in missing:
                lines.append(f"• <code>{name}</code> is not set — {html.escape(why)}")
            lines.append(
                "<i>Those detectors will stay silent. Silence from them is not "
                "an all-clear.</i>"
            )

        if health:
            lines.append("\n<b>🩺 Data source problems</b>")
            for key, value in health:
                pretty = str(key).replace("health:", "")
                lines.append(f"• <code>{html.escape(pretty)}</code> — {value} consecutive")
        else:
            lines.append("\n🩺 All data sources healthy.")

        if self.ctx.canslim is None or self.ctx.canslim.unavailable_reason:
            reason = (
                self.ctx.canslim.unavailable_reason
                if self.ctx.canslim
                else "not configured"
            )
            lines.append(f"\n📄 CAN SLIM: <i>unavailable — {html.escape(str(reason)[:90])}</i>")
        else:
            lines.append("\n📄 CAN SLIM: ready")

        lines.append(
            f"\n<i>state: {stats['seen']} dedup keys, {stats['signals']} signals</i>"
        )
        return Reply(
            "\n".join(lines),
            buttons=[Button("Watchlist", "list"), Button("Levels", "levels")],
        )


HANDLERS = {
    "help": "cmd_help",
    "start": "cmd_help",
    "list": "cmd_list",
    "ls": "cmd_list",
    "watchlist": "cmd_list",
    "add": "cmd_add",
    "remove": "cmd_remove",
    "rm": "cmd_remove",
    "del": "cmd_remove",
    "history": "cmd_history",
    "hist": "cmd_history",
    "grade": "cmd_grade",
    "canslim": "cmd_grade",
    "narrator": "cmd_narrator",
    "levels": "cmd_levels",
    "on": "cmd_on",
    "enable": "cmd_on",
    "off": "cmd_off",
    "disable": "cmd_off",
    "config": "cmd_config",
    "cfg": "cmd_config",
    "set": "cmd_set",
    "run": "cmd_run",
    "explain": "cmd_explain",
    "reset": "cmd_reset",
    "changes": "cmd_changes",
    "status": "cmd_status",
}


def _missing_credentials(cfg: Config) -> list[tuple[str, str]]:
    """Credentials a currently-enabled detector needs but does not have.

    A detector with no key does not fail loudly — it just never fires, which is
    indistinguishable from a quiet market. Naming the gap in `/status` is the
    only place the operator will see it.
    """
    import os

    from ..providers.sec_edgar import is_placeholder_user_agent

    wanted: list[tuple[str, str]] = []
    if cfg.needs("bars"):
        wanted.append(("FMP_API_KEY", "no intraday bars, so no volume anomalies"))
    if cfg.needs("trades") or cfg.needs("flow"):
        wanted.append(("UW_API_KEY", "no dark-pool prints or options flow"))
    if cfg.canslim.get("narrator") == "llm":
        wanted.append(("ANTHROPIC_API_KEY", "scorecards fall back to computed letters"))
    missing = [(name, why) for name, why in wanted if not os.environ.get(name)]

    ua = cfg.providers.sec_user_agent
    if cfg.needs("insider") and (
        not os.environ.get("SEC_USER_AGENT")
        and (not ua or is_placeholder_user_agent(ua))
    ):
        missing.append(
            ("SEC_USER_AGENT", "EDGAR needs a real contact address, so no Form 4 alerts")
        )
    return missing


def _ago(minutes: float) -> str:
    if minutes < 90:
        return f"{minutes:.0f} min ago"
    hours = minutes / 60
    if hours < 36:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f} days ago"


#: How far behind the cron may fall before the bot stops presenting its records
#: as current. The cron ticks every 5 minutes and GitHub's scheduler is often
#: 5-20 minutes late, so this has to tolerate ordinary lateness without going
#: quiet about a cron that has actually stopped.
STALE_RUN_MINUTES = 30


def _staleness_warning(last_run: datetime | None, now: datetime, trading: bool) -> str | None:
    """Warn when what the bot is about to show was written by a dead cron.

    Everything in `/list`, `/history` and `/status` is a record of what the cron
    saw. If the cron stopped an hour ago, an empty watchlist reads as "nothing is
    happening" when it means "nobody is looking" — the exact failure this project
    exists to avoid.
    """
    if last_run is None:
        return (
            "⚠️ <b>No run has ever been recorded.</b> These records are empty "
            "because nothing has polled yet, not because the market is quiet. "
            "Start the cron, or run <code>monitor run</code> once."
        )
    lag = (now - last_run).total_seconds() / 60.0
    if lag <= STALE_RUN_MINUTES:
        return None
    tail = (
        " The market is open, so this means alerts are being missed right now."
        if trading
        else ""
    )
    return (
        f"⚠️ <b>Last poll was {_ago(lag)}</b> — the counts below stop there and "
        f"may be out of date.{tail}"
    )


def _fmt(value: object) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float) and value == int(value):
        return f"{int(value):,}"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, list):
        return ", ".join(map(str, value)) or "—"
    return str(value)


def _closest(name: str) -> str | None:
    import difflib

    matches = difflib.get_close_matches(name, list(HANDLERS), n=1, cutoff=0.6)
    return matches[0] if matches else None
