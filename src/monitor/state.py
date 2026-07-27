"""Durable state: what we've already alerted on, and how far we've read.

Three concerns, one SQLite file:

* **seen**       — dedup keys, so a replayed window never notifies twice.
* **watermarks** — the newest event timestamp consumed per detector+ticker.
* **cooldowns**  — per-ticker silence windows after a fire.

On GitHub Actions the file lives in the Actions cache. Cache loss is expected
and handled: with no watermark a detector falls back to
``run.cold_start_lookback_minutes`` instead of replaying the whole day.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    dedup_id   TEXT PRIMARY KEY,
    detector   TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS seen_created_at ON seen (created_at);

CREATE TABLE IF NOT EXISTS watermarks (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cooldowns (
    key   TEXT PRIMARY KEY,
    until TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    started_at TEXT NOT NULL,
    alerts     INTEGER NOT NULL,
    note       TEXT
);
"""


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)
        self.db.commit()
        # Watermarks and cooldowns are staged rather than written immediately.
        # If delivery fails we must not record progress past an alert nobody
        # received, so these are only flushed for detector/ticker pairs whose
        # alerts all went out. See `flush_progress`.
        self._pending_watermarks: dict[tuple[str, str], datetime] = {}
        self._pending_cooldowns: dict[tuple[str, str], datetime] = {}

    # -- dedup ------------------------------------------------------------
    def is_new(self, dedup_id: str) -> bool:
        cur = self.db.execute("SELECT 1 FROM seen WHERE dedup_id = ?", (dedup_id,))
        return cur.fetchone() is None

    def mark_seen(self, dedup_id: str, detector: str, ticker: str, now: datetime) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO seen (dedup_id, detector, ticker, created_at) "
            "VALUES (?, ?, ?, ?)",
            (dedup_id, detector, ticker, _iso(now)),
        )

    # -- watermarks -------------------------------------------------------
    def watermark(self, detector: str, ticker: str) -> datetime | None:
        cur = self.db.execute(
            "SELECT value FROM watermarks WHERE key = ?", (f"{detector}:{ticker}",)
        )
        row = cur.fetchone()
        return _parse(row[0]) if row else None

    def set_watermark(self, detector: str, ticker: str, moment: datetime) -> None:
        """Stage a watermark advance; `flush_progress` decides if it sticks."""
        key = (detector, ticker)
        staged = self._pending_watermarks.get(key)
        if staged is None or moment > staged:
            self._pending_watermarks[key] = moment

    def _write_watermark(self, detector: str, ticker: str, moment: datetime) -> None:
        existing = self.watermark(detector, ticker)
        if existing and existing >= moment:
            return
        self.db.execute(
            "INSERT INTO watermarks (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"{detector}:{ticker}", _iso(moment)),
        )

    def flush_progress(self, skip: set[tuple[str, str]] | None = None) -> None:
        """Persist staged watermarks and cooldowns, except for failed pairs.

        A detector/ticker pair in `skip` had at least one alert that could not
        be delivered. Leaving its watermark where it was means the next run
        finds the same event again — the dedup table stops a duplicate once
        delivery recovers.
        """
        skip = skip or set()
        for key, moment in self._pending_watermarks.items():
            if key not in skip:
                self._write_watermark(key[0], key[1], moment)
        for key, until in self._pending_cooldowns.items():
            if key not in skip:
                self.db.execute(
                    "INSERT INTO cooldowns (key, until) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET until = excluded.until",
                    (f"{key[0]}:{key[1]}", _iso(until)),
                )
        self._pending_watermarks.clear()
        self._pending_cooldowns.clear()

    def since(
        self, detector: str, ticker: str, now: datetime, cold_start_minutes: int
    ) -> tuple[datetime, bool]:
        """The cutoff for 'new' events, and whether this is a cold start."""
        mark = self.watermark(detector, ticker)
        if mark is None:
            return now - timedelta(minutes=cold_start_minutes), True
        floor = now - timedelta(days=7)
        return max(mark, floor), False

    # -- cooldowns --------------------------------------------------------
    def in_cooldown(self, detector: str, ticker: str, now: datetime) -> bool:
        cur = self.db.execute(
            "SELECT until FROM cooldowns WHERE key = ?", (f"{detector}:{ticker}",)
        )
        row = cur.fetchone()
        return bool(row) and _parse(row[0]) > now

    def start_cooldown(
        self, detector: str, ticker: str, now: datetime, minutes: int
    ) -> None:
        """Stage a cooldown; `flush_progress` decides if it sticks."""
        if minutes <= 0:
            return
        self._pending_cooldowns[(detector, ticker)] = now + timedelta(minutes=minutes)

    # -- insider cluster support -----------------------------------------
    def recent_insider_buyers(self, ticker: str, now: datetime, days: int) -> int:
        """Distinct insiders we've recorded buying this name inside the window."""
        cutoff = _iso(now - timedelta(days=days))
        cur = self.db.execute(
            "SELECT COUNT(DISTINCT dedup_id) FROM seen "
            "WHERE ticker = ? AND detector = ? AND created_at >= ?",
            (ticker, "insider_buy_party", cutoff),
        )
        return int(cur.fetchone()[0])

    def record_insider_buyer(self, ticker: str, insider: str, now: datetime) -> None:
        import hashlib

        key = hashlib.sha1(f"{ticker}|{insider.lower()}".encode()).hexdigest()[:20]
        self.mark_seen(key, "insider_buy_party", ticker, now)

    # -- housekeeping -----------------------------------------------------
    def record_run(self, started_at: datetime, alerts: int, note: str = "") -> None:
        self.db.execute(
            "INSERT INTO runs (started_at, alerts, note) VALUES (?, ?, ?)",
            (_iso(started_at), alerts, note),
        )

    def prune(self, now: datetime, retention_days: int) -> None:
        cutoff = _iso(now - timedelta(days=retention_days))
        self.db.execute("DELETE FROM seen WHERE created_at < ?", (cutoff,))
        self.db.execute("DELETE FROM cooldowns WHERE until < ?", (_iso(now),))
        self.db.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))

    def stats(self) -> dict[str, int]:
        out = {}
        for table in ("seen", "watermarks", "cooldowns", "runs"):
            out[table] = int(
                self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        return out

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        with closing(self.db):
            self.db.commit()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
