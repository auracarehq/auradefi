"""``EtherscanSource`` — both seams, the engine's window, keyless parity.

What this file has to prove, because a host cannot check it themselves
until bind time:

1. the object satisfies BOTH structural Protocols, so ``Auradefi`` accepts
   it (the module under test may not import either Protocol — ``sources``
   cannot import ``portfolio`` or ``embed`` — so conformance is asserted
   here or nowhere);
2. ``fetch_txlist`` sends the caller's window VERBATIM. The engine's budget
   and its short-page termination both depend on getting the page it asked
   for, so a widened window or a silently paged answer is a data-loss bug,
   not an inefficiency;
3. rows come back RAW, as the decoder seam requires;
4. keyless requests omit ``apikey`` entirely rather than sending it empty —
   the same wire shape ``EtherscanV2`` produces, byte for byte.
"""

from __future__ import annotations

import httpx
import pytest

from auradefi.embed.sync import PageFetcher
from auradefi.errors import CaipParseError, SourceError
from auradefi.money.quantity import Quantity
from auradefi.portfolio.holdings import BalanceSource
from auradefi.sources.evm.source import EtherscanSource

CHAIN = "eip155:1"
ADDRESS = "0x1111111111111111111111111111111111111111"
BASE = "https://api.etherscan.io/v2/api"


def _row(index: int) -> dict:
    return {
        "hash": "0x" + f"{index:02x}" * 32,
        "blockNumber": str(18_000_000 + index),
        "timeStamp": str(1_753_000_000 + index),
        "from": "0x" + "99" * 20,
        "to": ADDRESS,
        "value": "1000000000000000000",
        "gasUsed": "21000",
        "gasPrice": "10000000000",
        "isError": "0",
    }


