"""Replaying bars from a file.

Two things carry the weight here. First, **no lookahead**: a replay at 11:00 must
not see the 14:00 bar, or every threshold tuned against it looks better than it
is. Second, **no silence about being a replay**: this module hands the engine
stale data on purpose, which is the one thing the rest of the project refuses to
do quietly, so the provenance line has to be impossible to miss.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from monitor import config as config_mod, engine
from monitor.market_calendar import ET
from monitor.models import Bar
from monitor.providers.base import ProviderError, SetupError
from monitor.providers.snapshot import SnapshotBars, write_snapshot

SESSION = datetime(2026, 7, 27, 14, 30, tzinfo=ET)


def rows(count: int, *, start: datetime, step_minutes: int = 30, volume: int = 1_000_000):
    return [
        {
            "ts": (start + timedelta(minutes=step_minutes * i)).isoformat(),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": volume,
        }
        for i in range(count)
    ]


def write(tmp_path, bars_by_ticker, *, interval="30min", captured_at=None, **extra):
    payload = {
        "captured_at": (captured_at or SESSION).isoformat(),
        "source": "test fixture",
        "interval": interval,
        "bars": bars_by_ticker,
        **extra,
    }
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(payload))
    return path


# --------------------------------------------------------------------------
# No lookahead
# --------------------------------------------------------------------------
def test_bars_after_the_run_clock_are_hidden(tmp_path):
    """The whole point: a replay must not be able to see its own future."""
    open_bar = datetime(2026, 7, 27, 9, 30, tzinfo=ET)
    path = write(tmp_path, {"NVDA": rows(13, start=open_bar)})

    provider = SnapshotBars(path, as_of=datetime(2026, 7, 27, 12, 0, tzinfo=ET))
    visible = provider.intraday_bars("NVDA", "30min")

    assert visible[-1].ts == datetime(2026, 7, 27, 12, 0, tzinfo=ET)
    assert all(bar.ts <= provider.as_of for bar in visible)
    assert provider.hidden_bars() == 13 - len(visible)


def test_without_a_clock_every_bar_is_visible(tmp_path):
    path = write(tmp_path, {"NVDA": rows(13, start=datetime(2026, 7, 27, 9, 30, tzinfo=ET))})
    provider = SnapshotBars(path)
    assert len(provider.intraday_bars("NVDA", "30min")) == 13
    assert provider.hidden_bars() == 0


def test_a_clock_before_the_snapshot_starts_is_an_error_not_an_empty_list(tmp_path):
    """Silently returning nothing would read as 'quiet market', not 'wrong clock'."""
    path = write(tmp_path, {"NVDA": rows(5, start=datetime(2026, 7, 27, 9, 30, tzinfo=ET))})
    provider = SnapshotBars(path, as_of=datetime(2026, 7, 20, 9, 30, tzinfo=ET))

    with pytest.raises(ProviderError) as exc:
        provider.intraday_bars("NVDA", "30min")
    assert "no bars at or before the run clock" in str(exc.value)
    assert "later --as-of" in str(exc.value)


def test_the_newest_bar_is_the_newest_visible_one(tmp_path):
    path = write(tmp_path, {"NVDA": rows(13, start=datetime(2026, 7, 27, 9, 30, tzinfo=ET))})
    clock = datetime(2026, 7, 27, 11, 0, tzinfo=ET)
    provider = SnapshotBars(path, as_of=clock)
    assert provider.newest_bar() == clock
    assert provider.age_hours(clock + timedelta(hours=2)) == pytest.approx(2.0)


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------
def test_utc_timestamps_are_normalised_to_eastern(tmp_path):
    """The detector buckets by (hour, minute) and the alert prints it as ET.

    A file of UTC stamps would label an 11:00 ET bar "15:00 ET", and would bucket
    bars into different slots either side of a DST change.
    """
    path = write(
        tmp_path,
        {"NVDA": [dict(r, ts=r["ts"]) for r in rows(1, start=datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc))]},
    )
    bar = SnapshotBars(path).intraday_bars("NVDA", "30min")[0]
    assert (bar.ts.hour, bar.ts.minute) == (11, 0)
    assert bar.ts.utcoffset() == timedelta(hours=-4)


def test_a_z_suffix_is_accepted(tmp_path):
    path = write(
        tmp_path,
        {"NVDA": [{"ts": "2026-07-27T15:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 5}]},
    )
    bar = SnapshotBars(path).intraday_bars("NVDA", "30min")[0]
    assert (bar.ts.hour, bar.ts.minute) == (11, 0)


def test_short_field_names_are_accepted(tmp_path):
    """Most market APIs use o/h/l/c/v, and so does anyone hand-writing a fixture."""
    path = write(
        tmp_path,
        {"NVDA": [{"ts": "2026-07-27T13:30:00-04:00", "o": 1, "h": 3, "l": 0.5, "c": 2, "v": 9}]},
    )
    bar = SnapshotBars(path).intraday_bars("NVDA", "30min")[0]
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (1.0, 3.0, 0.5, 2.0, 9)


def test_bars_are_sorted_however_the_file_ordered_them(tmp_path):
    unsorted = list(reversed(rows(4, start=datetime(2026, 7, 27, 9, 30, tzinfo=ET))))
    path = write(tmp_path, {"NVDA": unsorted})
    bars = SnapshotBars(path).intraday_bars("NVDA", "30min")
    assert bars == sorted(bars, key=lambda b: b.ts)


def test_unparseable_rows_are_skipped_not_fatal(tmp_path):
    good = rows(2, start=datetime(2026, 7, 27, 9, 30, tzinfo=ET))
    path = write(
        tmp_path,
        {"NVDA": [*good, {"ts": "not a date", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
                  {"ts": "2026-07-27T11:00:00-04:00"}, "not even a dict"]},
    )
    assert len(SnapshotBars(path).intraday_bars("NVDA", "30min")) == 2


# --------------------------------------------------------------------------
# Interval and ticker mismatches
# --------------------------------------------------------------------------
def test_the_wrong_bar_interval_is_refused(tmp_path):
    """A 30-minute bar judged against a 5-minute baseline is a fabricated anomaly."""
    path = write(tmp_path, {"NVDA": rows(3, start=SESSION)}, interval="30min")
    with pytest.raises(ProviderError) as exc:
        SnapshotBars(path).intraday_bars("NVDA", "5min")
    assert "snapshot holds 30min bars" in str(exc.value)
    assert "bar_interval" in str(exc.value)


def test_a_snapshot_with_no_declared_interval_is_taken_at_face_value(tmp_path):
    path = write(tmp_path, {"NVDA": rows(3, start=SESSION)}, interval="")
    assert SnapshotBars(path).intraday_bars("NVDA", "5min")


def test_a_missing_ticker_says_what_the_file_does_hold(tmp_path):
    path = write(tmp_path, {"NVDA": rows(3, start=SESSION), "MSFT": rows(3, start=SESSION)})
    with pytest.raises(ProviderError) as exc:
        SnapshotBars(path).intraday_bars("TSLA", "30min")
    assert "MSFT, NVDA" in str(exc.value)
    assert "Re-capture" in str(exc.value)


def test_tickers_are_matched_case_insensitively(tmp_path):
    path = write(tmp_path, {"nvda": rows(3, start=SESSION)})
    assert SnapshotBars(path).intraday_bars("NVDA", "30min")


# --------------------------------------------------------------------------
# Broken files
# --------------------------------------------------------------------------
def test_a_missing_file_names_the_command_that_writes_one(tmp_path):
    with pytest.raises(SetupError) as exc:
        SnapshotBars(tmp_path / "nope.json")
    assert "monitor capture" in str(exc.value)


@pytest.mark.parametrize(
    "body,expected",
    [
        ("{not json", "not valid JSON"),
        ("[1, 2, 3]", "must contain a JSON object"),
        ('{"bars": {}}', "no `bars` object"),
        ('{"bars": {"NVDA": []}}', "no usable bars"),
        ('{"bars": {"NVDA": [{"ts": "bad"}]}}', "no usable bars"),
    ],
)
def test_a_broken_snapshot_fails_at_setup_with_a_reason(tmp_path, body, expected):
    """SetupError rather than ProviderError: it is not a per-ticker problem."""
    path = tmp_path / "snapshot.json"
    path.write_text(body)
    with pytest.raises(SetupError) as exc:
        SnapshotBars(path)
    assert expected in str(exc.value)


# --------------------------------------------------------------------------
# ADV
# --------------------------------------------------------------------------
def test_adv_excludes_the_day_being_replayed(tmp_path):
    """Same rule as the live providers — a partial session drags the average down."""
    bars = [
        Bar(ts=datetime(2026, 7, d, 10, 0, tzinfo=ET), open=1, high=1, low=1, close=1,
            volume=vol)
        for d, vol in ((23, 10_000_000), (24, 10_000_000), (27, 1_000))
    ]
    path = write(tmp_path, {"NVDA": rows(1, start=SESSION)})
    provider = SnapshotBars(path)
    adv = provider.average_daily_volume(bars, sessions=20, now=SESSION)
    assert adv == pytest.approx(10_000_000)


def test_adv_falls_back_to_the_capture_time_when_given_no_clock(tmp_path):
    path = write(tmp_path, {"NVDA": rows(1, start=SESSION)}, captured_at=SESSION)
    provider = SnapshotBars(path)
    bars = [
        Bar(ts=datetime(2026, 7, 24, 10, 0, tzinfo=ET), open=1, high=1, low=1, close=1,
            volume=5_000_000),
        Bar(ts=SESSION, open=1, high=1, low=1, close=1, volume=7),
    ]
    assert provider.average_daily_volume(bars, sessions=20) == pytest.approx(5_000_000)


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------
def test_the_provenance_line_cannot_be_mistaken_for_a_live_run(tmp_path):
    path = write(tmp_path, {"NVDA": rows(13, start=datetime(2026, 7, 27, 9, 30, tzinfo=ET))})
    clock = datetime(2026, 7, 27, 11, 0, tzinfo=ET)
    line = SnapshotBars(path, as_of=clock).provenance(clock + timedelta(hours=3))

    assert line.startswith("REPLAY")
    assert "snapshot.json" in line
    assert "No live market data was fetched" in line
    assert "withheld" in line  # the later bars it is not allowed to see


def test_provenance_reports_days_when_the_snapshot_is_old(tmp_path):
    path = write(tmp_path, {"NVDA": rows(2, start=SESSION)})
    line = SnapshotBars(path).provenance(SESSION + timedelta(days=9))
    assert "days before the run clock" in line


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------
def test_written_snapshots_read_back_identically(tmp_path):
    """`monitor capture` writes this; SnapshotBars reads it. They must agree."""
    original = [
        Bar(ts=datetime(2026, 7, 27, 9, 30, tzinfo=ET), open=1.5, high=2.5, low=1.0,
            close=2.0, volume=1234),
        Bar(ts=datetime(2026, 7, 27, 10, 0, tzinfo=ET), open=2.0, high=3.0, low=1.9,
            close=2.9, volume=5678),
    ]
    path = write_snapshot(
        tmp_path / "out.json", {"NVDA": original}, interval="30min",
        source="unit test", captured_at=SESSION,
    )
    provider = SnapshotBars(path)
    assert provider.intraday_bars("NVDA", "30min") == original
    assert provider.interval == "30min"
    assert provider.source == "unit test"
    assert provider.captured_at == SESSION


# --------------------------------------------------------------------------
# Config and engine wiring
# --------------------------------------------------------------------------
def test_snapshot_is_an_allowed_bars_provider():
    cfg = config_mod.from_dict(
        {"tickers": ["NVDA"], "providers": {"bars": "snapshot",
                                            "snapshot": {"path": "state/s.json"}}}
    )
    assert cfg.providers.bars == "snapshot"
    assert cfg.providers.snapshot_path == "state/s.json"
    assert cfg.issues == []


def test_snapshot_without_a_path_is_a_config_error():
    """Replay must be asked for explicitly, never fallen into by half-configuring."""
    cfg = config_mod.from_dict({"tickers": ["NVDA"], "providers": {"bars": "snapshot"}})
    assert any(i.path == "providers.snapshot.path" for i in cfg.issues)
    assert any("monitor capture" in i.message for i in cfg.issues)


def test_a_replay_run_says_so_in_the_footer(tmp_path, state):
    """A replay that looks like a live run is the whole risk of this feature."""
    from conftest import build_bars

    bars = build_bars(sessions=12, slots=12)
    path = write_snapshot(
        tmp_path / "snap.json", {"TEST": bars}, interval="5min",
        source="unit test", captured_at=bars[-1].ts,
    )
    cfg = config_mod.from_dict(
        {
            "tickers": ["TEST"],
            "detectors": {n: {"enabled": n == "volume_anomaly"}
                          for n in config_mod.DETECTOR_SPECS},
            "providers": {"bars": "snapshot", "snapshot": {"path": str(path)}},
        }
    )

    class Recorder:
        def __init__(self):
            self.summaries: list[str] = []

        def send(self, alert, files=None):
            return True

        def send_summary(self, text):
            self.summaries.append(text)
            return True

    notifier = Recorder()
    result = engine.run(cfg, state, notifier, now=bars[-1].ts + timedelta(minutes=6))

    assert result.replay_note.startswith("REPLAY")
    assert "Replay run" in "\n".join(notifier.summaries)


def test_a_live_run_carries_no_replay_note(state):
    from conftest import NOW
    from test_engine import RecordingNotifier, StubProviders, only

    result = engine.run(
        only("dark_pool", min_pct_of_adv=0), state, RecordingNotifier(),
        now=NOW, providers=StubProviders(),
    )
    assert result.replay_note == ""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_as_of_forces_a_dry_run(tmp_path, monkeypatch, capsys):
    """A back-dated alert arriving on your phone today is worse than no alert."""
    from monitor import cli

    config_path = tmp_path / "config.yaml"
    config_path.write_text("tickers: [TEST]\ndetectors: {volume_anomaly: {enabled: true}}\n")
    captured: dict = {}
    monkeypatch.setattr(
        cli.engine, "run",
        lambda config, state, notifier, **kw: (
            captured.update(kw, notifier=notifier) or engine.RunResult(started_at=kw["now"])
        ),
    )
    code = cli.main(
        ["run", "-c", str(config_path), "--overlay", str(tmp_path / "o.json"),
         "--as-of", "2026-07-27T11:00:00-04:00"]
    )

    assert code == 0
    assert captured["now"] == datetime(2026, 7, 27, 11, 0, tzinfo=ET)
    assert type(captured["notifier"]).__name__ == "ConsoleNotifier"
    assert "dry-run forced" in capsys.readouterr().out


def test_a_naive_as_of_is_read_as_utc(tmp_path, monkeypatch):
    from monitor import cli

    config_path = tmp_path / "config.yaml"
    config_path.write_text("tickers: [TEST]\n")
    captured: dict = {}
    monkeypatch.setattr(
        cli.engine, "run",
        lambda config, state, notifier, **kw: (
            captured.update(kw) or engine.RunResult(started_at=kw["now"])
        ),
    )
    cli.main(["run", "-c", str(config_path), "--overlay", str(tmp_path / "o.json"),
              "--as-of", "2026-07-27T15:00:00"])
    assert captured["now"] == datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)


def test_a_nonsense_as_of_is_rejected(tmp_path, capsys):
    from monitor import cli

    config_path = tmp_path / "config.yaml"
    config_path.write_text("tickers: [TEST]\n")
    code = cli.main(["run", "-c", str(config_path), "--overlay", str(tmp_path / "o.json"),
                     "--as-of", "last tuesday"])
    assert code == 2
    assert "is not an ISO-8601 timestamp" in capsys.readouterr().err


def test_capture_refuses_to_copy_a_snapshot(tmp_path, capsys):
    """Capturing from a snapshot would just duplicate the file."""
    from monitor import cli

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "tickers: [TEST]\nproviders:\n  bars: snapshot\n  snapshot:\n    path: x.json\n"
    )
    assert cli.main(["capture", "-c", str(config_path), "-o", str(tmp_path / "o.json")]) == 2
    assert "already 'snapshot'" in capsys.readouterr().err
