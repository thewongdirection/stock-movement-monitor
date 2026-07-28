"""The one interpretive line on each alert.

Everything else in an alert is a measurement; this is the line that says what the
signal is evidence of. Two things are worth guarding: that it **changes with the
facts** (a boilerplate string per detector would be worse than none), and that it
**never claims a direction the data cannot support** — a dark pool print has no
side, so a read telling you to sell would be inventing one.
"""

from __future__ import annotations

import pytest
from conftest import make_config, make_context, make_insider_txn, make_trade, NOW

from monitor.detectors import DarkPoolDetector, InsiderTradeDetector
from monitor.detectors.reads import (
    block_read,
    dark_pool_read,
    insider_read,
    option_volume_read,
    options_flow_read,
    volume_read,
)
from monitor.notify.base import format_alert

#: Words that would turn a read into a recommendation.
ADVICE = ("you should", "we recommend", "buy now", "sell now", "take profit",
          "cut your", "add here", "load up", "get out")


def assert_not_advice(text: str) -> None:
    lowered = text.lower()
    for phrase in ADVICE:
        assert phrase not in lowered, f"read gives advice ({phrase!r}): {text}"


# --------------------------------------------------------------------------
# The read tracks the facts
# --------------------------------------------------------------------------
def test_a_down_bar_and_an_up_bar_read_differently():
    down = volume_read(-2.1, rvol=5.0)
    up = volume_read(+2.1, rvol=5.0)
    assert "distribution" in down.lower()
    assert "accumulation" in up.lower()
    assert down != up


def test_volume_with_no_move_says_so_rather_than_picking_a_side():
    flat = volume_read(0.1, rvol=6.0)
    assert "without a matching price move" in flat
    assert "absorption" in flat
    for word in ("distribution", "accumulation"):
        assert word not in flat.lower()


def test_a_bigger_spike_reads_more_strongly():
    mild = volume_read(-2.0, rvol=2.1)
    heavy = volume_read(-2.0, rvol=8.0)
    assert "Heavy volume" in heavy
    assert mild != heavy


@pytest.mark.parametrize("move,rvol", [(-3.0, 6.0), (3.0, 6.0), (0.0, 6.0), (-0.2, 2.0)])
def test_no_volume_read_gives_advice(move, rvol):
    assert_not_advice(volume_read(move, rvol))


# --------------------------------------------------------------------------
# The signals with no direction must not invent one
# --------------------------------------------------------------------------
def test_a_dark_pool_read_refuses_to_name_a_side():
    """The tape carries no aggressor flag. A directional read here is fabrication."""
    read = dark_pool_read(adv_share=2.4, notional=24_000_000)
    assert "no side" in read
    assert "not buying or selling pressure" in read
    assert_not_advice(read)


def test_a_large_dark_pool_print_is_described_as_larger():
    small = dark_pool_read(adv_share=0.1, notional=1_000_000)
    big = dark_pool_read(adv_share=3.0, notional=40_000_000)
    assert "large institutional" in big
    assert "large institutional" not in small


def test_a_block_read_calls_the_side_inferred():
    read = block_read(adv_share=1.2)
    assert "inferred" in read
    assert "not reported" in read
    assert_not_advice(read)


def test_call_premium_is_not_called_bullish_outright():
    """Large call premium may be a hedge or written against stock."""
    read = options_flow_read("CALL", otm_pct=5.0, dte=30, vol_oi=8.0)
    assert "upside exposure" in read
    assert "positioning" in read or "hedge" in read
    assert_not_advice(read)


def test_a_put_reads_as_downside_exposure():
    assert "downside" in options_flow_read("PUT", otm_pct=4.0, dte=21, vol_oi=3.0)


def test_a_long_dated_option_is_not_called_urgent():
    near = options_flow_read("CALL", otm_pct=6.0, dte=14, vol_oi=4.0)
    far = options_flow_read("CALL", otm_pct=6.0, dte=300, vol_oi=4.0)
    assert "positioning bet rather than a hedge" in near
    assert "does not say which" in far


