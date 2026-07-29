"""Sources: redaction, retry policy, parsing, and the no-lookahead rule."""

from __future__ import annotations

import json
import xml.etree.ElementTree as XET
from datetime import date, datetime, timedelta

import pytest
import responses

from monitor.clock import ET
from monitor.config import Config
from monitor.models import Bar
from monitor.sources import build, missing_credentials
from monitor.sources.base import (BadResponse, EmptyResponse, HttpClient, NotConfigured,
                                  Unreachable, redact)
from monitor.sources.fmp import FmpSource, _parse_bars
from monitor.sources.ibkr import IbkrSource, _month_start, _number, _option_months
from monitor.sources.replay import ReplaySource, write_bars, write_chain
from monitor.sources.sec import parse_form4

from conftest import FIXTURES, make_bars


class TestRedaction:
    def test_an_api_key_never_survives(self):
        assert "ZnWtVnJg" not in redact("https://x/api?apikey=aB3xQ7fake&sym=NVDA")
        assert "apikey=***" in redact("https://x/api?apikey=aB3xQ7fake")

    def test_every_named_secret_parameter_is_covered(self):
        for name in ("apikey", "api_key", "token", "access_token", "key", "secret", "password"):
            assert redact(f"?{name}=supersecret") == f"?{name}=***"

    def test_a_telegram_token_in_a_path_is_stripped(self):
        got = redact("POST https://api.telegram.org/bot1234567890:AAFakeTokenValue/sendMessage")
        assert "1234567890" not in got and "/bot***" in got

    def test_an_authorization_header_is_stripped(self):
        assert redact("Authorization: Bearer sk-ant-abc123") == "Authorization: Bearer ***"

    def test_ordinary_text_is_untouched(self):
        assert redact("NVDA volume 3.4x normal") == "NVDA volume 3.4x normal"


class TestHttpClient:
    @responses.activate
    def test_json_comes_back(self):
        responses.get("https://x/data", json={"ok": True})
        assert HttpClient("t", sleeper=lambda s: None).get_json("https://x/data") == {"ok": True}

    @responses.activate
    def test_a_transient_status_is_retried_then_succeeds(self):
        responses.get("https://x/data", status=503)
        responses.get("https://x/data", json={"ok": True})
        client = HttpClient("t", retries=2, sleeper=lambda s: None)
        assert client.get_json("https://x/data") == {"ok": True}
        assert len(responses.calls) == 2

    @responses.activate
    def test_retries_are_bounded_and_then_reported(self):
        responses.get("https://x/data", status=503)
        client = HttpClient("t", retries=2, sleeper=lambda s: None)
        with pytest.raises(Unreachable, match="3 attempts"):
            client.get_json("https://x/data")
        assert len(responses.calls) == 3

    @responses.activate
    def test_a_bad_key_is_not_retried(self):
        """401 does not become 200 on the third try, and hammering it gets keys suspended."""
        responses.get("https://x/data", status=401, body="bad key")
        client = HttpClient("t", retries=3, sleeper=lambda s: None)
        with pytest.raises(Unreachable):
            client.get_json("https://x/data")
        assert len(responses.calls) == 1

    @responses.activate
    def test_html_where_json_was_promised_is_corrupt_not_unreachable(self):
        responses.get("https://x/data", body="<html>maintenance</html>")
        with pytest.raises(BadResponse):
            HttpClient("t", retries=0, sleeper=lambda s: None).get_json("https://x/data")

    @responses.activate
    def test_the_failure_message_carries_no_key(self):
        responses.get("https://x/data", status=500)
        client = HttpClient("t", retries=0, sleeper=lambda s: None)
        with pytest.raises(Unreachable) as caught:
            client.get_json("https://x/data", {"apikey": "SECRETVALUE"})
        assert "SECRETVALUE" not in str(caught.value)


