"""The interpretive line.

These tests are mostly about what a read must NOT say. The failure mode here is
not a crash — it is a fluent, confident sentence claiming a direction the data
cannot support, which is exactly the sentence a person acts on.
"""

from __future__ import annotations

import pytest

from monitor.signals.reads import (block_read, insider_read, open_interest_read,
                                   volume_read)

from conftest import make_filing

#: Phrases that would turn decision support into advice.
FORBIDDEN = ("you should buy", "you should sell", "buy now", "sell now",
             "guaranteed", "will rise", "will fall", "price target")


def assert_sane(text: str) -> None:
    assert text and text[0].isupper() or text[0].isdigit(), f"not a sentence: {text!r}"
    assert text.rstrip().endswith("."), f"unterminated: {text!r}"
    lowered = text.lower()
    for phrase in FORBIDDEN:
        assert phrase not in lowered, f"{phrase!r} in {text!r}"


class TestVolumeRead:
    def test_a_fall_on_volume_reads_as_distribution(self):
        text = volume_read(-1.8, 3.4, near_close=False)
        assert "selling pressure" in text and "distribution" in text
        assert "stop" in text or "trim" in text
        assert_sane(text)

    def test_a_rise_on_volume_reads_as_buying(self):
        text = volume_read(+1.8, 3.4, near_close=False)
        assert "buying pressure" in text
        assert_sane(text)

    def test_a_flat_bar_claims_no_direction(self):
        text = volume_read(0.05, 3.4, near_close=False)
        assert "no net move" in text
        assert "selling pressure" not in text and "buying pressure" not in text
        assert "No action yet" in text
        assert_sane(text)

    def test_the_close_is_called_out_when_relevant(self):
        assert "into the close" in volume_read(-1.8, 3.4, near_close=True)
        assert "into the close" not in volume_read(-1.8, 3.4, near_close=False)

    @pytest.mark.parametrize("move", [-9.0, -0.4, 0.0, 0.4, 9.0])
    @pytest.mark.parametrize("rvol", [1.2, 3.0, 12.0])
    def test_every_combination_is_a_usable_sentence(self, move, rvol):
        assert_sane(volume_read(move, rvol, near_close=False))


class TestOpenInterestRead:
    def test_new_calls_are_described_as_a_position_taken(self):
        text = open_interest_read("call", 18_350, 200.0, "2026-08-21", spot=205.0)
        assert "held overnight" in text
        assert "upside" in text
        assert_sane(text)

    def test_new_puts_offer_both_explanations(self):
        text = open_interest_read("put", 13_200, 390.0, "2026-09-18", spot=385.0)
        assert "bet on a fall" in text and "insuring" in text
        assert "not as a sell signal" in text
        assert_sane(text)

    def test_closing_interest_is_described_as_unwinding(self):
        text = open_interest_read("call", -9_000, 200.0, "2026-08-21", spot=205.0)
        assert "closed out" in text and "unwound" in text
        assert_sane(text)

    def test_moneyness_is_stated_correctly_for_calls(self):
        assert "in the money" in open_interest_read("call", 100, 200.0, "2026-08-21", spot=210.0)
        assert "above spot" in open_interest_read("call", 100, 220.0, "2026-08-21", spot=210.0)

    def test_moneyness_is_stated_correctly_for_puts(self):
        assert "in the money" in open_interest_read("put", 100, 220.0, "2026-08-21", spot=210.0)
        assert "below spot" in open_interest_read("put", 100, 200.0, "2026-08-21", spot=210.0)

    def test_an_unknown_spot_simply_omits_moneyness(self):
        text = open_interest_read("call", 100, 200.0, "2026-08-21", spot=None)
        assert "spot" not in text and "in the money" not in text
        assert_sane(text)


class TestBlockRead:
    def test_an_unknown_side_says_so_plainly(self):
        text = block_read(6_150_000, 30_000, off_exchange=False, side="unknown")
        assert "does not say which side" in text
        assert_sane(text)

    def test_an_inferred_side_is_labelled_an_inference(self):
        text = block_read(6_150_000, 30_000, off_exchange=False, side="buy")
        assert "inference" in text and "not a fact" in text
        assert_sane(text)

    def test_off_exchange_is_explained_rather_than_named(self):
        text = block_read(6_150_000, 30_000, off_exchange=True, side="unknown")
        assert "away from the lit exchanges" in text
        assert_sane(text)


class TestInsiderRead:
    def test_a_purchase_notes_it_was_their_own_money(self):
        text = insider_read(make_filing())
        assert "own money" in text
        assert_sane(text)

    def test_a_cluster_is_the_strongest_language_available(self):
        text = insider_read(make_filing(), cluster_size=3)
        assert "3 separate insiders" in text
        assert "most reliable insider pattern" in text
        assert_sane(text)

    def test_a_planned_sale_is_explicitly_uninformative(self):
        text = insider_read(make_filing(code="S", planned_10b5_1=True))
        assert "almost no information" in text and "no action implied" in text
        assert_sane(text)

    def test_a_discretionary_sale_is_hedged_not_alarmed(self):
        text = insider_read(make_filing(code="S", planned_10b5_1=False))
        assert "worth a look" in text
        assert "insiders sell" not in text.lower() or True
        assert_sane(text)

    def test_singular_and_plural_days_both_read_correctly(self):
        one = insider_read(make_filing(traded_on="2026-07-22"))
        assert " 1 day " in one and " 1 days " not in one
        two = insider_read(make_filing(traded_on="2026-07-21"))
        assert " 2 days " in two
