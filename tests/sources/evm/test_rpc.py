"""Contract tests for the EVM JSON-RPC transport (RELEASE_0.2.0 §4, #3).

Every request in this file is served by an ``httpx.MockTransport``, so the
autouse socket guard in ``tests/conftest.py`` is never bypassed and no
cassette is needed: the shapes under test here are wire envelopes and
malformed-envelope refusals, which no recording contains.

The headline pin is the reversed-id batch. DECISIONS.md pins "JSON-RPC
POSTs share one cassette key, so recorded order IS the wire contract",
which is exactly why a batch may NOT be read positionally: one key serves
one recorded array, and a compliant node is free to answer that array in
any order. The reversed-id test below returns the three responses in id
order 3, 2, 1 with three distinguishable results, so an implementation
that zips the response array against the request array returns them
backwards and fails.

Golden values, each derived by hand and hardcoded:

  block_tag(20_450_000) -> "0x1380ad0"   (16777216+3145728+524288+2560+208)
  "0x1bc16d674ec80000"  -> 2000000000000000000 wei, which is 2 ETH
  "0x108718fee8b39a8f34e" -> 4878123456789012345678, a value that does NOT
      survive int(float(x)), so the equality mechanically fails any
      implementation that parses an amount through float (SPEC rules #1/#2).
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import json
import sys
from pathlib import Path

import httpx
import pytest

from auradefi.errors import SourceError, ValidationError
from auradefi.sources.evm.rpc import BatchResult, EvmRpc, block_tag

URL = "https://node.example.invalid/v1"

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_CHECKSUMMED = "0xA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"
VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

# keccak256("decimals()")[:4], the smallest real selector there is.
DECIMALS = "0x313ce567"
BALANCE_OF = "0x70a08231000000000000000000000000d8da6bf26964af9d7eed9e03e53415d37aa96045"

# One 32-byte word each, so the three batch results are distinguishable.
WORD_SIX = "0x0000000000000000000000000000000000000000000000000000000000000006"
WORD_BALANCE = "0x000000000000000000000000000000000000000000000042ed123b0bd8203a14"
HEAD = "0x1380ad0"

CALL_A = {"to": USDC, "data": DECIMALS}
CALL_B = {"to": DAI, "data": BALANCE_OF}

#: The exact object ``eth_call`` must put on the wire for the acceptance
#: vector. The wave-4 golden fixture keys on this params array, so key
#: order, address casing and an added "from" key are all wire contract.
PINNED_ETH_CALL_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "eth_call",
    "params": [{"to": USDC, "data": DECIMALS}, "latest"],
}

LOG_FILTER = {
    "fromBlock": "0x1380ad0",
    "toBlock": "0x1380ad0",
    "address": [USDC_CHECKSUMMED],
    "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"],
    # logs.py owns the filter keys, so rpc.py forwards even a key no node
    # defines instead of validating one it does not own.
    "aKeyNoNodeDefines": 7,
}

LOG_ROWS = [
    {
        "address": USDC_CHECKSUMMED,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            "0x000000000000000000000000d8da6bf26964af9d7eed9e03e53415d37aa96045",
        ],
        "data": "0x00000000000000000000000000000000000000000000000000000000000f4240",
        "blockNumber": "0x1380ad0",
        "logIndex": "0x2a",
        "removed": False,
        "aFieldNobodyModels": {"nested": [1, 2, 3]},
    },
    {
        "address": DAI,
        "topics": [],
        "data": "0x",
        "blockNumber": "0x1380ad1",
        "logIndex": "0x2b",
        "removed": True,
    },
]


def _scripted_client(*responses: object) -> tuple[httpx.Client, list[httpx.Request]]:
    """A client replaying ``responses`` in order; the last one repeats.

    Each entry is a JSON-serialisable body, a ``(status, body)`` pair, or
    an exception INSTANCE to raise instead of responding. A ``str`` body
    is sent as text, which is how the non-JSON case is built.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        spec = responses[min(len(seen) - 1, len(responses) - 1)]
        if isinstance(spec, BaseException):
            raise spec
        status, body = spec if isinstance(spec, tuple) else (200, spec)
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def _tripwire_client() -> tuple[httpx.Client, list[httpx.Request]]:
    """A client that records and refuses every request: proves ZERO HTTP."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise RuntimeError("HTTP attempted where the contract forbids it")

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def _ok(result: object, id_: int = 1) -> dict:
    """A well-formed JSON-RPC success envelope carrying ``result``."""
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _bodies(seen: list[httpx.Request]) -> list:
    return [json.loads(request.content) for request in seen]


def _rpc(*responses: object) -> tuple[EvmRpc, list[httpx.Request]]:
    client, seen = _scripted_client(*responses)
    return EvmRpc(client, URL), seen


class TestInterface:
    # pins: the client is a required injected argument and the node url is
    #       required too, so no EvmRpc can be built with a hidden endpoint
    def test_constructor_takes_a_required_client_and_a_required_url(self):
        params = inspect.signature(EvmRpc.__init__).parameters
        assert list(params) == ["self", "client", "url"]
        assert params["client"].default is inspect.Parameter.empty
        assert params["url"].default is inspect.Parameter.empty

    # pins: the two block-scoped reads default their block parameter to the
    #       string 'latest', so a caller that omits it reads the head
    def test_block_defaults_to_latest_on_the_block_scoped_reads(self):
        call = inspect.signature(EvmRpc.eth_call).parameters
        assert list(call) == ["self", "to", "data", "block"]
        assert call["block"].default == "latest"
        balance = inspect.signature(EvmRpc.eth_get_balance).parameters
        assert list(balance) == ["self", "address", "block"]
        assert balance["block"].default == "latest"

    # pins: the remaining public reads take exactly their documented
    #       arguments, so a caller cannot pass a block to a headless read
    def test_the_remaining_public_reads_take_their_documented_arguments(self):
        assert list(inspect.signature(EvmRpc.eth_block_number).parameters) == ["self"]
        assert list(inspect.signature(EvmRpc.eth_get_logs).parameters) == [
            "self",
            "filter_object",
        ]
        assert list(inspect.signature(EvmRpc.batch).parameters) == ["self", "requests"]

    # pins: block_tag is a module-level function, not a method, so logs.py,
    #       multicall.py and reader.py can build a tag without an EvmRpc
    def test_block_tag_is_a_module_level_function(self):
        assert inspect.isfunction(block_tag)
        assert list(inspect.signature(block_tag).parameters) == ["block_number"]
        assert not hasattr(EvmRpc, "block_tag")


class TestNoConstructionIO:
    # pins: the constructor performs no I/O, so building an EvmRpc against a
    #       transport that refuses every request still succeeds
    def test_constructing_against_a_hostile_transport_issues_no_request(self):
        client, seen = _tripwire_client()
        rpc = EvmRpc(client, URL)
        assert isinstance(rpc, EvmRpc)
        assert seen == []


class TestEthCallWireShape:
    # pins: eth_call posts the pinned envelope byte for byte, lowercasing the
    #       target address and leaving the calldata verbatim
    def test_eth_call_posts_the_pinned_request_body(self):
        rpc, seen = _rpc(_ok(WORD_SIX))
        rpc.eth_call(USDC_CHECKSUMMED, DECIMALS)
        assert _bodies(seen) == [PINNED_ETH_CALL_BODY]

    # pins: the envelope and the call object keep their declared key order,
    #       which the wave-4 golden fixture keys on
    def test_eth_call_request_body_key_order_is_the_wire_contract(self):
        rpc, seen = _rpc(_ok(WORD_SIX))
        rpc.eth_call(USDC_CHECKSUMMED, DECIMALS)
        body = _bodies(seen)[0]
        assert list(body) == ["jsonrpc", "id", "method", "params"]
        assert list(body["params"][0]) == ["to", "data"]

    # pins: the call object carries exactly 'to' and 'data', so no 'from',
    #       'gas' or 'value' key creeps into the fixture key
    def test_eth_call_object_carries_only_to_and_data(self):
        rpc, seen = _rpc(_ok(WORD_SIX))
        rpc.eth_call(USDC, DECIMALS)
        assert set(_bodies(seen)[0]["params"][0]) == {"to", "data"}

    # pins: an explicit block tag lands in the second params slot, so a
    #       historical read is not silently served from the head
    def test_an_explicit_block_tag_is_the_second_params_element(self):
        rpc, seen = _rpc(_ok(WORD_SIX))
        rpc.eth_call(USDC, DECIMALS, block="0x1380ad0")
        assert _bodies(seen)[0]["params"] == [
            {"to": USDC, "data": DECIMALS},
            "0x1380ad0",
        ]

    # pins: every single call carries id 1, batching being the only place
    #       ids climb
    def test_every_single_call_carries_id_one(self):
        rpc, seen = _rpc(_ok(WORD_SIX), _ok(WORD_BALANCE))
        rpc.eth_call(USDC, DECIMALS)
        rpc.eth_call(DAI, BALANCE_OF)
        assert [body["id"] for body in _bodies(seen)] == [1, 1]

    # pins: the result hex string comes back verbatim, with no case folding
    #       and no padding change: decoding the word is abi.py's job
    def test_eth_call_returns_the_result_string_verbatim(self):
        mixed = "0x0000000000000000000000000000000000000000000000000000000000000AbC"
        rpc, _ = _rpc(_ok(mixed))
        assert rpc.eth_call(USDC, DECIMALS) == mixed

    # pins: an eth_call result that is not a string is a malformed envelope
    #       and raises rather than being handed on as a non-str
    @pytest.mark.parametrize("result", [6, None, ["0x6"], {"value": "0x6"}, True])
    def test_a_non_string_eth_call_result_raises_source_error(self, result):
        rpc, _ = _rpc(_ok(result))
        with pytest.raises(SourceError):
            rpc.eth_call(USDC, DECIMALS)


class TestEthGetBalance:
    # pins: eth_getBalance posts the address and the block tag as its params
    def test_posts_the_address_and_the_block_tag(self):
        rpc, seen = _rpc(_ok("0x1bc16d674ec80000"))
        rpc.eth_get_balance(VITALIK)
        body = _bodies(seen)[0]
        assert body["method"] == "eth_getBalance"
        assert body["params"] == [VITALIK, "latest"]
        assert body["id"] == 1 and body["jsonrpc"] == "2.0"

    # pins: an explicit block tag is forwarded, so a historical balance is
    #       not served from the head
    def test_an_explicit_block_tag_is_forwarded(self):
        rpc, seen = _rpc(_ok("0x1bc16d674ec80000"))
        rpc.eth_get_balance(VITALIK, block="0x1380ad0")
        assert _bodies(seen)[0]["params"] == [VITALIK, "0x1380ad0"]

    # pins: a wei hex string parses with int(x, 16) to the exact integer
    def test_parses_the_wei_hex_string_as_base_sixteen(self):
        rpc, _ = _rpc(_ok("0x1bc16d674ec80000"))
        assert rpc.eth_get_balance(VITALIK) == 2_000_000_000_000_000_000

    # pins: a balance too large for a float survives exactly, so no
    #       implementation may route the parse through float
    def test_a_float_hostile_balance_survives_exactly(self):
        rpc, _ = _rpc(_ok("0x108718fee8b39a8f34e"))
        assert rpc.eth_get_balance(VITALIK) == 4878123456789012345678

    # pins: a zero balance is 0 and not an error, so an empty account reads
    #       as held-nothing rather than as a failure
    def test_a_zero_balance_parses_to_zero(self):
        rpc, _ = _rpc(_ok("0x0"))
        assert rpc.eth_get_balance(VITALIK) == 0

    # pins: a JSON integer result is a malformed envelope, because a raw
    #       on-chain amount is a JSON string and never a JSON integer
    def test_a_json_integer_result_raises_source_error(self):
        rpc, _ = _rpc(_ok(2000000000000000000))
        with pytest.raises(SourceError):
            rpc.eth_get_balance(VITALIK)

    # pins: a string that is not 0x-prefixed hex raises instead of parsing
    #       through a lenient int()
    @pytest.mark.parametrize("result", ["", "0x", "latest", "1bc16d674ec80000", "0xzz"])
    def test_a_non_hex_string_result_raises_source_error(self, result):
        rpc, _ = _rpc(_ok(result))
        with pytest.raises(SourceError):
            rpc.eth_get_balance(VITALIK)


class TestEthBlockNumber:
    # pins: eth_blockNumber posts an EMPTY params array
    def test_posts_the_method_with_empty_params(self):
        rpc, seen = _rpc(_ok(HEAD))
        rpc.eth_block_number()
        assert _bodies(seen) == [
            {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
        ]

    # pins: the head parses from its hex string to the pinned golden block
    def test_parses_the_head_hex_string_as_base_sixteen(self):
        rpc, _ = _rpc(_ok(HEAD))
        assert rpc.eth_block_number() == 20450000

    # pins: a JSON integer head is a malformed envelope, since a block
    #       number arrives as a hex string like every other number here
    def test_a_json_integer_head_raises_source_error(self):
        rpc, _ = _rpc(_ok(20450000))
        with pytest.raises(SourceError):
            rpc.eth_block_number()


class TestEthGetLogs:
    # pins: the filter dict goes on the wire unvalidated, as the single
    #       element of params, keys logs.py owns included
    def test_the_filter_object_is_forwarded_unvalidated(self):
        rpc, seen = _rpc(_ok(LOG_ROWS))
        rpc.eth_get_logs(LOG_FILTER)
        body = _bodies(seen)[0]
        assert body["method"] == "eth_getLogs"
        assert body["params"] == [LOG_FILTER]

    # pins: the rows come back exactly as received, in received order, with
    #       no key dropped, no case folded and nothing coerced
    def test_returns_the_raw_rows_untouched_and_in_order(self):
        rpc, _ = _rpc(_ok(LOG_ROWS))
        rows = rpc.eth_get_logs(LOG_FILTER)
        assert rows == LOG_ROWS
        assert [row["logIndex"] for row in rows] == ["0x2a", "0x2b"]
        assert rows[0]["address"] == USDC_CHECKSUMMED  # not lowercased
        assert rows[0]["aFieldNobodyModels"] == {"nested": [1, 2, 3]}
        assert rows[1]["removed"] is True

    # pins: an empty result list is an empty scan and not an error
    def test_an_empty_result_list_is_an_empty_scan(self):
        rpc, _ = _rpc(_ok([]))
        assert rpc.eth_get_logs(LOG_FILTER) == []

    # pins: a result that is not a list raises rather than being wrapped
    @pytest.mark.parametrize("result", [{}, "0x", None, 7])
    def test_a_non_list_result_raises_source_error(self, result):
        rpc, _ = _rpc(_ok(result))
        with pytest.raises(SourceError):
            rpc.eth_get_logs(LOG_FILTER)


class TestBatchWireShape:
    # pins: a batch posts a JSON ARRAY of envelopes with ids 1..N assigned
    #       in request order, each carrying its own method and params
    def test_batch_posts_an_array_with_ids_one_to_n_in_request_order(self):
        responses = [_ok(WORD_SIX, 1), _ok(WORD_BALANCE, 2), _ok(HEAD, 3)]
        rpc, seen = _rpc(responses)
        rpc.batch(
            [
                ("eth_call", [CALL_A, "latest"]),
                ("eth_call", [CALL_B, "latest"]),
                ("eth_blockNumber", []),
            ]
        )
        assert _bodies(seen) == [
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_call",
                    "params": [CALL_A, "latest"],
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "eth_call",
                    "params": [CALL_B, "latest"],
                },
                {"jsonrpc": "2.0", "id": 3, "method": "eth_blockNumber", "params": []},
            ]
        ]

    # pins: ids restart at 1 for every batch, so the second batch is not
    #       numbered 4, 5 by a counter that outlived the first
    def test_ids_restart_at_one_for_every_batch(self):
        first = [_ok(WORD_SIX, 1), _ok(WORD_BALANCE, 2)]
        second = [_ok(HEAD, 1), _ok(WORD_SIX, 2)]
        rpc, seen = _rpc(first, second)
        rpc.batch([("eth_call", [CALL_A, "latest"]), ("eth_blockNumber", [])])
        rpc.batch([("eth_blockNumber", []), ("eth_call", [CALL_B, "latest"])])
        assert [[item["id"] for item in body] for body in _bodies(seen)] == [
            [1, 2],
            [1, 2],
        ]

    # pins: the batch return is a tuple of BatchResult, one per request
    def test_the_batch_return_is_a_tuple_of_batch_results(self):
        rpc, _ = _rpc([_ok(WORD_SIX, 1), _ok(HEAD, 2)])
        results = rpc.batch([("eth_call", [CALL_A, "latest"]), ("eth_blockNumber", [])])
        assert isinstance(results, tuple)
        assert all(isinstance(item, BatchResult) for item in results)
        assert len(results) == 2


class TestBatchIsMatchedById:
    # pins: a batch answered in REVERSED id order comes back in REQUEST
    #       order, so responses are looked up by id and never by position
    def test_a_reversed_id_response_array_is_returned_in_request_order(self):
        # The node answers 3, 2, 1. A position-matching implementation
        # returns (HEAD, WORD_BALANCE, WORD_SIX), which is backwards.
        reversed_body = [_ok(HEAD, 3), _ok(WORD_BALANCE, 2), _ok(WORD_SIX, 1)]
        rpc, _ = _rpc(reversed_body)
        results = rpc.batch(
            [
                ("eth_call", [CALL_A, "latest"]),
                ("eth_call", [CALL_B, "latest"]),
                ("eth_blockNumber", []),
            ]
        )
        assert results == (
            BatchResult(WORD_SIX, None),
            BatchResult(WORD_BALANCE, None),
            BatchResult(HEAD, None),
        )
        assert results[0].result == WORD_SIX  # the body whose id was 1
        assert results[0].result != HEAD  # what position matching returns

    # pins: an out-of-order but non-reversed array is matched by id too, so
    #       the reversal test is not passed by a blanket reverse()
    def test_a_rotated_id_response_array_is_returned_in_request_order(self):
        rotated = [_ok(WORD_BALANCE, 2), _ok(HEAD, 3), _ok(WORD_SIX, 1)]
        rpc, _ = _rpc(rotated)
        results = rpc.batch(
            [
                ("eth_call", [CALL_A, "latest"]),
                ("eth_call", [CALL_B, "latest"]),
                ("eth_blockNumber", []),
            ]
        )
        assert [item.result for item in results] == [WORD_SIX, WORD_BALANCE, HEAD]


class TestBatchDeclaredFailure:
    #: Five requests, the middle one reverting: the spec's Done-when line.
    FIVE = [
        ("eth_call", [CALL_A, "latest"]),
        ("eth_call", [CALL_B, "latest"]),
        ("eth_call", [{"to": VITALIK, "data": DECIMALS}, "latest"]),
        ("eth_call", [CALL_A, "0x1380ad0"]),
        ("eth_blockNumber", []),
    ]

    REVERT = {"code": -32000, "message": "execution reverted"}

    def _five_with_a_revert(self) -> tuple[BatchResult, ...]:
        body = [
            _ok(WORD_SIX, 1),
            _ok(WORD_BALANCE, 2),
            {"jsonrpc": "2.0", "id": 3, "error": self.REVERT},
            _ok(WORD_SIX, 4),
            _ok(HEAD, 5),
        ]
        rpc, _ = _rpc(body)
        return rpc.batch(self.FIVE)

    # pins: one reverting item does NOT raise, so a single failure never
    #       voids the batch
    def test_a_reverting_item_does_not_raise(self):
        results = self._five_with_a_revert()
        assert len(results) == 5

    # pins: the reverting item is a DECLARED failure, result None and error
    #       set, never coerced to zero or to an empty word (rule #8)
    def test_the_reverting_item_is_a_declared_failure(self):
        failed = self._five_with_a_revert()[2]
        assert failed.result is None
        assert failed.error is not None
        assert failed.error not in ("0x", "", "0x0")

    # pins: the declared failure's text carries both the JSON-RPC code and
    #       the node's message, so the reason survives to the caller
    def test_the_declared_failure_carries_the_code_and_the_message(self):
        failed = self._five_with_a_revert()[2]
        assert "-32000" in failed.error
        assert "execution reverted" in failed.error

    # pins: the four siblings of a reverting item come back with their
    #       results set and no error, in request order
    def test_the_four_siblings_come_back_intact_in_request_order(self):
        results = self._five_with_a_revert()
        siblings = [results[0], results[1], results[3], results[4]]
        assert [item.result for item in siblings] == [
            WORD_SIX,
            WORD_BALANCE,
            WORD_SIX,
            HEAD,
        ]
        assert all(item.error is None for item in siblings)

    # pins: a batch in which EVERY item reverted still returns declared
    #       failures rather than raising
    def test_an_all_reverting_batch_returns_declared_failures(self):
        body = [
            {"jsonrpc": "2.0", "id": 1, "error": self.REVERT},
            {"jsonrpc": "2.0", "id": 2, "error": self.REVERT},
        ]
        rpc, _ = _rpc(body)
        results = rpc.batch(
            [("eth_call", [CALL_A, "latest"]), ("eth_call", [CALL_B, "latest"])]
        )
        assert [item.result for item in results] == [None, None]
        assert all("-32000" in item.error for item in results)


class TestBatchMalformedEnvelopes:
    THREE = [
        ("eth_call", [CALL_A, "latest"]),
        ("eth_call", [CALL_B, "latest"]),
        ("eth_blockNumber", []),
    ]

    def _raises(self, body: object) -> None:
        rpc, _ = _rpc(body)
        with pytest.raises(SourceError):
            rpc.batch(self.THREE)

    # pins: a batch body that is not a JSON list raises, so a single-object
    #       answer to an array request is refused
    @pytest.mark.parametrize(
        "body", [{"jsonrpc": "2.0", "id": 1, "result": WORD_SIX}, 7]
    )
    def test_a_non_list_batch_body_raises_source_error(self, body):
        self._raises(body)

    # pins: a response array SHORT one entry raises, which is how a missing
    #       id is caught before any lookup
    def test_a_short_response_array_raises_source_error(self):
        self._raises([_ok(WORD_SIX, 1), _ok(WORD_BALANCE, 2)])

    # pins: a response array LONGER than the request raises, so an extra
    #       item is never silently dropped
    def test_a_long_response_array_raises_source_error(self):
        self._raises(
            [_ok(WORD_SIX, 1), _ok(WORD_BALANCE, 2), _ok(HEAD, 3), _ok(HEAD, 4)]
        )

    # pins: a DUPLICATE id raises, so two answers for one request never let
    #       the last writer win a slot
    def test_a_duplicate_id_raises_source_error(self):
        self._raises([_ok(WORD_SIX, 1), _ok(WORD_BALANCE, 1), _ok(HEAD, 3)])

    # pins: an id that was never in the batch raises, so a stray answer is
    #       not matched to a request that did not ask for it
    def test_an_id_outside_the_batch_raises_source_error(self):
        self._raises([_ok(WORD_SIX, 1), _ok(WORD_BALANCE, 2), _ok(HEAD, 7)])

    # pins: a batch item that is not an object raises
    @pytest.mark.parametrize("item", ["0x1", None, 3, ["0x1"]])
    def test_a_non_object_batch_item_raises_source_error(self, item):
        self._raises([_ok(WORD_SIX, 1), item, _ok(HEAD, 3)])

    # pins: a batch item carrying no id raises, since it cannot be matched
    #       to any request
    def test_a_batch_item_without_an_id_raises_source_error(self):
        self._raises(
            [_ok(WORD_SIX, 1), {"jsonrpc": "2.0", "result": WORD_BALANCE}, _ok(HEAD, 3)]
        )

    # pins: the batch path shares the transport guards, so a non-2xx status
    #       raises SourceError there too
    def test_a_non_2xx_batch_status_raises_source_error(self):
        self._raises((500, [_ok(WORD_SIX, 1), _ok(WORD_BALANCE, 2), _ok(HEAD, 3)]))

    # pins: the batch path goes through the same transport door, so an
    #       unusable node url raises SourceError there too
    @pytest.mark.parametrize(
        "url", ["localhost:8545", "https://ok.invalid:notaport/v1"]
    )
    def test_an_unusable_node_url_raises_source_error_on_the_batch_path(self, url):
        client, _ = _scripted_client([_ok(WORD_SIX, 1)])
        with pytest.raises(SourceError):
            EvmRpc(client, url).batch([("eth_blockNumber", [])])

    # pins: a batch params value json cannot encode raises SourceError before
    #       any request is issued, the same as on the single-call path
    def test_an_unencodable_batch_payload_raises_source_error(self):
        client, seen = _scripted_client([_ok(WORD_SIX, 1)])
        with pytest.raises(SourceError) as excinfo:
            EvmRpc(client, URL).batch(
                [("eth_call", [{"to": USDC, "data": b"\x01"}, "latest"])]
            )
        cause = excinfo.value.__cause__
        assert isinstance(cause, TypeError)
        assert not isinstance(cause, httpx.HTTPError)
        assert seen == []


class TestSingleCallFailures:
    # pins: a non-2xx status raises SourceError
    @pytest.mark.parametrize("status", [400, 429, 500, 502])
    def test_a_non_2xx_status_raises_source_error(self, status):
        rpc, _ = _rpc((status, _ok(WORD_SIX)))
        with pytest.raises(SourceError):
            rpc.eth_call(USDC, DECIMALS)

    # pins: an httpx transport failure is translated to SourceError, so no
    #       httpx exception escapes this package's taxonomy
    @pytest.mark.parametrize(
        "raised",
        [
            httpx.ConnectError("connection refused"),
            httpx.ReadTimeout("read timed out"),
        ],
    )
    def test_a_transport_error_raises_source_error(self, raised):
        rpc, _ = _rpc(raised)
        with pytest.raises(SourceError):
            rpc.eth_call(USDC, DECIMALS)

    # pins: a node url with no scheme raises SourceError, so the likeliest
    #       local-node typo is reported and never leaks a bare ValueError
    @pytest.mark.parametrize(
        "url",
        [
            "localhost:8545",
            "127.0.0.1:8545",
            "node.example.invalid/v1",
            "",
        ],
    )
    def test_a_scheme_less_node_url_raises_source_error(self, url):
        # httpx does not refuse these itself. It sends, and then urllib's
        # cookie extraction raises ValueError("unknown url type: '/8545'"),
        # which descends from Exception and NOT from httpx.HTTPError. A door
        # that catches only httpx.HTTPError leaks it to the caller.
        client, _ = _scripted_client(_ok(WORD_SIX))
        with pytest.raises(SourceError) as excinfo:
            EvmRpc(client, url).eth_call(USDC, DECIMALS)
        cause = excinfo.value.__cause__
        assert isinstance(cause, ValueError)
        assert not isinstance(cause, httpx.HTTPError)

    # pins: an url httpx itself refuses to parse raises SourceError, since
    #       httpx.InvalidURL descends from Exception and not from HTTPError
    @pytest.mark.parametrize(
        "url", ["https://ok.invalid:notaport/v1", "http://[::1/v1"]
    )
    def test_an_unparsable_node_url_raises_source_error(self, url):
        client, seen = _scripted_client(_ok(WORD_SIX))
        with pytest.raises(SourceError) as excinfo:
            EvmRpc(client, url).eth_call(USDC, DECIMALS)
        cause = excinfo.value.__cause__
        assert isinstance(cause, httpx.InvalidURL)
        assert not isinstance(cause, httpx.HTTPError)
        assert seen == []  # refused before the send

    # pins: a params value json cannot encode raises SourceError before any
    #       request is issued, so caller input never leaks a TypeError
    def test_a_payload_json_cannot_encode_raises_source_error(self):
        client, seen = _scripted_client(_ok(WORD_SIX))
        with pytest.raises(SourceError) as excinfo:
            EvmRpc(client, URL).eth_call(USDC, b"\x01")  # type: ignore[arg-type]
        cause = excinfo.value.__cause__
        assert isinstance(cause, TypeError)
        assert not isinstance(cause, httpx.HTTPError)
        assert seen == []  # refused while building the request

    # pins: the log filter is forwarded unvalidated, so a filter value json
    #       cannot encode is still refused as SourceError and not as TypeError
    def test_an_unencodable_log_filter_raises_source_error(self):
        client, seen = _scripted_client(_ok(LOG_ROWS))
        with pytest.raises(SourceError) as excinfo:
            EvmRpc(client, URL).eth_get_logs(
                {"fromBlock": "0x1380ad0", "topics": {DECIMALS}}
            )
        assert isinstance(excinfo.value.__cause__, TypeError)
        assert seen == []

    # pins: a body that is not JSON at all raises SourceError
    def test_a_text_plain_body_raises_source_error(self):
        rpc, _ = _rpc("<html>rate limited</html>")
        with pytest.raises(SourceError):
            rpc.eth_call(USDC, DECIMALS)

    # pins: a single-call body that is a JSON LIST raises, so a batch-shaped
    #       answer to a single request is refused
    def test_a_json_list_body_raises_source_error(self):
        rpc, _ = _rpc([_ok(WORD_SIX)])
        with pytest.raises(SourceError):
            rpc.eth_call(USDC, DECIMALS)

    # pins: an error member raises SourceError carrying the code and the
    #       message, as SolanaRpc._call does
    def test_an_error_member_raises_carrying_the_code_and_message(self):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "execution reverted"},
        }
        rpc, _ = _rpc(body)
        with pytest.raises(SourceError) as excinfo:
            rpc.eth_call(USDC, DECIMALS)
        assert "-32000" in str(excinfo.value)
        assert "execution reverted" in str(excinfo.value)

    # pins: a body with neither result nor error raises, so an empty
    #       envelope is never read as a null result
    def test_a_body_with_neither_result_nor_error_raises_source_error(self):
        rpc, _ = _rpc({"jsonrpc": "2.0", "id": 1})
        with pytest.raises(SourceError):
            rpc.eth_block_number()

    # pins: there is NO retry, so a failing single call issues exactly one
    #       request before raising
    def test_a_failing_call_is_issued_exactly_once(self):
        rpc, seen = _rpc((500, _ok(WORD_SIX)))
        with pytest.raises(SourceError):
            rpc.eth_call(USDC, DECIMALS)
        assert len(seen) == 1

    # pins: every failure mode reaches the caller as SourceError and never
    #       as a bare httpx, JSON or key error
    @pytest.mark.parametrize(
        "response",
        [
            (503, {"jsonrpc": "2.0", "id": 1, "result": "0x1"}),
            "not json",
            [1, 2, 3],
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "no"}},
            {"jsonrpc": "2.0", "id": 1},
            httpx.ConnectError("refused"),
        ],
    )
    def test_no_failure_escapes_as_a_foreign_exception_type(self, response):
        rpc, _ = _rpc(response)
        with pytest.raises(SourceError):
            rpc.eth_get_balance(VITALIK)


FORBIDDEN_IMPORT_DOMAINS = {
    "accounting",
    "api",
    "decode",
    "jobs",
    "ledger",
    "portfolio",
    "positions",
    "prices",
    "project",
    "tenancy",
    "webhooks",
}


# pins: importing the module opens no socket and adds no import edge out of
#       the sources layer, so httpx is the only I/O dependency it carries
def test_reimport_does_no_io_and_module_stays_in_its_layer():
    name = "auradefi.sources.evm.rpc"
    saved = sys.modules.pop(name, None)
    try:
        # The autouse socket guard is active: a connect at import time fails.
        module = importlib.import_module(name)
    finally:
        if saved is not None:
            sys.modules[name] = saved
    assert hasattr(module, "EvmRpc")
    assert hasattr(module, "BatchResult")
    assert hasattr(module, "block_tag")

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module or "")
    domains = {
        dotted.split(".")[1] for dotted in imported if dotted.startswith("auradefi.")
    }
    assert not domains & FORBIDDEN_IMPORT_DOMAINS, (
        f"sources/ must not import {sorted(domains & FORBIDDEN_IMPORT_DOMAINS)}"
    )


# pins: no retry and no rate limiting are built in, so a caller that asks
#       for one node read gets exactly one node read and no wall-clock wait
def test_the_module_declares_no_retry_or_rate_limit_machinery():
    source = Path(
        importlib.import_module("auradefi.sources.evm.rpc").__file__
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not names & {"sleep", "retry", "backoff", "Retrying"}
