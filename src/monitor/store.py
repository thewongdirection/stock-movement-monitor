"""Durable state: what was already sent, what was last seen, and yesterday's OI.

This is the only component that remembers anything between runs, which makes it
the only place where a bug is invisible until weeks later. Three of its jobs are
load-bearing:

**Dedup.** The monitor re-reads the same bar on the next poll. Without a record
of what was already sent, an interesting 11:00 bar alerts again at 12:00, 13:00
and 14:00 until the market closes.

**Open-interest snapshots.** OI change is a difference between two days, and the
"two days ago" half has to come from somewhere. `OptionChain.previous` is built
from this table; without it the headline signal cannot fire at all.

**Insider cluster history.** Two officers buying three weeks apart is a cluster,
and neither poll on its own can see it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import OptionContract

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Alerts already delivered. Keyed by Alert.dedup_key.
CREATE TABLE IF NOT EXISTS sent (
    dedup_key   TEXT PRIMARY KEY,
    ticker      TEXT NOT NULL,
    signal      TEXT NOT NULL,
    severity    TEXT NOT NULL,
    headline    TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    sent_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sent_by_time ON sent (sent_at);
CREATE INDEX IF NOT EXISTS sent_by_ticker ON sent (ticker, sent_at);

-- Arbitrary "furthest point processed" markers, e.g. the newest Form 4
-- accession already handled for a ticker.
CREATE TABLE IF NOT EXISTS watermark (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- End-of-day open interest per contract. The 'previous' half of every OI diff.
CREATE TABLE IF NOT EXISTS oi_snapshot (
    contract_key  TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    expiry        TEXT NOT NULL,
    strike        REAL NOT NULL,
    right         TEXT NOT NULL,
    open_interest INTEGER NOT NULL,
    volume        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (contract_key, as_of)
);
CREATE INDEX IF NOT EXISTS oi_by_ticker ON oi_snapshot (ticker, as_of);

-- Distinct insiders who bought, for cluster detection across polls.
CREATE TABLE IF NOT EXISTS insider_buyer (
    ticker     TEXT NOT NULL,
    insider    TEXT NOT NULL,
    traded_on  TEXT NOT NULL,
    value      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (ticker, insider)
);
CREATE INDEX IF NOT EXISTS insider_by_date ON insider_buyer (ticker, traded_on);

-- Last bar timestamp observed per ticker, for frozen-feed detection.
CREATE TABLE IF NOT EXISTS feed_seen (
    ticker      TEXT PRIMARY KEY,
    last_bar_ts TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- One row per run, so /status can answer "is this thing alive".
CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    scanned     INTEGER NOT NULL DEFAULT 0,
    alerts      INTEGER NOT NULL DEFAULT 0,
    issues      INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS run_by_time ON run_log (started_at);

-- CAN SLIM grades. Fundamentals move quarterly; refetching hourly is waste.
CREATE TABLE IF NOT EXISTS canslim_cache (
    ticker    TEXT PRIMARY KEY,
    payload   TEXT NOT NULL,
    cached_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: str) -> datetime:
    parsed = datetime.fromisoformat(ts)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class RunSummary:
    started_at: datetime
    finished_at: datetime | None
    scanned: int
    alerts: int
    issues: int
    ok: bool
    note: str | None


class Store:
    """A thin, synchronous SQLite wrapper. One process, one connection.

    WAL is on so the chat bot can read `/status` while a scheduled run is
    writing. Both are still single-writer — the timer and the bot never write
    concurrently in practice, and SQLite's lock would serialise them if they did.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_DDL)
        self._set_meta("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- dedup --------------------------------------------------------------
    def already_sent(self, dedup_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sent WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        return row is not None

    def mark_sent(self, alert: Any) -> bool:
        """Record an alert as delivered. False if it was already there.

        The INSERT itself is the claim, so two runs racing cannot both send.
        """
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO sent "
                "(dedup_key, ticker, signal, severity, headline, occurred_at, sent_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    alert.dedup_key,
                    alert.ticker,
                    alert.signal,
                    alert.severity.value,
                    alert.headline,
                    alert.occurred_at.isoformat(),
                    _now(),
                ),
            )
        return cursor.rowcount > 0

    def recent_alerts(self, limit: int = 20, ticker: str | None = None) -> list[sqlite3.Row]:
        if ticker:
            return self.conn.execute(
                "SELECT * FROM sent WHERE ticker = ? ORDER BY sent_at DESC LIMIT ?",
                (ticker.upper(), limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM sent ORDER BY sent_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def alert_counts(self, since_days: int = 7) -> dict[str, int]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        rows = self.conn.execute(
            "SELECT signal, COUNT(*) AS n FROM sent WHERE sent_at >= ? GROUP BY signal",
            (cutoff,),
        ).fetchall()
        return {row["signal"]: row["n"] for row in rows}

    # -- watermarks ---------------------------------------------------------
    def watermark(self, name: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM watermark WHERE name = ?", (name,)
        ).fetchone()
        return row["value"] if row else None

    def set_watermark(self, name: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO watermark (name, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (name, value, _now()),
            )

    # -- open interest ------------------------------------------------------
    def save_oi_snapshot(self, ticker: str, as_of: str, contracts: list[OptionContract]) -> int:
        """Persist today's chain so tomorrow's run has something to diff against."""
        rows = [
            (c.key, as_of, ticker.upper(), c.expiry, float(c.strike),
             c.right, int(c.open_interest), int(c.volume))
            for c in contracts
        ]
        with self._tx() as conn:
            conn.executemany(
                "INSERT INTO oi_snapshot "
                "(contract_key, as_of, ticker, expiry, strike, right, open_interest, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(contract_key, as_of) DO UPDATE SET "
                "open_interest = excluded.open_interest, volume = excluded.volume",
                rows,
            )
        return len(rows)

    def previous_oi(self, ticker: str, before: str) -> tuple[dict[str, int], str | None]:
        """The most recent stored chain strictly before `before`.

        Returns the OI map and the date it came from. The date matters: a diff
        against a snapshot from six sessions ago is not an overnight change, and
        the caller needs to be able to say so rather than quietly overstating it.
        """
        row = self.conn.execute(
            "SELECT MAX(as_of) AS d FROM oi_snapshot WHERE ticker = ? AND as_of < ?",
            (ticker.upper(), before),
        ).fetchone()
        as_of = row["d"] if row else None
        if not as_of:
            return {}, None
        rows = self.conn.execute(
            "SELECT contract_key, open_interest FROM oi_snapshot "
            "WHERE ticker = ? AND as_of = ?",
            (ticker.upper(), as_of),
        ).fetchall()
        return {r["contract_key"]: r["open_interest"] for r in rows}, as_of

    def oi_snapshot_dates(self, ticker: str, limit: int = 10) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT as_of FROM oi_snapshot WHERE ticker = ? "
            "ORDER BY as_of DESC LIMIT ?",
            (ticker.upper(), limit),
        ).fetchall()
        return [r["as_of"] for r in rows]

    # -- insider clusters ---------------------------------------------------
    def record_insider_buyer(self, ticker: str, insider: str, traded_on: str, value: float) -> None:
        """Remember that a person bought, keeping the *most recent* purchase date.

        An upsert, not INSERT OR IGNORE. A repeat buyer whose row keeps its
        first-ever date drifts out of the cluster window and stops counting,
        which reads as "no cluster" exactly when the cluster is strongest.
        """
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO insider_buyer (ticker, insider, traded_on, value) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(ticker, insider) DO UPDATE SET "
                "traded_on = MAX(insider_buyer.traded_on, excluded.traded_on), "
                "value = excluded.value",
                (ticker.upper(), insider, traded_on, float(value)),
            )

    def insider_buyers_since(self, ticker: str, since: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT insider, traded_on, value FROM insider_buyer "
            "WHERE ticker = ? AND traded_on >= ? ORDER BY traded_on DESC",
            (ticker.upper(), since),
        ).fetchall()

    # -- feed health --------------------------------------------------------
    def note_feed(self, ticker: str, last_bar_ts: datetime) -> str | None:
        """Record the newest bar seen. Returns the previous value, if any."""
        row = self.conn.execute(
            "SELECT last_bar_ts FROM feed_seen WHERE ticker = ?", (ticker.upper(),)
        ).fetchone()
        previous = row["last_bar_ts"] if row else None
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO feed_seen (ticker, last_bar_ts, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "last_bar_ts = excluded.last_bar_ts, updated_at = excluded.updated_at",
                (ticker.upper(), last_bar_ts.isoformat(), _now()),
            )
        return previous

    def feed_state(self) -> dict[str, tuple[str, str]]:
        rows = self.conn.execute("SELECT * FROM feed_seen").fetchall()
        return {r["ticker"]: (r["last_bar_ts"], r["updated_at"]) for r in rows}

    # -- run log ------------------------------------------------------------
    def start_run(self) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO run_log (started_at) VALUES (?)", (_now(),)
            )
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, *, scanned: int, alerts: int,
                   issues: int, ok: bool, note: str | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE run_log SET finished_at = ?, scanned = ?, alerts = ?, "
                "issues = ?, ok = ?, note = ? WHERE id = ?",
                (_now(), scanned, alerts, issues, int(ok), note, run_id),
            )

    def last_runs(self, limit: int = 5) -> list[RunSummary]:
        rows = self.conn.execute(
            "SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            RunSummary(
                started_at=_parse(r["started_at"]),
                finished_at=_parse(r["finished_at"]) if r["finished_at"] else None,
                scanned=r["scanned"],
                alerts=r["alerts"],
                issues=r["issues"],
                ok=bool(r["ok"]),
                note=r["note"],
            )
            for r in rows
        ]

    # -- CAN SLIM cache -----------------------------------------------------
    def cache_canslim(self, ticker: str, payload: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO canslim_cache (ticker, payload, cached_at) VALUES (?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET payload = excluded.payload, "
                "cached_at = excluded.cached_at",
                (ticker.upper(), json.dumps(payload), _now()),
            )

    def read_canslim(self, ticker: str, max_age_hours: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload, cached_at FROM canslim_cache WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchone()
        if not row:
            return None
        age = datetime.now(timezone.utc) - _parse(row["cached_at"])
        if age > timedelta(hours=max_age_hours):
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            return None

    # -- housekeeping -------------------------------------------------------
    def prune(self, retention_days: int, cluster_window_days: int = 30) -> dict[str, int]:
        """Drop history the signals can no longer reach.

        `insider_buyer` is pruned on its own, longer clock. Cluster detection
        looks back `cluster_window_days`, and with the shipped defaults that
        window lands exactly on the retention boundary — a shared cutoff would
        delete the rows a cluster is about to be assembled from.
        """
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=retention_days)).isoformat()
        cluster_cutoff = (now - timedelta(days=max(retention_days, cluster_window_days) + 1)).date().isoformat()
        oi_cutoff = (now - timedelta(days=max(retention_days, 10))).date().isoformat()

        removed: dict[str, int] = {}
        with self._tx() as conn:
            removed["sent"] = conn.execute(
                "DELETE FROM sent WHERE sent_at < ?", (cutoff,)
            ).rowcount
            removed["oi_snapshot"] = conn.execute(
                "DELETE FROM oi_snapshot WHERE as_of < ?", (oi_cutoff,)
            ).rowcount
            removed["insider_buyer"] = conn.execute(
                "DELETE FROM insider_buyer WHERE traded_on < ?", (cluster_cutoff,)
            ).rowcount
            removed["run_log"] = conn.execute(
                "DELETE FROM run_log WHERE started_at < ?", (cutoff,)
            ).rowcount
        return removed

    def stats(self) -> dict[str, int]:
        tables = ("sent", "oi_snapshot", "insider_buyer", "run_log", "canslim_cache")
        return {
            name: self.conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
            for name in tables
        }
