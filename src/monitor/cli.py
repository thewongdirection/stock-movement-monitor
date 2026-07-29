"""Command line entry points.

`run` is what the systemd timer invokes. Everything else exists so that the
things which normally go wrong — a wrong key, an expired IBKR session, a
threshold nobody tuned — are discoverable before they turn into silence at
10:05 on a Tuesday.

Exit codes matter here, because systemd is the only thing watching: 0 success,
1 an unhandled failure, 2 configuration that cannot be used, 3 a required data
source was unreachable (only when `health.fail_on_unreachable` is set, so a
flaky afternoon does not trip the restart limiter by default).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .canslim import CanSlim, brief, render
from .canslim import status as canslim_status
from .clock import ET, market_phase, now_et
from .config import SCHEMA, ConfigError, Overlay, load, tunable_paths
from .engine import Engine
from .notify import build as build_notifier
from .notify.base import format_alert, format_issues
from .sources import ENV_VARS, build as build_sources, missing_credentials, write_bars, write_chain
from .sources.base import SourceError
from .store import Store

log = logging.getLogger("monitor")

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_SOURCE = 0, 1, 2, 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monitor",
        description="Watch a handful of stocks for movement that means somebody took a position.",
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--overlay", default="state/runtime.json",
                        help="runtime overrides written by the chat bot")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    run = subs.add_parser("run", help="one scheduled pass over the watchlist")
    run.add_argument("--ticker", action="append", help="override the watchlist")
    run.add_argument("--dry-run", action="store_true",
                     help="print to the console and record nothing as sent")
    run.add_argument("--as-of", help="replay position, ISO timestamp (replay sources only)")

    subs.add_parser("validate", help="check config.yaml and stop")

    verify = subs.add_parser("verify", help="probe every configured source now")
    verify.add_argument("--ticker", help="probe with this ticker instead of the first watched")
    verify.add_argument("--raw", action="store_true",
                        help="dump one raw IBKR snapshot row, to confirm field ids")

    subs.add_parser("console", help="interactive REPL over the bot command set")
    subs.add_parser("bot", help="run the Telegram bot in the foreground")

    grade = subs.add_parser("grade", help="CAN SLIM scorecard for one ticker")
    grade.add_argument("ticker")
    grade.add_argument("--brief", action="store_true",
                       help="print a paste-ready request for the can-slim-grader skill")

    capture = subs.add_parser("capture", help="save current data for replay and testing")
    capture.add_argument("--ticker", action="append", required=False)
    capture.add_argument("--dir", default=None, help="defaults to sources.replay_dir")

    params = subs.add_parser("params", help="list tunable settings and their ranges")
    params.add_argument("filter", nargs="?", default="")

    prune = subs.add_parser("prune", help="drop history past the retention window")
    prune.add_argument("--dry-run", action="store_true")

    return parser


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_run(args) -> int:
    config = _load(args)
    problems = config.validate()
    if problems:
        for problem in problems:
            print(f"✗ {problem}", file=sys.stderr)
        return EXIT_CONFIG
    _warn(config)

    as_of = datetime.fromisoformat(args.as_of).replace(tzinfo=ET) if args.as_of else None
    store = Store(config.get("state.path"))
    run_id = store.start_run()
    try:
        notifier, as_html = build_notifier(config, force_console=args.dry_run)
        with build_sources(config, as_of=as_of) as sources:
            engine = Engine(
                config, store, sources,
                canslim=CanSlim(config, store, fmp=sources.fundamentals),
                now=as_of,
            )
            result = engine.run(args.ticker)

        delivered = _deliver(result, notifier, as_html, config, store, args.dry_run)

        for note in result.health.notes:
            log.info("%s", note)
        store.finish_run(
            run_id, scanned=result.scanned, alerts=delivered,
            issues=len(result.health.issues), ok=result.health.ok,
            note=result.health.summary(),
        )

        print(
            f"{result.scanned} scanned · {result.generated} generated · "
            f"{delivered} sent · {result.duplicates} already seen · "
            f"{len(result.health.issues)} source issues"
            + (f" · {result.capped} above the per-run cap" if result.capped else "")
        )
        if result.health.issues and config.get("health.fail_on_unreachable"):
            return EXIT_SOURCE
        return EXIT_OK
    finally:
        store.close()


def _deliver(result, notifier, as_html, config, store, dry_run: bool) -> int:
    """Send, then mark. In that order, so a failed send is retried next run."""
    sent = 0
    if result.health.issues and config.get("notify.include_source_issues"):
        notifier.send(format_issues(result.health.issues, as_html=as_html))

    for alert in result.alerts:
        text = format_alert(alert, as_html=as_html,
                            canslim=result.canslim.get(alert.ticker))
        ok = (notifier.send(text, alert.severity)
              if _accepts_severity(notifier) else notifier.send(text))
        if not ok:
            log.error("delivery failed for %s — will retry next run", alert.headline)
            continue
        sent += 1
        if not dry_run:
            store.mark_sent(alert)
    return sent


def _accepts_severity(notifier) -> bool:
    import inspect
    try:
        return "severity" in inspect.signature(notifier.send).parameters
    except (TypeError, ValueError):
        return False


def cmd_validate(args) -> int:
    config = _load(args)
    _warn(config)
    problems = config.validate()
    for problem in problems:
        print(f"✗ {problem}")
    missing = missing_credentials(config)
    for name in missing:
        print(f"✗ {name} is not set — {ENV_VARS.get(name, '')}")

    if problems or missing:
        return EXIT_CONFIG
    print(f"✓ config is usable — watching {', '.join(config.watchlist)}")
    print(f"  signals: {', '.join(_enabled(config))}")
    print(f"  sources: bars={config.get('sources.bars')} "
          f"options={config.get('sources.options')} "
          f"insider={config.get('sources.insider')} "
          f"trades={config.get('sources.trades')}")
    print(f"  {canslim_status()}")
    return EXIT_OK


def cmd_verify(args) -> int:
    config = _load(args)
    _warn(config)
    ticker = (args.ticker or (config.watchlist[0] if config.watchlist else None))
    if ticker is None:
        print("✗ nothing to probe with — the watchlist is empty", file=sys.stderr)
        return EXIT_CONFIG
    ticker = ticker.upper()

    print(f"Probing with {ticker} at {now_et():%Y-%m-%d %H:%M %Z} (market {market_phase()})\n")
    for name, why in ENV_VARS.items():
        state = "set" if os.environ.get(name) else "not set"
        print(f"  {name:22} {state:8} {why}")
    print()

    failures = 0
    with build_sources(config) as sources:
        for issue in sources.issues:
            print(f"  {issue.line()}")
            failures += 1

        probes = [
            ("bars", sources.bars, lambda s: _describe_bars(s, ticker, config)),
            ("options", sources.options, lambda s: _describe_chain(s, ticker, config, args.raw)),
            ("insider", sources.insider, lambda s: _describe_filings(s, ticker, config)),
            ("trades", sources.trades,
             lambda s: f"{len(s.trades(ticker, now_et().replace(hour=0)))} prints today"),
            ("fundamentals", sources.fundamentals,
             lambda s: f"quote ${s.quote(ticker).get('price')}"),
        ]
        for role, source, probe in probes:
            if source is None:
                print(f"  — {role:13} not configured")
                continue
            try:
                print(f"  ✓ {role:13} {source.name}: {probe(source)}")
            except SourceError as exc:
                print(f"  ✗ {role:13} {source.name}: {exc.kind} — {exc.detail}")
                failures += 1
            except Exception as exc:                # noqa: BLE001
                print(f"  ✗ {role:13} {source.name}: {type(exc).__name__}: {exc}")
                failures += 1

    if config.get("notify.channel") == "telegram":
        failures += _verify_telegram()

    print()
    print(f"  {canslim_status()}")
    return EXIT_SOURCE if failures else EXIT_OK


def _describe_bars(source, ticker, config) -> str:
    bars = source.bars(ticker, config.get("poll.bar_minutes"),
                       config.get("poll.baseline_sessions"))
    newest = max(bar.ts for bar in bars)
    return (f"{len(bars)} bars, newest {newest:%Y-%m-%d %H:%M %Z}, "
            f"{len({bar.ts.date() for bar in bars})} sessions")


def _describe_chain(source, ticker, config, raw: bool) -> str:
    chain = source.chain(ticker, config.get("signals.open_interest.max_days_to_expiry"))
    total = sum(contract.open_interest for contract in chain.contracts)
    detail = (f"{len(chain.contracts)} contracts as of {chain.as_of}, "
              f"{total:,} total open interest")
    if raw and chain.contracts:
        sample = chain.contracts[0]
        detail += (f"\n      sample: {sample.label()} oi={sample.open_interest} "
                   f"vol={sample.volume} (field {config.get('ibkr.oi_field')})")
    return detail


def _describe_filings(source, ticker, config) -> str:
    from datetime import timedelta
    since = now_et().date() - timedelta(days=config.get("signals.insider.lookback_days"))
    filings = source.filings(ticker, since)
    return f"{len(filings)} Form 4 lines since {since}"


def _verify_telegram() -> int:
    from .notify.telegram import TelegramNotifier
    try:
        notifier = TelegramNotifier(
            os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        )
        info = notifier.verify()
        chat = info["chat"]
        print(f"  ✓ {'telegram':13} @{info['bot'].get('username')} → "
              f"{chat.get('first_name') or chat.get('title')} ({chat.get('id')})")
        return 0
    except Exception as exc:                        # noqa: BLE001
        print(f"  ✗ {'telegram':13} {exc}")
        return 1


def cmd_console(args) -> int:
    from .bot import Commands, repl
    return repl(Commands(args.config, args.overlay))


def cmd_bot(args) -> int:
    from .bot import Commands, TelegramBot
    bot = TelegramBot(
        os.environ.get("TELEGRAM_BOT_TOKEN"),
        os.environ.get("TELEGRAM_CHAT_ID"),
        Commands(args.config, args.overlay),
    )
    log.info("bot started, polling for commands")
    return bot.run_forever()


def cmd_grade(args) -> int:
    config = _load(args)
    store = Store(config.get("state.path"))
    try:
        with build_sources(config) as sources:
            if sources.fundamentals is None:
                print("✗ CAN SLIM grading needs FMP_API_KEY", file=sys.stderr)
                return EXIT_CONFIG
            card = CanSlim(config, store, fmp=sources.fundamentals).card(
                args.ticker.upper(), fresh=True
            )
        if card is None:
            print("✗ CAN SLIM is disabled in config", file=sys.stderr)
            return EXIT_CONFIG
        print(brief(card) if args.brief else render(card))
        return EXIT_OK
    finally:
        store.close()


def cmd_capture(args) -> int:
    """Save today's data so it can be replayed, tested and swept against."""
    config = _load(args)
    target = Path(args.dir or config.get("sources.replay_dir"))
    tickers = [t.upper() for t in (args.ticker or config.watchlist)]
    if not tickers:
        print("✗ nothing to capture", file=sys.stderr)
        return EXIT_CONFIG

    saved = 0
    with build_sources(config) as sources:
        for ticker in tickers:
            if sources.bars is not None:
                try:
                    bars = sources.bars.bars(
                        ticker, config.get("poll.bar_minutes"),
                        config.get("poll.baseline_sessions"),
                    )
                    write_bars(target / "bars" / f"{ticker}.json",
                               ticker, config.get("poll.bar_minutes"), bars)
                    print(f"  ✓ {ticker} bars: {len(bars)}")
                    saved += 1
                except SourceError as exc:
                    print(f"  ✗ {ticker} bars: {exc.detail}")
            if sources.options is not None:
                try:
                    chain = sources.options.chain(
                        ticker, config.get("signals.open_interest.max_days_to_expiry")
                    )
                    write_chain(target / "options" / f"{ticker}.json", chain)
                    print(f"  ✓ {ticker} chain: {len(chain.contracts)} contracts "
                          f"as of {chain.as_of}")
                    saved += 1
                except SourceError as exc:
                    print(f"  ✗ {ticker} options: {exc.detail}")

    print(f"\nCaptured {saved} files into {target}")
    return EXIT_OK if saved else EXIT_SOURCE


