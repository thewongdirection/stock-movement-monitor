"""Command line interface.

    monitor run                 one polling cycle (what the cron calls)
    monitor validate            strict config check — exit 1 on any problem
    monitor verify              live probe of every provider endpoint
    monitor explain             the threshold reference, defaults and bounds
    monitor test-alert          send a sample alert through Telegram
    monitor telegram-chat-id    look up your chat id during setup
    monitor state               what the state file currently holds
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as config_mod
from . import engine
from .models import Alert, Severity
from .notify.base import ConsoleNotifier
from .notify.telegram import TelegramNotifier
from .params import ConfigError, reference_table
from .providers.base import ProviderError
from .providers.sec_edgar import is_placeholder_user_agent
from .state import State

DEFAULT_CONFIG = "config.yaml"
DEFAULT_STATE = "state/monitor.db"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # requests is chatty at DEBUG and drowns out everything useful.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="monitor", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_config(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="path to config.yaml")
        return p

    run_p = with_config(sub.add_parser("run", help="one polling cycle"))
    run_p.add_argument("-s", "--state", default=DEFAULT_STATE, help="path to the state db")
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="print alerts to stdout instead of sending them, and leave state untouched",
    )
    run_p.add_argument(
        "--force",
        action="store_true",
        help="run session-bound detectors even when the market is closed",
    )
    run_p.set_defaults(handler=cmd_run)

    validate_p = with_config(sub.add_parser("validate", help="strict config check"))
    validate_p.set_defaults(handler=cmd_validate)

    verify_p = with_config(sub.add_parser("verify", help="probe provider endpoints"))
    verify_p.add_argument(
        "--ticker", default="", help="symbol to probe with (default: first configured)"
    )
    verify_p.set_defaults(handler=cmd_verify)

    explain_p = sub.add_parser("explain", help="threshold reference")
    explain_p.add_argument("detector", nargs="?", help="limit to one detector")
    explain_p.add_argument("--markdown", action="store_true", help="emit markdown tables")
    explain_p.set_defaults(handler=cmd_explain)

    test_p = sub.add_parser("test-alert", help="send a sample alert")
    test_p.set_defaults(handler=cmd_test_alert)

    chat_p = sub.add_parser("telegram-chat-id", help="look up your chat id")
    chat_p.set_defaults(handler=cmd_chat_id)

    state_p = sub.add_parser("state", help="inspect the state file")
    state_p.add_argument("-s", "--state", default=DEFAULT_STATE)
    state_p.set_defaults(handler=cmd_state)

    return parser


# --------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config, strict=False)
    for issue in cfg.issues:
        logging.warning("config: %s: %s", issue.path, issue.message)

    notifier = ConsoleNotifier() if args.dry_run else _telegram()
    state_path = ":memory:" if args.dry_run else args.state

    with State(state_path) as state:
        result = engine.run(cfg, state, notifier, force=args.force)

    print(f"\n{result.summary()} · {result.session_note}")
    if not result.ran_session_detectors:
        print("session-bound detectors were skipped; insider filings still checked")
    for note in result.notes:
        print(f"  note: {note}")
    for error in result.errors:
        print(f"  error: {error}", file=sys.stderr)

    # A run that alerted successfully but hit a provider error is still a
    # degraded run, and CI should show it as such.
    return 0 if result.ok else 1


def cmd_validate(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config, strict=True)
    print(f"✓ {args.config} is valid")
    print(f"  tickers ({len(cfg.tickers)}): {', '.join(cfg.tickers)}")
    enabled = cfg.enabled_detectors()
    print(f"  detectors enabled: {', '.join(enabled) if enabled else 'none'}")
    for name in enabled:
        level = config_mod.DETECTOR_LEVEL[name]
        print(f"    [{level}] {name}")
    if cfg.overrides:
        print("  per-ticker overrides:")
        for ticker, per_det in cfg.overrides.items():
            for det, settings in per_det.items():
                pairs = ", ".join(f"{k}={v}" for k, v in settings.items())
                print(f"    {ticker}.{det}: {pairs}")
    missing = _missing_secrets(cfg)
    if missing:
        print("\n  ⚠ environment variables not set (needed at run time):")
        for name, why in missing:
            print(f"    {name} — {why}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config, strict=False)
    ticker = (args.ticker or cfg.tickers[0]).upper()
    print(f"Probing providers with {ticker}\n")
    failures = 0

    if cfg.needs("bars"):
        print("── FMP (intraday bars) ──")
        try:
            provider = engine.Providers(cfg).bars
            bars = provider.intraday_bars(
                ticker, str(cfg.detector("volume_anomaly", ticker)["bar_interval"])
            )
            sessions = sorted({b.ts.date() for b in bars})
            adv = provider.average_daily_volume(bars, 20)
            print(f"  ok — {len(bars)} bars across {len(sessions)} sessions")
            if sessions:
                print(f"     range {sessions[0]} .. {sessions[-1]}")
            print(f"     average daily volume: {adv:,.0f}" if adv else "     ADV: n/a")
            if len(sessions) < 6:
                failures += 1
                print(
                    "  ⚠ fewer than 6 sessions of history — the volume baseline "
                    "needs more. Your FMP plan may cap intraday history."
                )
        except ProviderError as exc:
            failures += 1
            print(f"  FAILED — {exc}")

    if cfg.needs("trades") or cfg.needs("flow"):
        print("\n── Unusual Whales ──")
        print("  (paths are config values; correct any 404 under providers.unusual_whales)")
        try:
            for key, url, outcome in engine.Providers(cfg).uw.probe(ticker):
                mark = "ok  " if outcome.startswith("ok") else "FAIL"
                if not outcome.startswith("ok"):
                    failures += 1
                print(f"  [{mark}] {key}\n         {url}\n         {outcome}")
        except ProviderError as exc:
            failures += 1
            print(f"  FAILED — {exc}")

    if cfg.needs("insider"):
        print("\n── SEC EDGAR (Form 4) ──")
        try:
            sec = engine.Providers(cfg).sec
            cik = sec.cik_for(ticker)
            print(f"  ok — {ticker} maps to CIK {cik}")
            since = (datetime.now(timezone.utc) - timedelta(days=90)).date()
            filings = sec.recent_form4_filings(ticker, since)
            print(f"  ok — {len(filings)} Form 4 filing(s) in the last 90 days")
            if filings:
                txns = sec.fetch_transactions(ticker, filings[0])
                print(
                    f"  ok — parsed {len(txns)} transaction line(s) from "
                    f"{filings[0]['accessionNumber']}"
                )
                for txn in txns[:3]:
                    value = f"${txn.notional:,.0f}" if txn.value_known else "no price stated"
                    print(
                        f"     {txn.transaction_code} {txn.shares:,.0f} sh · {value} "
                        f"· {txn.insider_name}"
                    )
        except ProviderError as exc:
            failures += 1
            print(f"  FAILED — {exc}")

    print("\n── Telegram ──")
    try:
        _telegram()
        print("  ok — token and chat id are set (use `test-alert` to send one)")
    except ValueError as exc:
        failures += 1
        print(f"  FAILED — {exc}")

    print(
        f"\n{'✓ all probes passed' if not failures else f'✗ {failures} probe(s) need attention'}"
    )
    return 0 if not failures else 1


def cmd_explain(args: argparse.Namespace) -> int:
    names = [args.detector] if args.detector else list(config_mod.DETECTOR_SPECS)
    for name in names:
        specs = config_mod.DETECTOR_SPECS.get(name)
        if specs is None:
            print(f"unknown detector {name!r}", file=sys.stderr)
            return 2
        level = config_mod.DETECTOR_LEVEL[name]
        rows = reference_table(specs)
        if args.markdown:
            print(f"\n#### `{name}` — {level}\n")
            print("| Setting | Default | Allowed | What it does |")
            print("|---|---|---|---|")
            for setting, default, bounds, doc in rows:
                print(f"| `{setting}` | `{default}` | {bounds} | {doc} |")
        else:
            print(f"\n=== {name} ({level}) ===")
            for setting, default, bounds, doc in rows:
                print(f"  {setting}")
                print(f"      default: {default}   allowed: {bounds}")
                print(f"      {doc}")
    if not args.detector and args.markdown:
        print("\n#### `block_trades` presets\n")
        print("| Preset | min_shares | min_notional |")
        print("|---|---|---|")
        for preset, (sh, notional) in config_mod.BLOCK_PRESETS.items():
            print(f"| `{preset}` | {sh:,} | ${notional:,.0f} |")
    return 0


def cmd_test_alert(args: argparse.Namespace) -> int:
    notifier = _telegram()
    alert = Alert(
        ticker="TEST",
        detector="volume_anomaly",
        severity=Severity.MEDIUM,
        headline="Sample alert — setup check",
        occurred_at=datetime.now(timezone.utc),
        lines=[
            "RVOL <b>4.2×</b> normal for 10:35 ET · z-score <b>5.1</b>",
            "Volume 1.8M vs 430.0K typical (20-session baseline)",
            "Price ▲ +1.84% to $182.40 · ~$328.32M traded",
            "<i>If you can read this, delivery works.</i>",
        ],
    )
    ok = notifier.send(alert)
    print("✓ sent" if ok else "✗ failed — see the error above")
    return 0 if ok else 1


def cmd_chat_id(args: argparse.Namespace) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set.", file=sys.stderr)
        return 2
    notifier = TelegramNotifier(token, chat_id="0")
    chats = notifier.resolve_chat_id()
    if not chats:
        print(
            "No chats found. Send your bot a message (any text) in Telegram, "
            "then run this again.\n"
            "For a group, add the bot to the group and post a message there."
        )
        return 1
    print("Chats that have messaged this bot:")
    for chat_id, name in chats:
        print(f"  TELEGRAM_CHAT_ID={chat_id}   ({name})")
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    path = Path(args.state)
    if not path.exists():
        print(f"no state file at {path} — the next run will create one")
        return 0
    with State(path) as state:
        stats = state.stats()
    print(f"{path} ({path.stat().st_size:,} bytes)")
    for table, count in stats.items():
        print(f"  {table}: {count:,}")
    return 0


# --------------------------------------------------------------------------
def _telegram() -> TelegramNotifier:
    return TelegramNotifier(
        token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )


def _missing_secrets(cfg: config_mod.Config) -> list[tuple[str, str]]:
    wanted: list[tuple[str, str]] = [
        ("TELEGRAM_BOT_TOKEN", "alert delivery"),
        ("TELEGRAM_CHAT_ID", "alert delivery"),
    ]
    if cfg.needs("bars"):
        wanted.append(("FMP_API_KEY", "intraday bars for the volume detector"))
    if cfg.needs("trades") or cfg.needs("flow"):
        wanted.append(("UW_API_KEY", "dark pool prints and options flow"))
    ua = cfg.providers.sec_user_agent
    if cfg.needs("insider") and (not ua or is_placeholder_user_agent(ua)):
        wanted.append(
            (
                "SEC_USER_AGENT",
                "SEC needs a real contact address; config.yaml still has the "
                "example one",
            )
        )
    return [(name, why) for name, why in wanted if not os.environ.get(name)]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
