"""Captured market data, served back as if it were live.

This exists so the monitor can be demonstrated, tested and threshold-tuned
against real market history without a live feed — and so a threshold sweep
measures something true rather than something invented.

**The whole design rests on one rule: nothing after `as_of` is visible.** A
replay positioned at 11:00 must not be able to see the 14:00 bar. Getting that
wrong is lookahead bias, and lookahead bias makes every strategy look brilliant.
The truncation happens on read, in one place, for every role — and
`hidden_bars()` exists purely so tests can assert that data really was withheld.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..clock import ET, UTC, to_et
from ..models import Bar, InsiderTrade, OptionChain, OptionContract, Side, Trade
from .base import BadResponse, EmptyResponse, NotConfigured


class ReplaySource:
    """Serves bars, chains, prints and filings from a captured directory.

    Layout::

        <dir>/bars/NVDA.json
        <dir>/options/NVDA.json
        <dir>/insider/NVDA.json
        <dir>/trades/NVDA.json
    """

    name = "replay"

    def __init__(self, directory: str | Path, as_of: datetime | None = None):
        self.dir = Path(directory)
        if not self.dir.exists():
            raise NotConfigured(
                self.name,
                f"replay directory {self.dir} does not exist — run `monitor capture` first",
            )
        self.as_of = to_et(as_of) if as_of else None
        self._hidden: dict[str, int] = {}

    # -- files --------------------------------------------------------------
    def _load(self, role: str, ticker: str) -> Any:
        path = self.dir / role / f"{ticker.upper()}.json"
        if not path.exists():
            raise EmptyResponse(self.name, f"no captured {role} for {ticker.upper()}", ticker)
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise BadResponse(self.name, f"{path} is unreadable: {exc}", ticker) from exc

    def _visible(self, ts: datetime) -> bool:
        return self.as_of is None or ts <= self.as_of

    # -- bars ---------------------------------------------------------------
    def bars(self, ticker: str, minutes: int, sessions: int) -> list[Bar]:
        payload = self._load("bars", ticker)
        every = _parse_grid(payload, ticker) if "slots" in payload else _parse_rows(payload, ticker)
        if not every:
            raise EmptyResponse(self.name, "captured file contained no bars", ticker)

        visible = [bar for bar in every if self._visible(bar.ts)]
        self._hidden[ticker.upper()] = len(every) - len(visible)
        if not visible:
            raise EmptyResponse(
                self.name,
                f"every captured bar is later than as_of {self.as_of:%Y-%m-%d %H:%M}",
                ticker,
            )
        return visible

    def hidden_bars(self, ticker: str) -> int:
        """How many bars the last `bars()` call withheld. For tests and `capture --check`."""
        return self._hidden.get(ticker.upper(), 0)

    # -- option chain -------------------------------------------------------
    def chain(self, ticker: str, max_days_to_expiry: int) -> OptionChain:
        payload = self._load("options", ticker)
        snapshots = payload.get("snapshots") if isinstance(payload, dict) else None
        if not isinstance(snapshots, dict) or not snapshots:
            raise BadResponse(self.name, "options file has no 'snapshots' object", ticker)

        cutoff = (self.as_of.date() if self.as_of else date.max).isoformat()
        usable = sorted(d for d in snapshots if d <= cutoff)
        if not usable:
            raise EmptyResponse(
                self.name, f"no option snapshot on or before {cutoff}", ticker
            )
        as_of = usable[-1]
        horizon = (date.fromisoformat(as_of) + timedelta(days=max_days_to_expiry)).isoformat()

        contracts = [
            OptionContract(
                ticker=ticker.upper(),
                expiry=str(row["expiry"]),
                strike=float(row["strike"]),
                right=str(row["right"]).lower(),
                open_interest=int(row.get("oi", row.get("open_interest", 0))),
                volume=int(row.get("volume", 0)),
                as_of=as_of,
            )
            for row in snapshots[as_of]
            if str(row.get("expiry", "")) <= horizon
        ]
        if not contracts:
            raise EmptyResponse(
                self.name, f"snapshot {as_of} has no contracts inside {max_days_to_expiry} days", ticker
            )

        # A replay is also the only place a *previous* snapshot is already on
        # disk, so fill it here rather than making the engine special-case it.
        earlier = [d for d in usable if d < as_of]
        previous: dict[str, int] = {}
        if earlier:
            previous = {
                OptionContract(
                    ticker=ticker.upper(), expiry=str(r["expiry"]), strike=float(r["strike"]),
                    right=str(r["right"]).lower(), open_interest=0,
                ).key: int(r.get("oi", r.get("open_interest", 0)))
                for r in snapshots[earlier[-1]]
            }
        return OptionChain(ticker=ticker.upper(), as_of=as_of,
                           contracts=contracts, previous=previous)

    # -- insider ------------------------------------------------------------
    def filings(self, ticker: str, since: date) -> list[InsiderTrade]:
        payload = self._load("insider", ticker)
        rows = payload.get("filings", payload) if isinstance(payload, dict) else payload
        out: list[InsiderTrade] = []
        for row in rows or []:
            filed_at = to_et(datetime.fromisoformat(row["filed_at"]))
            if not self._visible(filed_at) or filed_at.date() < since:
                continue
            out.append(InsiderTrade(
                ticker=ticker.upper(),
                insider=row["insider"],
                title=row.get("title", "insider"),
                code=row.get("code", "P"),
                shares=float(row["shares"]),
                price=float(row["price"]),
                traded_on=row["traded_on"],
                filed_at=filed_at,
                accession=row.get("accession", ""),
                is_officer=bool(row.get("is_officer")),
                is_director=bool(row.get("is_director")),
                is_ten_percent=bool(row.get("is_ten_percent")),
                planned_10b5_1=bool(row.get("planned_10b5_1")),
                shares_after=row.get("shares_after"),
                url=row.get("url"),
            ))
        return out

    # -- prints -------------------------------------------------------------
    def trades(self, ticker: str, since: datetime) -> list[Trade]:
        payload = self._load("trades", ticker)
        rows = payload.get("trades", payload) if isinstance(payload, dict) else payload
        since = to_et(since)
        out: list[Trade] = []
        for row in rows or []:
            stamp = to_et(datetime.fromisoformat(row["ts"]))
            if stamp < since or not self._visible(stamp):
                continue
            out.append(Trade(
                ticker=ticker.upper(),
                ts=stamp,
                price=float(row["price"]),
                size=int(row["size"]),
                venue=row.get("venue"),
                off_exchange=bool(row.get("off_exchange")),
                side=Side(row.get("side", "unknown")),
                ref=row.get("ref"),
            ))
        return out


# --------------------------------------------------------------------------- #
# capture formats
# --------------------------------------------------------------------------- #

def _parse_rows(payload: Any, ticker: str) -> list[Bar]:
    """The canonical format written by `monitor capture`."""
    rows = payload.get("bars") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise BadResponse("replay", "expected a 'bars' list", ticker)
    bars = [
        Bar(
            ts=to_et(datetime.fromisoformat(row["ts"])),
            open=float(row["o"]), high=float(row["h"]),
            low=float(row["l"]), close=float(row["c"]), volume=int(row["v"]),
        )
        for row in rows
    ]
    bars.sort(key=lambda b: b.ts)
    return bars


def _parse_grid(payload: dict, ticker: str) -> list[Bar]:
    """The session x slot grid some vendor exports use.

    `slots` are UTC clock times, so they are converted rather than assumed —
    a '13:30' slot is the 09:30 Eastern opening bar, and reading it as Eastern
    would file the open under early afternoon.
    """
    sessions = payload.get("sessions") or []
    slots = payload.get("slots") or []
    volume = payload.get("volume") or []
    expected = len(sessions) * len(slots)
    if expected == 0:
        raise BadResponse("replay", "grid capture has no sessions or slots", ticker)
    if len(volume) != expected:
        raise BadResponse(
            "replay",
            f"grid capture is ragged: {len(sessions)} sessions x {len(slots)} slots "
            f"= {expected}, but {len(volume)} volume points",
            ticker,
        )

    series = {name: payload.get(name) or [] for name in ("open", "high", "low", "close")}
    bars: list[Bar] = []
    for si, day in enumerate(sessions):
        for qi, slot in enumerate(slots):
            i = si * len(slots) + qi
            hour, minute = (int(part) for part in slot.split(":"))
            stamp = datetime.combine(
                date.fromisoformat(day), datetime.min.time(), UTC
            ).replace(hour=hour, minute=minute).astimezone(ET)
            bars.append(Bar(
                ts=stamp,
                open=float(series["open"][i]),
                high=float(series["high"][i]),
                low=float(series["low"][i]),
                close=float(series["close"][i]),
                volume=int(volume[i]),
            ))
    bars.sort(key=lambda b: b.ts)
    return bars


def write_bars(path: Path, ticker: str, minutes: int, bars: list[Bar]) -> None:
    """Write the canonical format. Used by `monitor capture`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "ticker": ticker.upper(),
        "interval_minutes": minutes,
        "captured_at": datetime.now(ET).isoformat(timespec="seconds"),
        "bars": [
            {"ts": b.ts.isoformat(), "o": b.open, "h": b.high,
             "l": b.low, "c": b.close, "v": b.volume}
            for b in bars
        ],
    }, indent=1))


def write_chain(path: Path, chain: OptionChain) -> None:
    """Append today's chain to a capture file, keeping earlier snapshots."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"ticker": chain.ticker, "snapshots": {}}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            if isinstance(existing, dict) and isinstance(existing.get("snapshots"), dict):
                payload = existing
        except (json.JSONDecodeError, OSError):
            pass
    payload["snapshots"][chain.as_of] = [
        {"expiry": c.expiry, "strike": c.strike, "right": c.right,
         "oi": c.open_interest, "volume": c.volume}
        for c in chain.contracts
    ]
    path.write_text(json.dumps(payload, indent=1, sort_keys=True))