def cmd_params(args) -> int:
    needle = args.filter.lower()
    shown = 0
    for path in tunable_paths():
        if needle and needle not in path.lower():
            continue
        print(f"{path}\n    {SCHEMA[path].describe()}")
        shown += 1
    if not shown:
        print(f"No tunable settings match '{args.filter}'.")
    return EXIT_OK


def cmd_prune(args) -> int:
    config = _load(args)
    store = Store(config.get("state.path"))
    try:
        print("Before:", store.stats())
        if args.dry_run:
            print("(dry run — nothing deleted)")
            return EXIT_OK
        removed = store.prune(
            config.get("state.retention_days"),
            config.get("signals.insider.cluster_window_days"),
        )
        print("Removed:", removed)
        print("After: ", store.stats())
        return EXIT_OK
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #

def _load(args):
    return load(args.config, overlay=args.overlay)


def _warn(config) -> None:
    for warning in config.warnings:
        print(f"⚠ {warning}", file=sys.stderr)


def _enabled(config) -> list[str]:
    return [
        name for name in ("open_interest", "insider", "blocks", "volume")
        if config.get(f"signals.{name}.enabled")
    ] or ["none"]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    handler = globals()[f"cmd_{args.command}"]
    try:
        return handler(args)
    except ConfigError as exc:
        print(f"✗ configuration: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except SourceError as exc:
        print(f"✗ data source: {exc}", file=sys.stderr)
        return EXIT_SOURCE
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
