"""Replay bars from a captured file instead of calling a provider.

Why this exists: thresholds are the hard part of this project. `rvol_threshold:
2.5` is a guess until you have watched it against a real session, and you cannot
iterate on a guess at one cron tick every five minutes. A snapshot lets you run
the real detectors over a real session as many times as you like, change a
number, and run it again — with no API budget and no key.

It is also how the pipeline can be exercised in CI, where there are no
credentials at all.

**This is deliberately loud about being stale.** The rest of this codebase works
hard never to serve old data quietly; a file of yesterday's bars is old data by
construction, so the provider reports its own age, the run footer says a replay
happened, and `monitor validate` refuses to let it pass unremarked. The failure
this guards against is someone leaving `bars: snapshot` in a committed config and
believing they are being alerted on a live market.

File format — one JSON object, timestamps ISO-8601 with an offset:

    {
      "captured_at": "2026-07-28T02:51:12+00:00",
      "source": "IBKR Client Portal, 15-minute delayed",
      "interval": "15min",
      "bars": {
        "NVDA": [
          {"ts": "2026-07-27T13:30:00+00:00",
           "open": 196.8, "high": 197.95, "low": 196.31,
           "close": 196.52, "volume": 6645213}
        ]
      }
    }

`monitor capture` writes one from whatever live provider is configured.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..models import Bar
from .base import ProviderError, SetupError

#: Bars are normalised to US/Eastern on the way in, exactly as the FMP and IBKR
#: providers do. Two reasons it has to happen here rather than being left as
#: whatever the file said: the detector buckets bars by (hour, minute) to build a
#: same-time-of-day baseline, and the alert prints that clock time as ET. A file
#: of UTC timestamps would label an 11:00 ET bar "15:00 ET" and — worse — would
#: bucket bars into different slots either side of a DST change, quietly
#: comparing 09:30 against 10:30.
ET = ZoneInfo("America/New_York")

log = logging.getLogger(__name__)

#: Keys accepted for each OHLCV field. Short forms are allowed because most
#: market APIs — and anyone hand-assembling a fixture — use them.
FIELD_ALIASES = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c"),
    "volume": ("volume", "v"),
}


class SnapshotBars:
    """A bars provider backed by a file. Same interface as FMP and IBKR."""

    def __init__(self, path: str | Path, as_of: datetime | None = None):
        #: The run clock. Bars after it are hidden, because a replay that can see
        #: the future is not a replay — it is lookahead bias, and it would make
        #: every threshold you tuned against it look better than it is.
        self.as_of = as_of
        self.path = Path(path)
        if not self.path.exists():
            raise SetupError(
                f"snapshot file not found: {self.path}. Write one with "
                "`monitor capture`, or point providers.snapshot.path at an "
                "existing one."
            )
        try:
            payload = json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            raise SetupError(f"snapshot file {self.path} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SetupError(f"snapshot file {self.path} must contain a JSON object")

        self.captured_at = _read_time(payload.get("captured_at"))
        self.source = str(payload.get("source") or "unspecified")
        self.interval = str(payload.get("interval") or "")
        raw = payload.get("bars")
        if not isinstance(raw, dict) or not raw:
            raise SetupError(
                f"snapshot file {self.path} has no `bars` object mapping tickers to rows"
            )
        self._bars: dict[str, list[Bar]] = {}
        for symbol, rows in raw.items():
            parsed = _parse_rows(symbol, rows)
            if parsed:
                self._bars[symbol.upper()] = parsed

        if not self._bars:
            raise SetupError(f"snapshot file {self.path} contained no usable bars")

    # -- provider interface ------------------------------------------------
    def visible(self, symbol: str) -> list[Bar] | None:
        """Bars for a symbol, truncated at the run clock."""
        bars = self._bars.get(symbol.upper())
        if bars is None:
            return None
        if self.as_of is None:
            return list(bars)
        return [bar for bar in bars if bar.ts <= self.as_of]

    def intraday_bars(
        self, symbol: str, interval: str, lookback_days: int = 45
    ) -> list[Bar]:
        bars = self.visible(symbol)
        if bars is None:
            raise ProviderError(
                f"{symbol} is not in the snapshot ({self.path.name}); it holds "
                f"{', '.join(sorted(self._bars))}. Re-capture with {symbol} on the "
                "watchlist."
            )
        # The interval is a property of the capture, not a request parameter. Say
        # so rather than silently returning bars of the wrong width — a 15-minute
        # bar judged against a 5-minute baseline is a fabricated anomaly.
        if self.interval and interval and self.interval != interval:
            raise ProviderError(
                f"snapshot holds {self.interval} bars but volume_anomaly is set to "
                f"{interval}. Set detectors.volume_anomaly.bar_interval to "
                f"{self.interval}, or re-capture at {interval}."
            )
        if not bars:
            raise ProviderError(
                f"{symbol} has no bars at or before the run clock "
                f"({self.as_of.isoformat() if self.as_of else 'unset'}). The snapshot "
                f"starts at {self._bars[symbol.upper()][0].ts.isoformat()} — pass a "
                "later --as-of."
            )
        return bars

    def average_daily_volume(
        self, bars: list[Bar], sessions: int, now: datetime | None = None
    ) -> float | None:
        """Same rule as the live providers: whole sessions only, today excluded."""
        by_day: dict[Any, int] = {}
        for bar in bars:
            by_day[bar.ts.date()] = by_day.get(bar.ts.date(), 0) + bar.volume
        if not by_day:
            return None
        reference = now or self.captured_at or datetime.now(timezone.utc)
        today = reference.date()
        complete = [v for d, v in sorted(by_day.items()) if d != today]
        if not complete:
            return None
        window = complete[-sessions:]
        return sum(window) / len(window)

    def close(self) -> None:
        return None

    # -- provenance --------------------------------------------------------
    @property
    def tickers(self) -> list[str]:
        return sorted(self._bars)

    def newest_bar(self) -> datetime | None:
        """Newest bar the run clock can see — not the newest in the file."""
        newest = [
            visible[-1].ts
            for symbol in self._bars
            if (visible := self.visible(symbol))
        ]
        return max(newest, default=None)

    def hidden_bars(self) -> int:
        """How many bars the run clock is holding back. Reported, not silent."""
        if self.as_of is None:
            return 0
        return sum(
            1 for bars in self._bars.values() for bar in bars if bar.ts > self.as_of
        )

    def age_hours(self, now: datetime) -> float | None:
        newest = self.newest_bar()
        if newest is None:
            return None
        return max(0.0, (now - newest).total_seconds() / 3600.0)

    def provenance(self, now: datetime) -> str:
        """One line for the run footer. Never let a replay pass for a live run."""
        age = self.age_hours(now)
        when = (
            f"{age:.1f}h" if age is not None and age < 72 else
            f"{(age or 0) / 24:.1f} days"
        )
        hidden = self.hidden_bars()
        tail = (
            f" {hidden:,} later bar(s) withheld so the replay cannot see past the "
            "run clock."
            if hidden
            else ""
        )
        return (
            f"REPLAY — bars came from {self.path.name} ({self.source}), newest visible "
            f"bar {when} before the run clock. No live market data was fetched.{tail}"
        )


def _parse_rows(symbol: str, rows: Any) -> list[Bar]:
    if not isinstance(rows, list):
        log.warning("snapshot entry for %s is not a list; skipped", symbol)
        return []
    out: list[Bar] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = _read_time(row.get("ts") or row.get("time") or row.get("date"))
        if ts is None:
            continue
        try:
            values = {
                name: float(_first(row, aliases))
                for name, aliases in FIELD_ALIASES.items()
            }
        except (TypeError, ValueError):
            continue
        out.append(
            Bar(
                ts=ts.astimezone(ET),
                open=values["open"],
                high=values["high"],
                low=values["low"],
                close=values["close"],
                volume=int(values["volume"]),
            )
        )
    out.sort(key=lambda b: b.ts)
    return out


def _first(row: dict, aliases: tuple[str, ...]) -> Any:
    for key in aliases:
        if key in row and row[key] is not None:
            return row[key]
    raise ValueError(f"missing any of {aliases}")


def _read_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def write_snapshot(
    path: str | Path,
    bars: dict[str, list[Bar]],
    *,
    interval: str,
    source: str,
    captured_at: datetime,
) -> Path:
    """Serialise captured bars into the format `SnapshotBars` reads."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "captured_at": captured_at.isoformat(),
                "source": source,
                "interval": interval,
                "bars": {
                    symbol: [
                        {
                            "ts": bar.ts.isoformat(),
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                        }
                        for bar in rows
                    ]
                    for symbol, rows in sorted(bars.items())
                },
            },
            indent=2,
        )
    )
    return out