def _client(handler) -> tuple[httpx.Client, list[httpx.Request]]:
    """A client recording every request, answering through ``handler``."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(recording)), seen


def _ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"status": "1", "message": "OK", "result": result})


class TestSeams:
    def test_satisfies_both_source_protocols(self):
        client, _ = _client(lambda request: _ok([]))
        source = EtherscanSource(client)
        # Exactly what Auradefi.__init__ checks before it will bind a host's
        # source; failing either half is a bind-time ValidationError.
        assert isinstance(source, BalanceSource)
        assert isinstance(source, PageFetcher)

    def test_from_key_owns_a_client_and_performs_no_io(self):
        source = EtherscanSource.from_key("KEY", timeout_s=2.5)
        assert isinstance(source, BalanceSource)
        assert isinstance(source, PageFetcher)
        assert source._client.timeout.read == 2.5

    def test_constructor_opens_no_connection(self):
        # The autouse socket guard in tests/conftest.py would turn any real
        # connection into a failure; constructing must not attempt one.
        client, seen = _client(lambda request: _ok([]))
        EtherscanSource(client, api_key="KEY")
        assert seen == []


class TestHistoryWindow:
    def test_sends_the_callers_window_verbatim(self):
        client, seen = _client(lambda request: _ok([_row(0), _row(1)]))
        source = EtherscanSource(client, api_key="KEY")

        rows = source.fetch_txlist(
            CHAIN, ADDRESS, start_block=17_000_000, end_block=18_000_500,
            page=3, offset=2, sort="desc",
        )

        assert len(rows) == 2
        (request,) = seen
        params = request.url.params
        assert params["startblock"] == "17000000"
        assert params["endblock"] == "18000500"
        assert params["page"] == "3"
        assert params["offset"] == "2"
        assert params["sort"] == "desc"
        assert params["chainid"] == "1"          # CAIP-2 -> numeric chain id
        assert params["action"] == "txlist"

    def test_one_call_is_exactly_one_request(self):
        """A full page must NOT be auto-paged: the engine owns the budget."""
        client, seen = _client(lambda request: _ok([_row(0), _row(1)]))
        source = EtherscanSource(client, api_key="KEY")

        source.fetch_txlist(CHAIN, ADDRESS, start_block=0, end_block=99_999_999,
                            page=1, offset=2, sort="asc")

        assert len(seen) == 1

    def test_rows_come_back_raw_for_the_decoder_seam(self):
        client, _ = _client(lambda request: _ok([_row(7)]))
        source = EtherscanSource(client)

        (row,) = source.fetch_txlist(CHAIN, ADDRESS, start_block=0,
                                     end_block=99_999_999, page=1, offset=10,
                                     sort="asc")

        assert isinstance(row, dict)
        assert row["blockNumber"] == "18000007"   # str, unparsed
        assert row["hash"].startswith("0x07")

    def test_empty_history_is_an_empty_page_not_an_error(self):
        body = {"status": "0", "message": "No transactions found", "result": []}
        client, _ = _client(lambda request: httpx.Response(200, json=body))

        assert EtherscanSource(client).fetch_txlist(
            CHAIN, ADDRESS, start_block=0, end_block=99_999_999, page=9,
            offset=10, sort="asc",
        ) == []

    def test_upstream_refusal_raises_source_error(self):
        body = {"status": "0", "message": "NOTOK", "result": "Invalid API Key"}
        client, _ = _client(lambda request: httpx.Response(200, json=body))
        source = EtherscanSource(client, api_key="WRONG")

        with pytest.raises(SourceError, match="NOTOK"):
            source.fetch_txlist(CHAIN, ADDRESS, start_block=0,
                                end_block=99_999_999, page=1, offset=10,
                                sort="asc")

    def test_a_non_caip2_chain_is_refused_before_any_request(self):
        client, seen = _client(lambda request: _ok([]))
        source = EtherscanSource(client)

        with pytest.raises(CaipParseError):
            source.fetch_txlist("ethereum", ADDRESS, start_block=0,
                                end_block=1, page=1, offset=10, sort="asc")
        assert seen == []


class TestKeylessParity:
    def test_no_key_omits_the_param_entirely(self):
        """`apikey=` is a DIFFERENT url from no apikey at all."""
        client, seen = _client(lambda request: _ok([]))

        EtherscanSource(client).fetch_txlist(
            CHAIN, ADDRESS, start_block=0, end_block=99_999_999, page=1,
            offset=10, sort="asc",
        )

        (request,) = seen
        assert "apikey" not in request.url.params

    def test_a_key_is_attached_last(self):
        client, seen = _client(lambda request: _ok([]))

        EtherscanSource(client, api_key="KEY").fetch_txlist(
            CHAIN, ADDRESS, start_block=0, end_block=99_999_999, page=1,
            offset=10, sort="asc",
        )

        (request,) = seen
        assert list(request.url.params)[-1] == "apikey"
        assert request.url.params["apikey"] == "KEY"


class TestBalances:
    def test_delegates_to_etherscan_v2(self):
        """One native balance, no tokens — proving the delegation, not the
        balance logic, which tests/sources/evm/test_etherscan.py owns."""
        def handler(request: httpx.Request) -> httpx.Response:
            action = request.url.params["action"]
            if action == "balance":
                return _ok("2000000000000000000")
            if action == "tokentx":
                return httpx.Response(200, json={
                    "status": "0", "message": "No transactions found", "result": []})
            raise AssertionError(f"unexpected action {action!r}")

        client, seen = _client(handler)

        (native,) = EtherscanSource(client).balances(CHAIN, ADDRESS)

        assert native.caip19 == "eip155:1/slip44:60"
        assert native.quantity == Quantity(2 * 10**18, 18)
        assert native.contract_address is None
        assert [request.url.params["action"] for request in seen] == [
            "balance", "tokentx",
        ]

    def test_base_url_override_reaches_both_seams(self):
        client, seen = _client(lambda request: _ok([]))
        source = EtherscanSource(client, base_url="https://proxy.example/v2/api")

        source.fetch_txlist(CHAIN, ADDRESS, start_block=0, end_block=1,
                            page=1, offset=10, sort="asc")

        assert seen[0].url.host == "proxy.example"
