"""L2 (prints) and L3 (options flow) detector tests."""

from __future__ import annotations

from conftest import (
    NOW,
    make_config,
    make_context,
    make_option_trade,
    make_trade,
)

from monitor.detectors import BlockTradeDetector, DarkPoolDetector, OptionsFlowDetector
from monitor.models import Side

ADV = 10_000_000  # 10M shares/day, so 50k shares = 0.5% of ADV


def block_settings(**overrides):
    return make_config(block_trades=overrides).detector("block_trades")


def dark_settings(**overrides):
    return make_config(dark_pool=overrides).detector("dark_pool")


def flow_settings(**overrides):
    return make_config(options_flow=overrides).detector("options_flow")


# --------------------------------------------------------------------------
# Block trades
# --------------------------------------------------------------------------
def test_large_print_fires(state):
    ctx = make_context(
        state,
        block_settings(),
        prints=[make_trade(size=50_000, price=100.0)],
        adv=ADV,
    )
    alerts = BlockTradeDetector().run(ctx)
    assert len(alerts) == 1
    assert "$5.00M" in alerts[0].headline


def test_small_print_is_ignored(state):
    ctx = make_context(
        state, block_settings(), prints=[make_trade(size=500, price=100.0)], adv=ADV
    )
    assert BlockTradeDetector().run(ctx) == []


def test_classic_preset_catches_what_institutional_misses(state):
    print_ = make_trade(size=12_000, price=25.0)  # 12k shares, $300k
    assert (
        BlockTradeDetector().run(
            make_context(state, block_settings(preset="institutional", min_pct_of_adv=0), prints=[print_])
        )
        == []
    )
    assert (
        len(
            BlockTradeDetector().run(
                make_context(
                    state,
                    block_settings(preset="classic", min_pct_of_adv=0),
                    prints=[print_],
                )
            )
        )
        == 1
    )


def test_combine_all_requires_both_share_and_dollar_floors(state):
    # 30k shares but only $150k — clears shares, fails notional.
    print_ = make_trade(size=30_000, price=5.0)
    assert (
        BlockTradeDetector().run(
            make_context(
                state,
                block_settings(combine="all", min_pct_of_adv=0),
                prints=[print_],
            )
        )
        == []
    )
    assert (
        len(
            BlockTradeDetector().run(
                make_context(
                    state,
                    block_settings(combine="any", min_pct_of_adv=0),
                    prints=[print_],
                )
            )
        )
        == 1
    )


def test_pct_of_adv_filters_a_print_that_is_large_only_in_absolute_terms(state):
    # 50k shares against a 500M-share ADV is 0.01% — noise for that name.
    ctx = make_context(
        state,
        block_settings(min_pct_of_adv=0.25),
        prints=[make_trade(size=50_000, price=100.0)],
        adv=500_000_000,
    )
    assert BlockTradeDetector().run(ctx) == []


def test_missing_adv_skips_that_test_and_says_so(state):
    ctx = make_context(
        state,
        block_settings(min_pct_of_adv=0.25),
        prints=[make_trade(size=50_000, price=100.0)],
        adv=None,
    )
    assert len(BlockTradeDetector().run(ctx)) == 1
    assert any("average daily volume unavailable" in n for n in ctx.notes)


def test_side_is_reported_as_inferred_never_asserted(state):
    ctx = make_context(
        state, block_settings(), prints=[make_trade(price=100.02, bid=99.98, ask=100.02)], adv=ADV
    )
    alerts = BlockTradeDetector().run(ctx)
    joined = " ".join(alerts[0].lines)
    assert "inferred" in joined
    assert "not reported" in joined


def test_side_unknown_when_no_quote_context(state):
    ctx = make_context(
        state, block_settings(), prints=[make_trade(bid=None, ask=None)], adv=ADV
    )
    alerts = BlockTradeDetector().run(ctx)
    joined = " ".join(alerts[0].lines)
    assert "Side undetermined" in joined


def test_max_alerts_per_run_caps_and_keeps_the_biggest(state):
    prints = [
        make_trade(size=30_000 + i * 10_000, price=100.0, raw_id=f"p{i}", minutes_ago=2)
        for i in range(8)
    ]
    ctx = make_context(
        state,
        block_settings(max_alerts_per_run=3, min_pct_of_adv=0),
        prints=prints,
        adv=ADV,
    )
    alerts = BlockTradeDetector().run(ctx)
    assert len(alerts) == 3
    assert any("suppressed by max_alerts_per_run" in n for n in ctx.notes)


# --------------------------------------------------------------------------
# Dark pool
# --------------------------------------------------------------------------
def test_dark_pool_print_fires_on_notional(state):
    ctx = make_context(
        state, dark_settings(), prints=[make_trade(size=50_000, price=100.0)], adv=ADV
    )
    alerts = DarkPoolDetector().run(ctx)
    assert len(alerts) == 1
    assert "Dark pool print" in alerts[0].headline
    assert any("Venue is not disclosed" in line for line in alerts[0].lines)


def test_dark_pool_ignores_on_exchange_prints(state):
    ctx = make_context(
        state,
        dark_settings(),
        prints=[make_trade(size=50_000, price=100.0, off_exchange=False)],
        adv=ADV,
    )
    assert DarkPoolDetector().run(ctx) == []


