"""The one line that says what an alert is evidence of, and what to do about it.

Every alert carries a `read`. It is the difference between "NVDA volume 3.4x
normal" — which is a fact you now have to interpret at 11am — and "heavy
selling into a falling price; if you are long, this is the kind of bar that
precedes further downside, so review your stop".

Three rules hold this together.

**Never invent a direction.** Volume has none. A block print has none. Saying
"institutions are accumulating" because a bar was heavy is the single most
common way market tools mislead people, and it is easy to write by accident.
Where direction is unknown the read says so and tells you what would settle it.

**Always end with something to do**, even if that thing is "wait for the close".
A read with no action is a fact wearing a costume.

**Never say buy or sell.** These are position-management prompts — trim, hold,
tighten, size, check — for someone who already has a view. The tool does not.
"""

from __future__ import annotations

from ..models import InsiderTrade, humanise, money

#: Below this, a bar's move is noise and the read must not claim a direction.
FLAT_PCT = 0.3


# --------------------------------------------------------------------------- #
# volume
# --------------------------------------------------------------------------- #

def volume_read(move_pct: float, rvol: float, near_close: bool) -> str:
    heavy = "very heavy" if rvol >= 4 else "heavy"
    closing = " into the close, which is where institutional orders finish" if near_close else ""

    if move_pct <= -FLAT_PCT:
        return (
            f"{heavy.capitalize()} selling pressure — price fell {abs(move_pct):.1f}% on "
            f"{rvol:.1f}x normal volume{closing}. This is what distribution looks like: if you "
            f"hold this, treat it as a prompt to review your stop or trim, not to average down."
        )
    if move_pct >= FLAT_PCT:
        return (
            f"{heavy.capitalize()} buying pressure — price rose {move_pct:.1f}% on {rvol:.1f}x "
            f"normal volume{closing}. Volume-backed moves tend to follow through, so if you are "
            f"long this is confirmation to hold; if you were waiting to exit, you are getting "
            f"liquidity to do it into."
        )
    return (
        f"{rvol:.1f}x normal volume with almost no net move ({move_pct:+.1f}%) — buyers and "
        f"sellers are fighting at this level, which often precedes a decisive move. No action "
        f"yet; watch which side gives way, and note the level."
    )


VOLUME_CAVEATS = [
    "Volume alone carries no direction — the tape does not say who initiated.",
    "Index rebalances, expiry days and news can all produce this without anyone changing their view.",
]


# --------------------------------------------------------------------------- #
# open interest
# --------------------------------------------------------------------------- #

def open_interest_read(right: str, delta: int, strike: float, expiry: str,
                       spot: float | None) -> str:
    """The strongest read available, because open interest is the only proof.

    Volume can be the same contracts changing hands all day. Open interest rising
    means contracts that did not exist yesterday exist now and someone is holding
    them overnight — a position was taken, with money at risk over a weekend.
    """
    where = ""
    if spot:
        distance = (strike - spot) / spot * 100
        if right == "call":
            where = (f" — {abs(distance):.0f}% above spot"
                     if distance > 0 else f" — already {abs(distance):.0f}% in the money")
        else:
            where = (f" — {abs(distance):.0f}% below spot"
                     if distance < 0 else f" — already {abs(distance):.0f}% in the money")

    if delta > 0 and right == "call":
        return (
            f"{humanise(delta)} new call contracts at ${strike:g} expiring {expiry} were opened "
            f"and held overnight{where}. Someone is paying to be right about upside on a deadline. "
            f"If you are long, that is mild confirmation; if you are short or holding covered "
            f"calls near this strike, size for the possibility they are right."
        )
    if delta > 0 and right == "put":
        return (
            f"{humanise(delta)} new put contracts at ${strike:g} expiring {expiry} were opened "
            f"and held overnight{where}. That is either a bet on a fall or someone insuring a "
            f"large existing long — both mean a serious holder sees downside risk worth paying "
            f"for. Treat it as a prompt to check your own downside plan, not as a sell signal."
        )
    closed = abs(delta)
    return (
        f"{humanise(closed)} {right} contracts at ${strike:g} expiring {expiry} were closed out"
        f"{where}. Positioning is being unwound rather than built — whatever conviction built "
        f"this strike is leaving, so expect less support from option hedging around this level."
    )


OI_CAVEATS = [
    "Open interest is computed overnight by the OCC, so this is yesterday's positioning, not live.",
    "A change cannot distinguish a directional bet from a hedge against something you cannot see.",
]


# --------------------------------------------------------------------------- #
# blocks
# --------------------------------------------------------------------------- #

def block_read(notional: float, shares: int, off_exchange: bool, side: str) -> str:
    venue = (
        "away from the lit exchanges, which is where size goes to avoid moving the price"
        if off_exchange else "on the public tape"
    )
    if side in ("buy", "sell"):
        lean = (
            f"It printed on the {'ask' if side == 'buy' else 'bid'}, which leans "
            f"{'buyer' if side == 'buy' else 'seller'}-initiated — an inference from the quote, "
            f"not a fact."
        )
    else:
        lean = "The tape does not say which side initiated, so this is size, not direction."
    return (
        f"A single {humanise(shares)}-share print worth {money(notional)} crossed {venue}. "
        f"{lean} Someone institutional moved a position today; watch whether price holds above "
        f"or below the print level over the next few bars — that is what tells you who needed it more."
    )


BLOCK_CAVEATS = [
    "Side is inferred from the prevailing quote and is often simply unknown.",
    "A single large print can be one leg of a spread, a portfolio trade, or a transfer between accounts.",
]


# --------------------------------------------------------------------------- #
# insider
# --------------------------------------------------------------------------- #

def insider_read(trade: InsiderTrade, cluster_size: int = 1) -> str:
    who = trade.title or "an insider"
    lag = trade.filing_lag_days

    if trade.is_purchase:
        if cluster_size > 1:
            return (
                f"{cluster_size} separate insiders have bought on the open market within the "
                f"cluster window. Cluster buying is the most reliable insider pattern there is — "
                f"one person can be wrong, but several people with the same private view of the "
                f"business rarely act together by accident. If you were waiting for a reason to "
                f"hold through weakness, this is one."
            )
        return (
            f"{who} bought {money(trade.value)} of stock on the open market with their own money "
            f"{lag} day{'s' if lag != 1 else ''} ago. Insiders sell for a dozen reasons but buy "
            f"for one. Worth holding through noise; not worth chasing a gap on."
        )

    if trade.planned_10b5_1:
        return (
            f"{who} sold {money(trade.value)} under a pre-arranged 10b5-1 plan set months ago. "
            f"This carries almost no information about their current view — noted for "
            f"completeness, and no action implied."
        )
    return (
        f"{who} sold {money(trade.value)} on the open market outside any pre-arranged plan, "
        f"filed {lag} day{'s' if lag != 1 else ''} after the trade. Discretionary insider selling "
        f"is worth a look — check whether others are doing the same, and whether it coincides "
        f"with a run-up you might want to take something off into."
    )


INSIDER_CAVEATS = [
    "Form 4 is due within two business days, so this is always news about a completed trade.",
    "Sales can fund tax, divorce, or a house. Purchases are the more informative direction.",
]