class TestFmp:
    def test_bars_parse_and_sort_oldest_first(self):
        payload = [
            {"date": "2026-07-24 11:00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
            {"date": "2026-07-24 10:30:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 20},
        ]
        bars = _parse_bars(payload, "NVDA", "fmp")
        assert [b.ts.hour * 60 + b.ts.minute for b in bars] == [630, 660]

    def test_bar_timestamps_are_eastern_not_utc(self):
        bars = _parse_bars(
            [{"date": "2026-07-24 09:30:00", "open": 1, "high": 1, "low": 1,
              "close": 1, "volume": 1}], "NVDA", "fmp")
        assert bars[0].ts.tzinfo is ET and bars[0].ts.hour == 9

    def test_a_plan_error_returned_as_200_is_surfaced(self):
        """FMP reports entitlement problems in the body, not the status line."""
        payload = {"Error Message": "Exclusive Endpoint: This endpoint is not available"}
        with pytest.raises(BadResponse, match="Exclusive Endpoint"):
            _parse_bars(payload, "NVDA", "fmp")

    def test_an_empty_list_is_empty_not_corrupt(self):
        with pytest.raises(EmptyResponse):
            _parse_bars([], "NVDA", "fmp")

    def test_a_malformed_row_is_reported_with_the_row(self):
        with pytest.raises(BadResponse, match="malformed"):
            _parse_bars([{"date": "2026-07-24 09:30:00"}], "NVDA", "fmp")

    def test_an_unsupported_interval_is_refused_rather_than_substituted(self):
        source = FmpSource("key", HttpClient("fmp"))
        with pytest.raises(NotConfigured, match="no 7-minute interval"):
            source.bars("NVDA", 7, 10)

    def test_a_missing_key_is_named(self):
        source = FmpSource(None, HttpClient("fmp"))
        with pytest.raises(NotConfigured, match="FMP_API_KEY"):
            source.bars("NVDA", 30, 10)


class TestIbkrHelpers:
    @pytest.mark.parametrize("raw,expected", [
        ("5.25", 5.25), ("C5.25", 5.25), (5, 5.0), ("1,234", 1234.0),
        ("1.2M", 1_200_000.0), ("3K", 3000.0),
    ])
    def test_numbers_parse_through_the_gateway_decorations(self, raw, expected):
        assert _number(raw) == expected

    def test_junk_is_none_not_zero(self):
        """Zero open interest is a real value; junk must not masquerade as it."""
        assert _number("n/a") is None
        assert _number(None) is None
        assert _number("") is None
        assert _number(0) == 0.0

    def test_option_months_are_split(self):
        sections = [{"secType": "STK"}, {"secType": "OPT", "months": "JUL26;AUG26;SEP26"}]
        assert _option_months(sections) == ["JUL26", "AUG26", "SEP26"]

    def test_a_missing_opt_section_yields_nothing(self):
        assert _option_months([{"secType": "STK"}]) == []

    def test_month_tokens_become_dates(self):
        assert _month_start("AUG26") == date(2026, 8, 1)
        assert _month_start("nonsense") is None
        assert _month_start("XXX26") is None


class TestIbkrSource:
    def _source(self, **kwargs):
        return IbkrSource(HttpClient("ibkr", retries=0, sleeper=lambda s: None),
                          base_url="https://gw/v1/api", sleeper=lambda s: None, **kwargs)

    @responses.activate
    def test_an_unauthenticated_gateway_is_reported_clearly(self):
        responses.get("https://gw/v1/api/iserver/auth/status", json={"authenticated": False})
        with pytest.raises(Unreachable, match="not authenticated"):
            self._source().check_auth()

    @responses.activate
    def test_a_competing_session_is_reported(self):
        responses.get("https://gw/v1/api/iserver/auth/status",
                      json={"authenticated": True, "competing": True})
        with pytest.raises(Unreachable, match="competing|taken over"):
            self._source().check_auth()

    @responses.activate
    def test_bar_volume_is_scaled_out_of_hundreds(self):
        responses.get("https://gw/v1/api/iserver/auth/status", json={"authenticated": True})
        responses.get("https://gw/v1/api/iserver/secdef/search",
                      json=[{"symbol": "NVDA", "conid": "4815747"}])
        responses.get("https://gw/v1/api/iserver/marketdata/history", json={
            "data": [{"t": 1785333000000, "o": 204.0, "h": 205.0, "l": 203.0,
                      "c": 204.5, "v": 142652.24}]})
        bars = self._source().bars("NVDA", 30, 10)
        assert bars[0].volume == 14_265_224

    @responses.activate
    def test_an_empty_history_says_why(self):
        responses.get("https://gw/v1/api/iserver/auth/status", json={"authenticated": True})
        responses.get("https://gw/v1/api/iserver/secdef/search",
                      json=[{"symbol": "NVDA", "conid": 1}])
        responses.get("https://gw/v1/api/iserver/marketdata/history", json={"data": []})
        with pytest.raises(EmptyResponse, match="gateway session|subscription"):
            self._source().bars("NVDA", 30, 10)

    @responses.activate
    def test_an_unknown_symbol_is_empty_not_corrupt(self):
        responses.get("https://gw/v1/api/iserver/secdef/search", json=[])
        with pytest.raises(EmptyResponse):
            self._source().conid("ZZZZ")

    @responses.activate
    def test_the_conid_is_cached_after_the_first_lookup(self):
        responses.get("https://gw/v1/api/iserver/secdef/search",
                      json=[{"symbol": "NVDA", "conid": 4815747}])
        source = self._source()
        assert source.conid("NVDA") == source.conid("nvda") == 4815747
        assert len(responses.calls) == 1

    def test_an_unsupported_bar_size_is_refused(self):
        with pytest.raises(BadResponse, match="no 7-minute bar"):
            self._source().bars("NVDA", 7, 10)


class TestForm4:
    def test_a_purchase_parses(self):
        root = XET.parse(FIXTURES / "form4_purchase.xml").getroot()
        trades = parse_form4(root, ticker="NVDA", accession="a-1", filed_on=date(2026, 7, 29))
        assert len(trades) == 1
        trade = trades[0]
        assert trade.is_purchase and trade.shares == 12_000 and trade.price == 205.44
        assert trade.title == "Chief Financial Officer"
        assert trade.is_officer and not trade.is_director
        assert trade.value == pytest.approx(2_465_280)
        assert trade.filing_lag_days == 2

    def test_a_grant_is_not_reported_as_a_purchase(self):
        """Code A is compensation vesting, not a decision about the stock."""
        root = XET.parse(FIXTURES / "form4_purchase.xml").getroot()
        trades = parse_form4(root, ticker="NVDA", accession="a-1", filed_on=date(2026, 7, 29))
        assert all(t.code == "P" for t in trades)

    def test_a_planned_sale_is_detected_from_the_footnote(self):
        root = XET.parse(FIXTURES / "form4_planned_sale.xml").getroot()
        trade = parse_form4(root, ticker="MSFT", accession="a-2",
                            filed_on=date(2026, 7, 29))[0]
        assert trade.is_open_market_sale and trade.planned_10b5_1
        assert trade.title == "Director"

    def test_a_transaction_dated_after_the_filing_is_rejected(self):
        """A negative filing lag is impossible, and reads as nonsense in an alert."""
        root = XET.parse(FIXTURES / "form4_purchase.xml").getroot()
        trades = parse_form4(root, ticker="NVDA", accession="a-1",
                             filed_on=date(2026, 7, 20))
        assert trades == []

    def test_a_document_with_no_transactions_yields_nothing(self):
        root = XET.fromstring("<ownershipDocument><reportingOwner/></ownershipDocument>")
        assert parse_form4(root, ticker="NVDA", accession="a", filed_on=date(2026, 7, 29)) == []


class TestReplayNoLookahead:
    def test_bars_after_as_of_are_withheld(self, replay_dir):
        cutoff = datetime(2026, 7, 23, 11, 0, tzinfo=ET)
        source = ReplaySource(replay_dir, as_of=cutoff)
        bars = source.bars("NVDA", 30, 8)
        assert bars, "expected some visible bars"
        assert all(bar.ts <= cutoff for bar in bars)
        assert source.hidden_bars("NVDA") > 0

    def test_without_as_of_everything_is_visible(self, replay_dir):
        source = ReplaySource(replay_dir)
        assert source.hidden_bars("NVDA") == 0 or True
        assert len(source.bars("NVDA", 30, 8)) > 0
        assert source.hidden_bars("NVDA") == 0

    def test_an_as_of_before_all_data_is_empty_not_silent(self, replay_dir):
        source = ReplaySource(replay_dir, as_of=datetime(2020, 1, 1, tzinfo=ET))
        with pytest.raises(EmptyResponse, match="later than as_of"):
            source.bars("NVDA", 30, 8)

    def test_the_chain_uses_the_newest_snapshot_at_or_before_as_of(self, replay_dir):
        source = ReplaySource(replay_dir, as_of=datetime(2026, 7, 23, 16, 0, tzinfo=ET))
        chain = source.chain("NVDA", 120)
        assert chain.as_of == "2026-07-23"
        assert chain.contracts[0].open_interest == 34_051

    def test_the_chain_carries_the_earlier_snapshot_as_its_baseline(self, replay_dir):
        chain = ReplaySource(replay_dir).chain("NVDA", 120)
        assert chain.as_of == "2026-07-24"
        assert chain.has_baseline
        assert chain.oi_change(chain.contracts[0]) == 52_400 - 34_051

    def test_filings_after_as_of_are_withheld(self, replay_dir):
        source = ReplaySource(replay_dir, as_of=datetime(2026, 7, 22, tzinfo=ET))
        assert source.filings("NVDA", date(2026, 1, 1)) == []

    def test_a_missing_capture_is_empty_with_the_ticker_named(self, replay_dir):
        with pytest.raises(EmptyResponse, match="TSLA"):
            ReplaySource(replay_dir).bars("TSLA", 30, 8)

    def test_a_missing_directory_is_reported_at_construction(self, tmp_path):
        with pytest.raises(NotConfigured, match="capture"):
            ReplaySource(tmp_path / "absent")

    def test_corrupt_json_is_reported_as_corrupt(self, replay_dir):
        (replay_dir / "bars" / "BAD.json").write_text("{not json")
        with pytest.raises(BadResponse):
            ReplaySource(replay_dir).bars("BAD", 30, 8)


class TestReplayGridFormat:
    def _grid(self, tmp_path):
        root = tmp_path / "replay"
        (root / "bars").mkdir(parents=True)
        (root / "bars" / "NVDA.json").write_text(json.dumps({
            "sessions": ["2026-07-23", "2026-07-24"],
            "slots": ["13:30", "14:00"],            # UTC, per the vendor export
            "volume": [10, 20, 30, 40],
            "open": [1, 1, 1, 1], "high": [2, 2, 2, 2],
            "low": [0.5, 0.5, 0.5, 0.5], "close": [1.5, 1.5, 1.5, 1.5],
        }))
        return root

    def test_utc_slots_are_converted_to_eastern(self, tmp_path):
        bars = ReplaySource(self._grid(tmp_path)).bars("NVDA", 30, 2)
        assert len(bars) == 4
        assert bars[0].ts.hour == 9 and bars[0].ts.minute == 30

    def test_a_ragged_grid_is_reported_rather_than_half_read(self, tmp_path):
        root = self._grid(tmp_path)
        payload = json.loads((root / "bars" / "NVDA.json").read_text())
        payload["volume"] = [10, 20, 30]
        (root / "bars" / "NVDA.json").write_text(json.dumps(payload))
        with pytest.raises(BadResponse, match="ragged"):
            ReplaySource(root).bars("NVDA", 30, 2)


class TestCapture:
    def test_written_bars_read_back_identically(self, tmp_path):
        bars = make_bars(sessions=3)
        path = tmp_path / "bars" / "NVDA.json"
        write_bars(path, "NVDA", 30, bars)
        again = ReplaySource(tmp_path).bars("NVDA", 30, 3)
        assert [b.ts for b in again] == [b.ts for b in bars]
        assert [b.volume for b in again] == [b.volume for b in bars]

    def test_writing_a_chain_keeps_earlier_snapshots(self, tmp_path):
        from conftest import make_chain
        path = tmp_path / "options" / "NVDA.json"
        write_chain(path, make_chain(as_of="2026-07-23"))
        write_chain(path, make_chain(as_of="2026-07-24"))
        payload = json.loads(path.read_text())
        assert set(payload["snapshots"]) == {"2026-07-23", "2026-07-24"}


class TestBuild:
    def test_a_missing_credential_becomes_an_issue_not_an_exception(self, config):
        config.values["sources.bars"] = "fmp"
        config.values["sources.insider"] = "sec"
        config.values["sources.options"] = "off"
        config.values["canslim.enabled"] = False
        with build(config, env={}) as sources:
            assert sources.bars is not None          # FMP fails later, on use
            assert sources.insider is None           # SEC needs a user agent up front
            assert any("SEC_USER_AGENT" in i.detail for i in sources.issues)

    def test_off_means_no_source_and_no_complaint(self, config):
        for role in ("options", "insider", "trades"):
            config.values[f"sources.{role}"] = "off"
        config.values["sources.bars"] = "fmp"
        config.values["canslim.enabled"] = False
        with build(config, env={"FMP_API_KEY": "k"}) as sources:
            assert sources.options is sources.insider is sources.trades is None
            assert sources.issues == []

    def test_fundamentals_are_wired_even_when_bars_come_from_ibkr(self, config):
        config.values["sources.bars"] = "ibkr"
        config.values["sources.options"] = "off"
        config.values["sources.insider"] = "off"
        config.values["canslim.enabled"] = True
        with build(config, env={"FMP_API_KEY": "k"}) as sources:
            assert sources.fundamentals is not None
            assert sources.fundamentals.name == "fmp"

    def test_canslim_without_a_key_is_reported(self, config):
        config.values["sources.bars"] = "ibkr"
        config.values["sources.options"] = "off"
        config.values["sources.insider"] = "off"
        with build(config, env={}) as sources:
            assert sources.fundamentals is None
            assert any("CAN SLIM" in i.detail for i in sources.issues)


class TestMissingCredentials:
    def test_fmp_is_not_demanded_when_bars_come_from_ibkr(self, config):
        config.values["sources.bars"] = "ibkr"
        config.values["sources.insider"] = "off"
        config.values["notify.channel"] = "console"
        config.values["canslim.enabled"] = False
        assert missing_credentials(config, env={}) == []

    def test_the_narrator_key_is_only_demanded_when_the_narrator_is_on(self, config):
        config.values["sources.bars"] = "ibkr"
        config.values["sources.insider"] = "off"
        config.values["notify.channel"] = "console"
        config.values["canslim.enabled"] = True
        config.values["canslim.narrator"] = "off"
        assert missing_credentials(config, env={"FMP_API_KEY": "k"}) == []
        config.values["canslim.narrator"] = "llm"
        assert missing_credentials(config, env={"FMP_API_KEY": "k"}) == ["ANTHROPIC_API_KEY"]

    def test_telegram_credentials_are_demanded_for_the_telegram_channel(self, config):
        config.values["sources.bars"] = "ibkr"
        config.values["sources.insider"] = "off"
        config.values["canslim.enabled"] = False
        config.values["notify.channel"] = "telegram"
        assert missing_credentials(config, env={}) == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
