"""Open interest change — the only signal here that proves a position was taken.

Everything else in this project measures activity. A heavy bar might be one
institution accumulating or it might be the same shares round-tripping between
market makers forty times. A large option volume figure has exactly the same
ambiguity: contracts that opened and closed the same day leave no trace.

Open interest is different. It counts contracts that exist at the end of the
day because somebody opened them and *kept* them. A rise means positions were
created and carried overnight, with capital committed and time decay running.
That is as close to proof of intent as public market data gets, and it is the
reason this signal is evaluated first and reported first.

Its cost is latency. The OCC computes open interest overnight, so the freshest
possible figure during a session is yesterday's close. This signal is therefore
a once-a-day statement, not an intraday one, and it says so in every alert.
"""

from __future__ import annotations

from datetime import date

from ..models import Alert, OptionContract, Severity, escalate, humanise, money
from .base import Context
from .reads import OI_CAVEATS, open_interest_read

#: US listed equity options are 100 shares apiece.
CONTRACT_MULTIPLIER = 100


class OpenInterestSignal:
    name = "open_interest"

    def evaluate(self, ctx: Context) -> list[Alert]:
        chain = ctx.chain
        if chain is None or not chain.has_baseline:
            # A first snapshot knows nothing about change. The engine reports
            # that as a note, because it is a normal bootstrap state and not a
            # fault worth an alert.
            return []

        min_change = ctx.setting("signals.open_interest.min_oi_change")
        min_pct = ctx.setting("signals.open_interest.min_oi_change_pct")
        min_notional = ctx.setting("signals.open_interest.min_notional")
        max_days = ctx.setting("signals.open_interest.max_days_to_expiry")
        top_n = ctx.setting("signals.open_interest.top_n")

        as_of = _as_date(chain.as_of)
        movers: list[tuple[OptionContract, int, float]] = []
        net = {"call": 0, "put": 0}

        for contract in chain.contracts:
            delta = chain.oi_change(contract)
            if delta is None:
                continue
            expiry = _as_date(contract.expiry)
            if as_of and expiry and (expiry - as_of).days > max_days:
                continue
            net[contract.right] = net.get(contract.right, 0) + delta

            before = chain.previous.get(contract.key, 0)
            notional = abs(delta) * CONTRACT_MULTIPLIER * contract.strike
            pct = abs(delta) / before * 100 if before else float("inf")
            if abs(delta) < min_change or pct < min_pct or notional < min_notional:
                continue
            movers.append((contract, delta, notional))

        if not movers:
            return []

        movers.sort(key=lambda row: abs(row[2]), reverse=True)
        top = movers[:top_n]
        lead, lead_delta, lead_notional = top[0]
        spot = ctx.latest.close if ctx.latest else None

        # Anything that reaches here already cleared three thresholds, so the
        # bar for HIGH is set well above them — an order of magnitude past the
        # contract floor, twenty times the dollar floor. Escalating at 3x would
        # make every alert this signal ever sends a red one, and a channel where
        # everything is urgent is a channel nobody reads.
        severity = escalate(
            Severity.MEDIUM,
            abs(lead_delta) >= min_change * 10 and lead_notional >= min_notional * 20,
        )

        facts = [
            f"{'Opened' if delta > 0 else 'Closed'} {humanise(abs(delta))} × "
            f"{contract.label()} — {money(abs(notional))} notional, "
            f"open interest {chain.previous.get(contract.key, 0):,} → {contract.open_interest:,}"
            for contract, delta, notional in top
        ]
        if len(movers) > len(top):
            facts.append(f"...and {len(movers) - len(top)} further contracts past the thresholds")
        facts.append(
            f"Net across the chain: calls {net['call']:+,}, puts {net['put']:+,}"
        )
        facts.append(
            f"Snapshot {chain.as_of} against the previous stored chain — "
            f"open interest is settled overnight, so this is end-of-day positioning"
        )

        verb = "opened" if lead_delta > 0 else "closed"
        return [Alert(
            ticker=ctx.ticker,
            signal=self.name,
            severity=severity,
            headline=(
                f"{ctx.ticker} — {humanise(abs(lead_delta))} {lead.right} contracts {verb} "
                f"at ${lead.strike:g} ({lead.expiry})"
            ),
            occurred_at=ctx.now,
            facts=facts,
            read=open_interest_read(lead.right, lead_delta, lead.strike, lead.expiry, spot),
            caveats=list(OI_CAVEATS),
            identity=(chain.as_of, lead.key, str(lead_delta)),
        )]


def _as_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
