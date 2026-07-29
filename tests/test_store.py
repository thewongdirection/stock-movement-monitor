"""Durable state: dedup, open-interest snapshots, cluster history, pruning."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from monitor.clock import ET
from monitor.models import Alert, OptionContract, Severity
from monitor.store import Store


def alert(**kwargs) -> Alert:
    defaults = dict(
        ticker="NVDA", signal="volume", severity=Severity.HIGH,
        headline="hot bar", occurred_at=datetime(2026, 7, 24, 11, 0, tzinfo=ET),
        identity=("2026-07-24T11:00",),
    )
    defaults.update(kwargs)
    return Alert(**defaults)


def contract(strike: float = 200.0, oi: int = 1000, right: str = "call") -> OptionContract:
    return OptionContract(ticker="NVDA", expiry="2026-08-21", strike=strike,
                          right=right, open_interest=oi)


class TestDedup:
    def test_the_same_alert_is_only_claimed_once(self, store):
        assert store.mark_sent(alert()) is True
        assert store.mark_sent(alert()) is False
        assert store.already_sent(alert().dedup_key)

    def test_a_different_bar_is_a_different_alert(self, store):
        store.mark_sent(alert())
        other = alert(identity=("2026-07-24T11:30",))
        assert store.mark_sent(other) is True

    def test_the_same_event_on_a_different_ticker_is_not_a_duplicate(self, store):
        store.mark_sent(alert())
        assert store.mark_sent(alert(ticker="MSFT")) is True

    def test_the_same_identity_from_a_different_signal_is_not_a_duplicate(self, store):
        store.mark_sent(alert())
        assert store.mark_sent(alert(signal="blocks")) is True

    def test_recent_alerts_can_be_filtered_by_ticker(self, store):
        store.mark_sent(alert())
        store.mark_sent(alert(ticker="MSFT", identity=("x",)))
        assert len(store.recent_alerts(10)) == 2
        assert len(store.recent_alerts(10, "msft")) == 1

    def test_counts_group_by_signal(self, store):
        store.mark_sent(alert())
        store.mark_sent(alert(signal="insider", identity=("y",)))
        assert store.alert_counts(7) == {"volume": 1, "insider": 1}


class TestWatermarks:
    def test_a_watermark_round_trips(self, store):
        assert store.watermark("oi:NVDA") is None
        store.set_watermark("oi:NVDA", "2026-07-24")
        assert store.watermark("oi:NVDA") == "2026-07-24"

    def test_a_watermark_is_overwritten_not_duplicated(self, store):
        store.set_watermark("k", "a")
        store.set_watermark("k", "b")
        assert store.watermark("k") == "b"


class TestOpenInterest:
    def test_a_first_snapshot_has_no_baseline(self, store):
        store.save_oi_snapshot("NVDA", "2026-07-24", [contract(oi=1000)])
        previous, when = store.previous_oi("NVDA", "2026-07-24")
        assert previous == {} and when is None

    def test_the_previous_snapshot_is_the_most_recent_earlier_one(self, store):
        store.save_oi_snapshot("NVDA", "2026-07-22", [contract(oi=800)])
        store.save_oi_snapshot("NVDA", "2026-07-23", [contract(oi=1000)])
        store.save_oi_snapshot("NVDA", "2026-07-24", [contract(oi=1500)])
        previous, when = store.previous_oi("NVDA", "2026-07-24")
        assert when == "2026-07-23"
        assert previous[contract().key] == 1000

    def test_today_is_never_its_own_baseline(self, store):
        """`< as_of`, not `<=` — otherwise every diff is zero."""
        store.save_oi_snapshot("NVDA", "2026-07-24", [contract(oi=1500)])
        previous, when = store.previous_oi("NVDA", "2026-07-24")
        assert previous == {}

    def test_a_ticker_cannot_see_another_tickers_chain(self, store):
        store.save_oi_snapshot("MSFT", "2026-07-23", [contract(oi=999)])
        assert store.previous_oi("NVDA", "2026-07-24") == ({}, None)

    def test_re_saving_the_same_day_updates_rather_than_duplicates(self, store):
        store.save_oi_snapshot("NVDA", "2026-07-24", [contract(oi=1000)])
        store.save_oi_snapshot("NVDA", "2026-07-24", [contract(oi=1200)])
        assert store.stats()["oi_snapshot"] == 1
        store.save_oi_snapshot("NVDA", "2026-07-25", [contract(oi=1300)])
        previous, _ = store.previous_oi("NVDA", "2026-07-25")
        assert previous[contract().key] == 1200

    def test_snapshot_dates_come_back_newest_first(self, store):
        for day in ("2026-07-22", "2026-07-23", "2026-07-24"):
            store.save_oi_snapshot("NVDA", day, [contract()])
        assert store.oi_snapshot_dates("NVDA")[0] == "2026-07-24"


class TestInsiderClusters:
    def test_a_repeat_buyer_keeps_the_most_recent_date(self, store):
        """The bug this replaced: INSERT OR IGNORE froze the first-ever date.

        A buyer whose row keeps an old date drifts out of the cluster window and
        stops counting — so the signal goes quiet exactly when the buying is
        most persistent.
        """
        store.record_insider_buyer("NVDA", "Jane Doe", "2026-06-01", 500_000)
        store.record_insider_buyer("NVDA", "Jane Doe", "2026-07-20", 900_000)
        rows = store.insider_buyers_since("NVDA", "2026-07-01")
        assert len(rows) == 1
        assert rows[0]["traded_on"] == "2026-07-20"
        assert rows[0]["value"] == 900_000

    def test_an_out_of_order_filing_does_not_move_the_date_backwards(self, store):
        store.record_insider_buyer("NVDA", "Jane Doe", "2026-07-20", 900_000)
        store.record_insider_buyer("NVDA", "Jane Doe", "2026-06-01", 500_000)
        rows = store.insider_buyers_since("NVDA", "2026-07-01")
        assert rows and rows[0]["traded_on"] == "2026-07-20"

    def test_distinct_buyers_are_counted_separately(self, store):
        store.record_insider_buyer("NVDA", "Jane Doe", "2026-07-20", 900_000)
        store.record_insider_buyer("NVDA", "Alan Smith", "2026-07-22", 850_000)
        assert len(store.insider_buyers_since("NVDA", "2026-07-01")) == 2

    def test_buyers_before_the_window_are_excluded(self, store):
        store.record_insider_buyer("NVDA", "Old Timer", "2026-01-01", 100_000)
        assert store.insider_buyers_since("NVDA", "2026-07-01") == []


class TestFeed:
    def test_note_feed_returns_the_previous_value(self, store):
        first = datetime(2026, 7, 24, 11, 0, tzinfo=ET)
        assert store.note_feed("NVDA", first) is None
        assert store.note_feed("NVDA", first + timedelta(minutes=30)) == first.isoformat()

    def test_feed_state_covers_every_noted_ticker(self, store):
        store.note_feed("NVDA", datetime(2026, 7, 24, 11, 0, tzinfo=ET))
        store.note_feed("MSFT", datetime(2026, 7, 24, 11, 0, tzinfo=ET))
        assert set(store.feed_state()) == {"NVDA", "MSFT"}


class TestRunLog:
    def test_a_run_is_recorded_and_closed(self, store):
        run_id = store.start_run()
        store.finish_run(run_id, scanned=2, alerts=1, issues=0, ok=True, note="fine")
        run = store.last_runs(1)[0]
        assert run.scanned == 2 and run.ok and run.finished_at is not None

    def test_an_unfinished_run_is_still_listed(self, store):
        store.start_run()
        assert store.last_runs(1)[0].finished_at is None


class TestCanSlimCache:
    def test_a_fresh_entry_is_returned(self, store):
        store.cache_canslim("NVDA", {"grade": "B+"})
        assert store.read_canslim("NVDA", 24) == {"grade": "B+"}

    def test_a_stale_entry_is_ignored(self, store):
        store.cache_canslim("NVDA", {"grade": "B+"})
        assert store.read_canslim("NVDA", 0) is None

    def test_an_absent_entry_is_none(self, store):
        assert store.read_canslim("TSLA", 24) is None


class TestPrune:
    def _age(self, store, table, column, days):
        old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        store.conn.execute(f"UPDATE {table} SET {column} = ?", (old,))
        store.conn.commit()

    def test_old_alerts_are_dropped(self, store):
        store.mark_sent(alert())
        self._age(store, "sent", "sent_at", 90)
        assert store.prune(45, 30)["sent"] == 1

    def test_recent_alerts_survive(self, store):
        store.mark_sent(alert())
        assert store.prune(45, 30)["sent"] == 0

    def test_cluster_history_is_kept_past_the_alert_retention(self, store):
        """The carve-out that matters.

        With the shipped defaults retention (45) and the cluster window (30)
        are different clocks, and a shared cutoff would delete a buyer the
        cluster detector still needs.
        """
        store.record_insider_buyer("NVDA", "Jane Doe", "2026-07-20", 900_000)
        removed = store.prune(retention_days=1, cluster_window_days=365)
        assert removed["insider_buyer"] == 0
        assert store.insider_buyers_since("NVDA", "2026-01-01")

    def test_stats_counts_every_table(self, store):
        store.mark_sent(alert())
        stats = store.stats()
        assert set(stats) == {"sent", "oi_snapshot", "insider_buyer", "run_log", "canslim_cache"}


class TestLifecycle:
    def test_the_store_creates_its_parent_directory(self, tmp_path):
        path = tmp_path / "nested" / "deep" / "monitor.db"
        with Store(path) as made:
            assert path.exists()
            assert made.stats()["sent"] == 0

    def test_reopening_keeps_what_was_written(self, tmp_path):
        path = tmp_path / "monitor.db"
        with Store(path) as made:
            made.mark_sent(alert())
        with Store(path) as again:
            assert again.already_sent(alert().dedup_key)