def test_one_print_cannot_alert_through_both_l2_detectors(state):
    """block_trades and dark_pool share the UW off-exchange stream."""
    prints = [make_trade(size=50_000, price=100.0)]
    ctx = make_context(state, {}, prints=prints, adv=ADV)

    ctx.settings = block_settings(min_pct_of_adv=0)
    from_block = BlockTradeDetector().run(ctx)
    ctx.settings = dark_settings(min_pct_of_adv=0)
    from_dark = DarkPoolDetector().run(ctx)

    assert len(from_block) == 1
    assert from_dark == []


def test_dark_pool_combine_all_requires_both_tests(state):
    # $5M notional but only 0.05% of a 100M-share ADV.
    ctx = make_context(
        state,
        dark_settings(combine="all", min_pct_of_adv=0.5),
        prints=[make_trade(size=50_000, price=100.0)],
        adv=100_000_000,
    )
    assert DarkPoolDetector().run(ctx) == []


# --------------------------------------------------------------------------
# Options flow
# --------------------------------------------------------------------------
def test_whale_premium_fires(state):
    ctx = make_context(state, flow_settings(), option_trades=[make_option_trade()])
    alerts = OptionsFlowDetector().run(ctx)
    assert len(alerts) == 1
    assert "Options flow" in alerts[0].headline
    assert "CALL" in alerts[0].headline


def test_small_premium_is_ignored(state):
    ctx = make_context(
        state, flow_settings(), option_trades=[make_option_trade(premium=25_000)]
    )
    assert OptionsFlowDetector().run(ctx) == []


def test_volume_below_open_interest_is_filtered(state):
    """Volume <= OI suggests existing contracts changing hands, not new risk."""
    trade = make_option_trade(volume=500, open_interest=5_000)
    assert (
        OptionsFlowDetector().run(
            make_context(state, flow_settings(require_volume_gt_oi=True), option_trades=[trade])
        )
        == []
    )
    assert (
        len(
            OptionsFlowDetector().run(
                make_context(
                    state, flow_settings(require_volume_gt_oi=False), option_trades=[trade]
                )
            )
        )
        == 1
    )


def test_missing_oi_data_notes_that_the_filter_could_not_apply(state):
    trade = make_option_trade(volume=None, open_interest=None)
    ctx = make_context(state, flow_settings(), option_trades=[trade])
    assert len(OptionsFlowDetector().run(ctx)) == 1
    assert any("require_volume_gt_oi" in n for n in ctx.notes)


def test_long_dated_flow_is_excluded_by_default(state):
    ctx = make_context(
        state, flow_settings(), option_trades=[make_option_trade(days_out=900)]
    )
    assert OptionsFlowDetector().run(ctx) == []


def test_deep_itm_is_excluded(state):
    """A $50 strike call with the stock at $100 is 50% ITM — a stock substitute."""
    trade = make_option_trade(strike=50.0, underlying=100.0)
    assert (
        OptionsFlowDetector().run(
            make_context(state, flow_settings(exclude_deep_itm_pct=20.0), option_trades=[trade])
        )
        == []
    )
    assert (
        len(
            OptionsFlowDetector().run(
                make_context(
                    state, flow_settings(exclude_deep_itm_pct=0.0), option_trades=[trade]
                )
            )
        )
        == 1
    )


def test_trade_type_filter_respects_the_allow_list(state):
    trade = make_option_trade(trade_type="block")
    assert (
        OptionsFlowDetector().run(
            make_context(state, flow_settings(trade_types=["sweep"]), option_trades=[trade])
        )
        == []
    )


def test_unknown_trade_type_is_kept_rather_than_dropped(state):
    """A feed rename must not silently discard a $2M sweep."""
    trade = make_option_trade(trade_type="unknown", premium=2_000_000)
    ctx = make_context(state, flow_settings(trade_types=["sweep"]), option_trades=[trade])
    assert len(OptionsFlowDetector().run(ctx)) == 1


def test_flow_alert_does_not_claim_a_direction_the_feed_did_not_give(state):
    trade = make_option_trade()
    assert trade.side is Side.UNKNOWN
    ctx = make_context(state, flow_settings(), option_trades=[trade])
    joined = " ".join(OptionsFlowDetector().run(ctx)[0].lines)
    assert "not automatically directional" in joined


def test_sweep_outranks_an_equivalent_non_sweep(state):
    """Judged in one run, so neither the cooldown nor the watermark interferes."""
    alerts = OptionsFlowDetector().run(
        make_context(
            state,
            flow_settings(),
            option_trades=[
                make_option_trade(trade_type="sweep", raw_id="sweep"),
                make_option_trade(trade_type="block", raw_id="block"),
            ],
        )
    )
    by_id = {a.dedup_parts[0]: a.severity for a in alerts}
    assert by_id["sweep"].rank > by_id["block"].rank


# --------------------------------------------------------------------------
# Shared behaviour
# --------------------------------------------------------------------------
def test_cold_start_only_looks_back_a_bounded_window(state):
    """With no state, an old print must not be replayed as news."""
    stale = make_trade(minutes_ago=600, raw_id="old")
    ctx = make_context(state, dark_settings(), prints=[stale], adv=ADV)
    assert DarkPoolDetector().run(ctx) == []


def test_watermark_prevents_repeat_alerts(state):
    prints = [make_trade(size=50_000, price=100.0)]
    ctx = make_context(state, dark_settings(cooldown_minutes=0), prints=prints, adv=ADV)
    assert len(DarkPoolDetector().run(ctx)) == 1
    state.flush_progress()

    again = make_context(
        state, dark_settings(cooldown_minutes=0), prints=prints, adv=ADV, now=NOW
    )
    assert DarkPoolDetector().run(again) == []