def test_volume_above_open_interest_is_called_out():
    opening = options_flow_read("CALL", otm_pct=5.0, dte=30, vol_oi=8.0)
    unknown = options_flow_read("CALL", otm_pct=5.0, dte=30, vol_oi=None)
    assert opening.startswith("Volume above open interest")
    assert not unknown.startswith("Volume above open interest")


# --------------------------------------------------------------------------
# Option chain skew
# --------------------------------------------------------------------------
def test_chain_skew_changes_the_read():
    calls = option_volume_read(3.2, skew=5.0)
    puts = option_volume_read(3.2, skew=0.2)
    neutral = option_volume_read(3.2, skew=1.1)
    assert "Call-heavy" in calls and "upside" in calls
    assert "Put-heavy" in puts and "hedging" in puts
    assert "heavy" not in neutral.lower()
    for read in (calls, puts, neutral):
        assert "3.2×" in read
        assert_not_advice(read)


# --------------------------------------------------------------------------
# Insider — the one place direction is genuinely knowable
# --------------------------------------------------------------------------
def test_a_cluster_buy_reads_stronger_than_a_single_buy():
    single = insider_read(True, 1, 3, False, True, False)
    cluster = insider_read(True, 4, 3, False, True, False)
    assert "strongest insider pattern" in cluster
    assert "weaker than a cluster" in single


def test_an_insider_buy_is_allowed_to_be_called_a_vote_of_confidence():
    """Unlike a print, this one has a direction: a person chose to increase."""
    read = insider_read(True, 1, 3, False, True, False)
    assert "vote of confidence" in read
    assert "insiders buy for exactly one reason" in read
    assert_not_advice(read)


def test_a_sale_read_stays_even_handed():
    read = insider_read(False, 0, 3, False, True, False)
    assert "not a scheduled" in read
    assert "innocent reasons" in read
    assert "prompt to look at why, not a verdict" in read
    assert_not_advice(read)


def test_the_role_is_named():
    assert insider_read(True, 1, 3, False, True, False).startswith("An officer")
    assert insider_read(True, 1, 3, False, False, True).startswith("A director")
    assert insider_read(True, 1, 3, True, False, False).startswith("A 10% holder")


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------
def test_the_read_reaches_the_telegram_message(state):
    cfg = make_config(dark_pool={"enabled": True, "min_pct_of_adv": 0})
    ctx = make_context(state, cfg.detector("dark_pool"))
    ctx.adv = 10_000_000
    ctx.prints = [make_trade(size=50_000, price=100.0)]
    alerts = DarkPoolDetector().run(ctx)

    assert alerts
    rendered = format_alert(alerts[0])
    assert "➤" in rendered
    assert "no side" in rendered
    # It sits after the evidence and before the timestamp, so a reader can tell
    # measurement from interpretation.
    assert rendered.index("➤") > rendered.index("off-exchange")


def test_every_insider_alert_carries_a_read(state):
    cfg = make_config(insider_trades={"enabled": True})
    ctx = make_context(state, cfg.detector("insider_trades"))
    ctx.insider_transactions = [make_insider_txn(shares=20_000, price=100.0, code="P")]
    alerts = InsiderTradeDetector().run(ctx)

    assert alerts
    assert alerts[0].read
    assert_not_advice(alerts[0].read)


def test_the_dry_run_preview_shows_what_telegram_would_show(state, capsys):
    """The preview renders through format_alert, so it cannot drift from the real
    message — which it had, silently dropping the read."""
    from monitor.notify.base import ConsoleNotifier

    cfg = make_config(dark_pool={"enabled": True, "min_pct_of_adv": 0})
    ctx = make_context(state, cfg.detector("dark_pool"))
    ctx.adv = 10_000_000
    ctx.prints = [make_trade(size=50_000, price=100.0)]
    alert = DarkPoolDetector().run(ctx)[0]

    ConsoleNotifier().send(alert)
    printed = capsys.readouterr().out
    assert "➤" in printed
    assert "no side" in printed
    assert "<b>" not in printed  # tags stripped for the terminal
