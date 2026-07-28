"""The one-line read on each alert: what it is evidence of, and what to check.

Every other line in an alert is a measurement. This is the one line that is an
*interpretation*, so it is kept here rather than scattered through the detectors
— one file to review when you disagree with a reading, and one place where the
line between "what happened" and "what it might mean" is enforced.

**These are reads, not recommendations.** The distinction is not legal
throat-clearing; it is forced by the data:

* A dark pool print has **no side**. The consolidated tape carries no aggressor
  flag, so a $24M cross is $24M of *someone* trading — it is not selling
  pressure, and a line saying "consider trimming" would be inventing a direction
  the feed never supplied.
* Large call premium is **not automatically bullish**. It may be a hedge against
  a short, or calls written against stock someone already owns.
* Even a volume spike with price up is *net* buying pressure inferred from where
  the bar closed, not a measured buy/sell split.

So each read says what the signal is consistent with, names the ambiguity where
there is one, and points at the next thing to look at — a chart, a filing, the
scorecard. Where direction genuinely is knowable, it says so without hedging: an
insider selling on the open market is a real person choosing to reduce, and the
read reflects that.

What none of them do is tell you to buy or sell. That is your call, made with
your position size, your basis and your thesis — none of which this tool knows.
"""

from __future__ import annotations

#: Below this, a move is chop rather than direction worth naming.
MEANINGFUL_MOVE_PCT = 0.75


def volume_read(move_pct: float, rvol: float, is_breakout_hour: bool = False) -> str:
    """L1: unusual volume on a bar, with the direction the bar closed."""
    heavy = rvol >= 4.0
    if move_pct <= -MEANINGFUL_MOVE_PCT:
        return (
            "Heavy volume on a down bar — distribution is the usual reading. "
            "Check whether it breaks a level you care about before deciding "
            "anything about your position."
            if heavy
            else "Volume behind a down move. Worth a chart check for a broken "
            "level; one bar is not a trend."
        )
    if move_pct >= MEANINGFUL_MOVE_PCT:
        return (
            "Heavy volume on an up bar — accumulation is the usual reading, and "
            "conviction moves tend to come on volume like this. Check the chart "
            "for a breakout you might be early to."
            if heavy
            else "Volume behind an up move. Check whether it is a breakout from "
            "a base or just an intraday pop."
        )
    return (
        "Volume without a matching price move — often absorption, a block being "
        "worked, or index rebalancing. Watch the next few bars to see which way "
        "it resolves rather than acting on this one."
    )


def dark_pool_read(adv_share: float | None, notional: float) -> str:
    """L2: an off-exchange print. Size is known; side is not."""
    big = adv_share is not None and adv_share >= 1.0
    scale = "a large institutional order" if big else "an institutional order"
    return (
        f"Someone moved size off-exchange — {scale} being worked quietly rather "
        "than shown to the lit market. The tape gives no side, so this is not "
        "buying or selling pressure on its own: watch whether price follows in "
        "the next few sessions, and treat repeated prints in one name as the "
        "signal rather than any single one."
    )


def block_read(adv_share: float | None) -> str:
    """L2: a single large print, sized in shares."""
    return (
        "One participant took or supplied a lot of stock in a single print. "
        "Direction is inferred from the quote, not reported, so treat it as "
        "evidence of institutional interest rather than a directional call — "
        "the follow-through over the next sessions is what tells you which."
    )


def options_flow_read(
    kind: str, otm_pct: float | None, dte: int | None, vol_oi: float | None
) -> str:
    """L3: a single large options trade."""
    urgency = (
        "Short-dated and out of the money, which is a positioning bet rather "
        "than a hedge for most sizes."
        if (dte is not None and dte <= 45 and otm_pct is not None and otm_pct > 2)
        else "Could be positioning or a hedge — the feed does not say which."
    )
    side = "upside" if kind.lower() == "call" else "downside"
    opening = (
        "Volume above open interest means the position is being opened, not closed. "
        if vol_oi is not None and vol_oi > 1
        else ""
    )
    return (
        f"{opening}Someone paid real money for {side} exposure in size. {urgency} "
        "Worth checking the earnings date and any pending catalyst before reading "
        "intent into it."
    )


def option_volume_read(ratio: float, skew: float | None) -> str:
    """L3-lite: the whole chain busier than usual."""
    lean = ""
    if skew is not None and skew >= 3.0:
        lean = "Call-heavy, so the activity leans toward upside positioning. "
    elif skew is not None and skew <= 0.33:
        lean = "Put-heavy, which is either downside positioning or hedging. "
    return (
        f"{lean}The whole option chain is {ratio:.1f}× its normal activity — "
        "something has drawn attention to this name, though this signal does not "
        "say what. Check the news and the earnings calendar; it often front-runs "
        "a move in the stock."
    )


def insider_read(
    is_purchase: bool,
    cluster_count: int,
    cluster_min: int,
    is_ten_pct_owner: bool,
    is_officer: bool,
    is_director: bool,
) -> str:
    """Form 4. The one signal here where direction is genuinely knowable."""
    who = (
        "An officer"
        if is_officer
        else "A director"
        if is_director
        else "A 10% holder"
        if is_ten_pct_owner
        else "An insider"
    )
    if is_purchase:
        if cluster_count >= cluster_min:
            return (
                f"{cluster_count} insiders buying their own stock inside a month is "
                "the strongest insider pattern there is — people with the best view "
                "of the business are choosing to increase exposure with their own "
                "money. Read the filings and the last earnings call before acting, "
                "and note the trade date is already days old."
            )
        return (
            f"{who} chose to buy on the open market with their own money, which is "
            "a genuine vote of confidence — insiders buy for exactly one reason. "
            "One buyer is weaker than a cluster; check whether others follow."
        )
    return (
        f"{who} is reducing their position by choice — this was not a scheduled "
        "10b5-1 sale, which is why you are seeing it. Insiders sell for many "
        "innocent reasons (tax, diversification, a house), so this is a prompt to "
        "look at why, not a verdict. Size relative to their remaining holding is "
        "the thing to weigh."
    )
