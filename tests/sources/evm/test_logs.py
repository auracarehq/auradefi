"""Contract tests for the chunked ``eth_getLogs`` scan (RELEASE_0.2.0 §4).

Every request here is served by an ``httpx.MockTransport``, so the autouse
socket guard in ``tests/conftest.py`` stays armed and no cassette is
needed: what these tests pin is the filter object this module authors and
the chunk boundaries it derives, neither of which a recording contains.

The headline pin is the chunk vector. ``rpc.py`` forwards a filter dict
unvalidated, so ``logs.py`` is the sole author of the wire shape, and the
inclusive-range arithmetic has exactly one correct reading. Every other
reading either asks a node for one block twice or steps over one.

GOLDEN VECTORS, each derived by hand from the pinned formula
``chunk k = [from + k*chunk, min(from + (k+1)*chunk - 1, to)]`` with
``ceil((to - from + 1) / chunk)`` chunks, then hardcoded:

  from 20_449_000 to 20_450_000 by 500, so ceil(1001/500) = 3 chunks
      [20449000, 20449499] -> ("0x13806e8", "0x13808db")
      [20449500, 20449999] -> ("0x13808dc", "0x1380acf")
      [20450000, 20450000] -> ("0x1380ad0", "0x1380ad0")
  from 1000 to 1999 by 500, an exact multiple, so ceil(1000/500) = 2 and
  there is no third request
      [1000, 1499] -> ("0x3e8", "0x5db")
      [1500, 1999] -> ("0x5dc", "0x7cf")
  from 0 to 4000 at the 2000-block default, so ceil(4001/2000) = 3
      ("0x0", "0x7cf"), ("0x7d0", "0xf9f"), ("0xfa0", "0xfa0")

  20_450_000 = 0x1380ad0, the 0.2.0 golden block, matching rpc.py's
  block_tag vector. 1000 = 0x3e8, 1499 = 0x5db, 1500 = 0x5dc, 1999 =
  0x7cf, 4000 = 0xfa0, and 0 is "0x0" because block zero is a real height.

The three-chunk ordering test hands back log indices 5 and 3, then 9, then
1, in that order across the chunks. Received order is the contract, so an
implementation that sorts the accumulated rows returns 1, 3, 5, 9 and
fails, and one that reverses the chunks returns 1, 9, 5, 3 and fails too.

Four refusals here look pedantic and are each a coercion the module is
forbidden to make (rule #8, declare and never default). A present
``removed`` that is not a JSON boolean, an absent ``data`` key, a
``topics`` argument given as a bare string, and a ``bytes`` address all
have an obvious lenient reading, and each lenient reading silently
changes an answer: truthiness turns the string "false" into a reorged
row, a defaulted payload turns "the node said nothing" into the empty
word, and a str or bytes is a Sequence, so iterating one turns a single
topic0 into 66 one-character slots. An empty container is the fixture
that reaches the last of those, since a non-empty ``bytes`` address is
refused a second time by the per-entry address check.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import json
import math
import sys
from pathlib import Path

import httpx
import pytest

from auradefi.errors import SourceError, ValidationError
from auradefi.sources.evm.logs import DEFAULT_CHUNK_BLOCKS, LogRecord, scan_logs
from auradefi.sources.evm.rpc import EvmRpc

URL = "https://node.example.invalid/v1"

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_CHECKSUMMED = "0xA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"
DAI_CHECKSUMMED = "0x6B175474E89094C44DA98B954EEDEAC495271D0F"
VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

#: keccak256("Transfer(address,address,uint256)"), the topic0 every ERC-20
#: transfer carries and the reason this module exists.
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

#: An indexed address topic: 12 zero bytes then the 20-byte address.
PAD_VITALIK = "0x000000000000000000000000d8da6bf26964af9d7eed9e03e53415d37aa96045"
PAD_USDC = "0x000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
PAD_USDC_CHECKSUMMED = (
    "0x000000000000000000000000A0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
)

TX = "0x9a8f1c2b3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8"
TX_LOUD = "0x9A8F1C2B3D4E5F60718293A4B5C6D7E8F90A1B2C3D4E5F60718293A4B5C6D7E8"

#: 1,000,000 as a 32-byte big-endian word, which is 1 USDC at 6 decimals.
DATA_ONE_MILLION = (
    "0x00000000000000000000000000000000000000000000000000000000000f4240"
)

#: The same word as bytes, written as its padding plus its three
#: significant bytes so a reader can check it by eye: 0x0f4240 is
#: 1,000,000, and 29 + 3 is 32.
DATA_ONE_MILLION_BYTES = bytes(29) + b"\x0f\x42\x40"

GOLDEN_BLOCK = 20_450_000

#: One well-formed row, deliberately loud where the contract says the
#: typed record is quiet: the address, the transaction hash and the third
#: topic are all checksummed on the wire and lowercase in the record. It
#: also carries a key nobody models and NO "removed" key, which is the
#: absent-reads-False branch.
BASE_ROW = {
    "address": USDC_CHECKSUMMED,
    "topics": [TRANSFER, PAD_VITALIK, PAD_USDC_CHECKSUMMED],
    "data": DATA_ONE_MILLION,
    "blockNumber": "0x1380ad0",
    "transactionHash": TX_LOUD,
    "logIndex": "0x2a",
    "aFieldNobodyModels": {"nested": [1, 2, 3]},
}

GOLDEN_RECORD = LogRecord(
    address=USDC,
    topics=(TRANSFER, PAD_VITALIK, PAD_USDC),
    data=DATA_ONE_MILLION_BYTES,
    block_number=GOLDEN_BLOCK,
    transaction_hash=TX,
    log_index=42,
    removed=False,
)

#: Sentinel for :func:`_row`: this key is DELETED, not overwritten.
ABSENT = object()


def _row(**overrides: object) -> dict:
    """:data:`BASE_ROW` with keys replaced, or removed via :data:`ABSENT`."""
    row = dict(BASE_ROW)
    for key, value in overrides.items():
        if value is ABSENT:
            row.pop(key, None)
        else:
            row[key] = value
    return row


def _scan_rpc(*results: list) -> tuple[EvmRpc, list[dict]]:
    """An rpc serving one ``eth_getLogs`` row list per request, in order.

    Requests past the last entry are answered with an empty list, so
    ``_scan_rpc()`` answers everything empty. Every posted body is
    recorded, which is how the filter objects are asserted.
    """
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        index = len(bodies) - 1
        rows = results[index] if index < len(results) else []
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": rows}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return EvmRpc(client, URL), bodies


def _rpc_failing_on(index: int) -> tuple[EvmRpc, list[dict]]:
    """An rpc whose ``index``-th ``eth_getLogs`` is a node error envelope."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) - 1 == index:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {"code": -32005, "message": "query returned too much"},
                },
            )
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": [BASE_ROW]}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return EvmRpc(client, URL), bodies


def _tripwire_rpc() -> tuple[EvmRpc, list[httpx.Request]]:
    """An rpc that records and refuses every request: proves ZERO HTTP."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise RuntimeError("HTTP attempted where the contract forbids it")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return EvmRpc(client, URL), seen


def _filters(bodies: list[dict]) -> list[dict]:
    """The filter object of each recorded ``eth_getLogs`` body."""
    return [body["params"][0] for body in bodies]


def _ranges(bodies: list[dict]) -> list[tuple[str, str]]:
    """The (fromBlock, toBlock) pair each recorded request asked for."""
    return [
        (filter_object["fromBlock"], filter_object["toBlock"])
        for filter_object in _filters(bodies)
    ]


# pins: the pinned data word and its byte form are the same 32 bytes, so a
#       typo in either fixture literal fails here instead of accusing the
#       implementation of a decode bug it does not have
def test_the_pinned_data_word_and_its_byte_form_agree():
    assert DATA_ONE_MILLION_BYTES == bytes.fromhex(DATA_ONE_MILLION[2:])
    assert len(DATA_ONE_MILLION_BYTES) == 32
    assert int.from_bytes(DATA_ONE_MILLION_BYTES, "big") == 1_000_000


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


class TestChunking:
    # pins: the 1001-block inclusive range at width 500 goes out as exactly
    #       three requests over the pinned hex boundaries, so a chunk holds
    #       500 blocks and the last one is clamped to to_block
    def test_the_chunk_vector_is_three_requests_over_the_pinned_ranges(self):
        rpc, bodies = _scan_rpc()
        scan_logs(rpc, from_block=20_449_000, to_block=20_450_000, chunk_blocks=500)
        assert _ranges(bodies) == [
            ("0x13806e8", "0x13808db"),
            ("0x13808dc", "0x1380acf"),
            ("0x1380ad0", "0x1380ad0"),
        ]

    # pins: the range is INCLUSIVE at both ends, so a single-block scan is
    #       one request whose fromBlock and toBlock are that same block
    def test_a_single_block_range_is_one_request_on_one_block(self):
        rpc, bodies = _scan_rpc()
        scan_logs(rpc, from_block=GOLDEN_BLOCK, to_block=GOLDEN_BLOCK)
        assert _ranges(bodies) == [("0x1380ad0", "0x1380ad0")]

    # pins: a span that is an exact multiple of chunk_blocks sends exactly
    #       span/chunk requests and no trailing empty one
    def test_an_exact_multiple_span_sends_no_trailing_empty_request(self):
        rpc, bodies = _scan_rpc()
        scan_logs(rpc, from_block=1000, to_block=1999, chunk_blocks=500)
        assert _ranges(bodies) == [("0x3e8", "0x5db"), ("0x5dc", "0x7cf")]

    # pins: a chunk wider than the range collapses to one request whose
    #       toBlock is clamped to to_block and never runs past it
    def test_a_chunk_wider_than_the_range_is_one_clamped_request(self):
        rpc, bodies = _scan_rpc()
        scan_logs(rpc, from_block=1000, to_block=1999, chunk_blocks=5000)
        assert _ranges(bodies) == [("0x3e8", "0x7cf")]

    # pins: the default width is 2000 blocks, which is what a caller who
    #       names no chunk_blocks gets on the wire, and block zero is a
    #       real height that scans as "0x0"
    def test_the_default_width_is_two_thousand_blocks_and_zero_scans(self):
        assert DEFAULT_CHUNK_BLOCKS == 2_000
        rpc, bodies = _scan_rpc()
        scan_logs(rpc, from_block=0, to_block=4000)
        assert _ranges(bodies) == [
            ("0x0", "0x7cf"),
            ("0x7d0", "0xf9f"),
            ("0xfa0", "0xfa0"),
        ]

    # pins: the chunks tile the inclusive range exactly, each starting on
    #       the block after the previous one ended, so no block is asked
    #       for twice and none is stepped over
    @pytest.mark.parametrize(
        ("from_block", "to_block", "chunk_blocks"),
        [
            (0, 0, 1),
            (0, 1, 1),
            (7, 7, 2000),
            (100, 199, 7),
            (20_449_000, 20_450_000, 500),
            (1, 100_000, 2000),
        ],
    )
    def test_the_chunks_tile_the_range_without_overlap_or_gap(
        self, from_block, to_block, chunk_blocks
    ):
        rpc, bodies = _scan_rpc()
        scan_logs(
            rpc,
            from_block=from_block,
            to_block=to_block,
            chunk_blocks=chunk_blocks,
        )
        spans = [
            (int(low, 16), int(high, 16)) for low, high in _ranges(bodies)
        ]
        assert len(spans) == math.ceil((to_block - from_block + 1) / chunk_blocks)
        assert spans[0][0] == from_block
        assert spans[-1][1] == to_block
        for (_, previous_high), (low, _) in zip(spans, spans[1:]):
            assert low == previous_high + 1
        for low, high in spans[:-1]:
            assert high - low + 1 == chunk_blocks
        assert all(low <= high for low, high in spans)

    # pins: each chunk goes out as exactly one eth_getLogs whose params
    #       array holds the filter object and nothing else
    def test_each_chunk_is_one_eth_get_logs_carrying_only_the_filter(self):
        rpc, bodies = _scan_rpc()
        scan_logs(rpc, from_block=1000, to_block=1999, chunk_blocks=500)
        assert [body["method"] for body in bodies] == ["eth_getLogs", "eth_getLogs"]
        assert bodies[0]["params"] == [{"fromBlock": "0x3e8", "toBlock": "0x5db"}]


class TestFilterObject:
    # pins: address=None and topics=() are ABSENT keys, never a null and
    #       never an empty list, both of which a node reads as a filter
    def test_an_absent_address_and_empty_topics_are_omitted_keys(self):
        rpc, bodies = _scan_rpc()
        scan_logs(rpc, from_block=1000, to_block=1000)
        assert _filters(bodies) == [{"fromBlock": "0x3e8", "toBlock": "0x3e8"}]

    # pins: a str address is emitted as a lowercased STRING, not wrapped
    #       in a one-element list
    def test_a_str_address_is_emitted_lowercased_as_a_string(self):
        rpc, bodies = _scan_rpc()
        scan_logs(
            rpc, from_block=1000, to_block=1000, address=USDC_CHECKSUMMED
        )
        assert _filters(bodies)[0]["address"] == USDC

    # pins: a sequence of addresses is emitted as a LIST of lowercase
    #       strings, in the order given
    def test_a_sequence_address_is_emitted_as_a_list_of_lowercase_strings(self):
        rpc, bodies = _scan_rpc()
        scan_logs(
            rpc,
            from_block=1000,
            to_block=1000,
            address=[USDC_CHECKSUMMED, DAI_CHECKSUMMED],
        )
        assert _filters(bodies)[0]["address"] == [USDC, DAI]

    # pins: the topics array nests exactly as given: a str is itself, None
    #       is a JSON null wildcard slot, and a nested sequence is a topic
    #       OR list, all in one array
    def test_the_topics_array_carries_wildcards_and_or_lists(self):
        rpc, bodies = _scan_rpc()
        scan_logs(
            rpc,
            from_block=1000,
            to_block=1000,
            topics=(TRANSFER, None, (PAD_VITALIK, PAD_USDC)),
        )
        assert _filters(bodies)[0]["topics"] == [
            TRANSFER,
            None,
            [PAD_VITALIK, PAD_USDC],
        ]

    # pins: a topic OR given as a LIST nests the same way a tuple does, so
    #       the caller's container type is not part of the wire shape
    def test_a_list_topic_or_nests_like_a_tuple(self):
        rpc, bodies = _scan_rpc()
        scan_logs(
            rpc,
            from_block=1000,
            to_block=1000,
            topics=[[PAD_VITALIK, PAD_USDC]],
        )
        assert _filters(bodies)[0]["topics"] == [[PAD_VITALIK, PAD_USDC]]

    # pins: an EMPTY address sequence still sends the address key holding an
    #       empty list, because only address=None omits it: a caller who
    #       passed a container asked for a container
    @pytest.mark.parametrize("address", [[], ()], ids=["empty-list", "empty-tuple"])
    def test_an_empty_address_sequence_is_sent_as_an_empty_list(self, address):
        rpc, bodies = _scan_rpc()
        scan_logs(rpc, from_block=1000, to_block=1000, address=address)
        assert _filters(bodies) == [
            {"fromBlock": "0x3e8", "toBlock": "0x3e8", "address": []}
        ]

    # pins: an empty topic OR list goes on the wire as an empty JSON array
    #       and is never rewritten to a null, so the slot the caller wrote is
    #       the slot the node reads
    @pytest.mark.parametrize("slot", [[], ()], ids=["empty-list", "empty-tuple"])
    def test_an_empty_topic_or_list_is_sent_as_an_empty_array(self, slot):
        rpc, bodies = _scan_rpc()
        scan_logs(rpc, from_block=1000, to_block=1000, topics=(slot,))
        assert _filters(bodies)[0]["topics"] == [[]]

    # pins: the filter object is rebuilt per chunk, so the address and
    #       topic filters ride on EVERY request and not only the first
    def test_every_chunk_carries_the_same_address_and_topic_filters(self):
        rpc, bodies = _scan_rpc()
        scan_logs(
            rpc,
            from_block=1000,
            to_block=1999,
            chunk_blocks=500,
            address=USDC,
            topics=(TRANSFER,),
        )
        assert [f["address"] for f in _filters(bodies)] == [USDC, USDC]
        assert [f["topics"] for f in _filters(bodies)] == [[TRANSFER], [TRANSFER]]


class TestAccumulation:
    # pins: rows accumulate in RECEIVED order across chunks, so an
    #       implementation that sorts the result returns them backwards
    def test_rows_from_three_chunks_concatenate_in_received_order(self):
        rpc, bodies = _scan_rpc(
            [_row(logIndex="0x5"), _row(logIndex="0x3")],
            [_row(logIndex="0x9")],
            [_row(logIndex="0x1")],
        )
        records = scan_logs(rpc, from_block=1000, to_block=2499, chunk_blocks=500)
        assert len(bodies) == 3
        assert [record.log_index for record in records] == [5, 3, 9, 1]

    # pins: an empty MIDDLE chunk contributes nothing and does NOT end the
    #       scan, so the third request still goes out and its rows return
    def test_an_empty_middle_chunk_does_not_end_the_scan(self):
        rpc, bodies = _scan_rpc(
            [_row(logIndex="0x5")],
            [],
            [_row(logIndex="0x1")],
        )
        records = scan_logs(rpc, from_block=1000, to_block=2499, chunk_blocks=500)
        assert _ranges(bodies) == [
            ("0x3e8", "0x5db"),
            ("0x5dc", "0x7cf"),
            ("0x7d0", "0x9c3"),
        ]
        assert [record.log_index for record in records] == [5, 1]

    # pins: a scan whose every chunk is empty returns an empty list and
    #       still issues every request the range asks for
    def test_an_all_empty_scan_returns_an_empty_list(self):
        rpc, bodies = _scan_rpc()
        assert scan_logs(rpc, from_block=1000, to_block=1999, chunk_blocks=500) == []
        assert len(bodies) == 2

    # pins: a node error mid-scan reaches the caller as SourceError and the
    #       rows already collected are NOT returned as a completed range
    def test_a_node_error_mid_scan_is_not_swallowed(self):
        rpc, bodies = _rpc_failing_on(1)
        with pytest.raises(SourceError):
            scan_logs(rpc, from_block=1000, to_block=2499, chunk_blocks=500)
        assert len(bodies) == 2


class TestValidation:
    # pins: an inverted or negative range is the caller's mistake and is
    #       refused before any HTTP, as ValidationError
    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"from_block": 1001, "to_block": 1000}, id="one-past"),
            pytest.param({"from_block": 20_450_000, "to_block": 0}, id="far-past"),
            pytest.param({"from_block": -1, "to_block": 10}, id="negative-from"),
            pytest.param({"from_block": -10, "to_block": -5}, id="wholly-negative"),
        ],
    )
    def test_a_refused_range_raises_before_any_request(self, kwargs):
        rpc, seen = _tripwire_rpc()
        with pytest.raises(ValidationError):
            scan_logs(rpc, **kwargs)
        assert seen == []

    # pins: a non-positive chunk width is refused before any HTTP, because
    #       a width of zero is an endless scan and a negative one walks
    #       backwards out of the range
    @pytest.mark.parametrize("chunk_blocks", [0, -1, -500])
    def test_a_non_positive_chunk_width_raises_before_any_request(
        self, chunk_blocks
    ):
        rpc, seen = _tripwire_rpc()
        with pytest.raises(ValidationError):
            scan_logs(rpc, from_block=0, to_block=10, chunk_blocks=chunk_blocks)
        assert seen == []

    # pins: a topic entry that is not a str, a str list or tuple, or None
    #       is refused before any HTTP: the filter is this module's to
    #       author, so a filter it cannot author never reaches a node
    @pytest.mark.parametrize(
        "topics",
        [
            pytest.param((7,), id="int-topic"),
            pytest.param((b"0x00",), id="bytes-topic"),
            pytest.param(({"topic": TRANSFER},), id="dict-topic"),
            pytest.param(([TRANSFER, 7],), id="int-inside-an-or-list"),
            pytest.param(((TRANSFER, None),), id="none-inside-an-or-tuple"),
            pytest.param((TRANSFER, 1.0), id="float-topic"),
        ],
    )
    def test_an_unusable_topic_raises_before_any_request(self, topics):
        rpc, seen = _tripwire_rpc()
        with pytest.raises(ValidationError):
            scan_logs(rpc, from_block=0, to_block=10, topics=topics)
        assert seen == []

    # pins: an address that is not a 0x-prefixed 40-hex string is refused
    #       before any HTTP, whether it is given alone or inside a sequence
    @pytest.mark.parametrize(
        "address",
        [
            pytest.param("0x1234", id="too-short"),
            pytest.param(USDC[2:], id="no-0x-prefix"),
            pytest.param("0x" + "zz" * 20, id="not-hex"),
            pytest.param(USDC + "00", id="too-long"),
            pytest.param(7, id="not-a-string"),
            pytest.param([USDC, "0x1234"], id="short-entry-in-a-sequence"),
            pytest.param([USDC, 7], id="non-string-in-a-sequence"),
        ],
    )
    def test_an_unusable_address_raises_before_any_request(self, address):
        rpc, seen = _tripwire_rpc()
        with pytest.raises(ValidationError):
            scan_logs(rpc, from_block=0, to_block=10, address=address)
        assert seen == []

    # pins: a topics argument given as a bare STRING is refused whole, so a
    #       caller who meant one topic0 never has it iterated into one slot
    #       per character and answered with nothing
    @pytest.mark.parametrize(
        "topics",
        [
            pytest.param(TRANSFER, id="topic0-as-a-bare-string"),
            pytest.param("0x", id="two-character-string"),
        ],
    )
    def test_a_bare_string_topics_argument_raises_before_any_request(self, topics):
        rpc, seen = _tripwire_rpc()
        with pytest.raises(ValidationError, match="topics"):
            scan_logs(rpc, from_block=0, to_block=10, topics=topics)
        assert seen == []

    # pins: a bytes address is refused as a container instead of iterated,
    #       so b"" never reaches the wire as an address filter of no
    #       addresses
    @pytest.mark.parametrize(
        "address",
        [
            pytest.param(b"", id="empty-bytes"),
            pytest.param(USDC.encode(), id="ascii-bytes-address"),
        ],
    )
    def test_a_bytes_address_raises_before_any_request(self, address):
        # b"" is the fixture that reaches this guard. A non-empty bytes
        # address is refused a second time, by the per-entry check, because
        # iterating bytes yields ints, so only the empty one tells the two
        # readings apart.
        rpc, seen = _tripwire_rpc()
        with pytest.raises(ValidationError, match="address"):
            scan_logs(rpc, from_block=0, to_block=10, address=address)
        assert seen == []

    # pins: a range this module accepts is scanned, so the guards above
    #       refuse the caller's mistake and nothing wider: from_block zero,
    #       an equal pair and a one-block width all reach the node
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"from_block": 0, "to_block": 0},
            {"from_block": 0, "to_block": 1, "chunk_blocks": 1},
            {"from_block": GOLDEN_BLOCK, "to_block": GOLDEN_BLOCK},
        ],
    )
    def test_a_legal_range_is_not_refused(self, kwargs):
        rpc, bodies = _scan_rpc()
        assert scan_logs(rpc, **kwargs) == []
        assert bodies


def test_reimport_does_no_io_and_the_module_stays_in_its_layer():
    name = "auradefi.sources.evm.logs"
    saved = sys.modules.pop(name, None)
    try:
        # The autouse socket guard is active: a connect at import time fails.
        module = importlib.import_module(name)
    finally:
        if saved is not None:
            sys.modules[name] = saved
    assert hasattr(module, "LogRecord")
    assert hasattr(module, "scan_logs")
    assert hasattr(module, "DEFAULT_CHUNK_BLOCKS")

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
    assert "auradefi.sources.evm.rpc" in imported


# pins: there is no retry and no rate limiting, so a caller who asks for a
#       range gets exactly the chunks that range needs and no wall-clock
#       wait between them
def test_the_module_declares_no_retry_or_rate_limit_machinery():
    source = Path(
        importlib.import_module("auradefi.sources.evm.logs").__file__
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not names & {"sleep", "retry", "backoff", "Retrying"}


class TestRowTyping:
    # pins: a recorded row types to the exact record phases 13 and 14
    #       consume: address, transaction hash and topics lowercased, data
    #       decoded to bytes, both quantities parsed from hex
    def test_a_recorded_row_types_to_the_golden_log_record(self):
        rpc, _ = _scan_rpc([BASE_ROW])
        assert scan_logs(
            rpc, from_block=GOLDEN_BLOCK, to_block=GOLDEN_BLOCK
        ) == [GOLDEN_RECORD]

    # pins: an absent "removed" key reads as False and a node that sends
    #       true is believed, so a reorged row is never silently kept
    @pytest.mark.parametrize(
        ("sent", "expected"),
        [(ABSENT, False), (False, False), (True, True)],
        ids=["absent", "false", "true"],
    )
    def test_the_removed_flag_defaults_to_false_and_is_read_when_sent(
        self, sent, expected
    ):
        rpc, _ = _scan_rpc([_row(removed=sent)])
        (record,) = scan_logs(rpc, from_block=GOLDEN_BLOCK, to_block=GOLDEN_BLOCK)
        assert record.removed is expected

    # pins: the empty payload "0x" decodes to b"" and never to b"\x00" or
    #       to the string "0x"
    def test_an_empty_data_payload_decodes_to_empty_bytes(self):
        rpc, _ = _scan_rpc([_row(data="0x")])
        (record,) = scan_logs(rpc, from_block=GOLDEN_BLOCK, to_block=GOLDEN_BLOCK)
        assert record.data == b""

    # pins: a row with no topics types to an EMPTY TUPLE, so an anonymous
    #       event is a real record and not a refusal
    def test_a_row_with_no_topics_types_to_an_empty_tuple(self):
        rpc, _ = _scan_rpc([_row(topics=[])])
        (record,) = scan_logs(rpc, from_block=GOLDEN_BLOCK, to_block=GOLDEN_BLOCK)
        assert record.topics == ()

    # pins: the record carries python types and never the wire strings, so
    #       block_number and log_index are ints parsed from hex
    def test_the_record_carries_python_types_and_not_wire_strings(self):
        rpc, _ = _scan_rpc([_row(blockNumber="0xff", logIndex="0x10")])
        (record,) = scan_logs(rpc, from_block=255, to_block=255)
        assert isinstance(record.topics, tuple)
        assert isinstance(record.data, bytes)
        assert record.block_number == 255
        assert record.log_index == 16
        assert isinstance(record.removed, bool)

    # pins: a scanned record is hashable, so two identical rows collapse in
    #       a set: tuple topics and bytes data, never a list or bytearray
    def test_two_identical_scanned_rows_collapse_in_a_set(self):
        rpc, _ = _scan_rpc([BASE_ROW, dict(BASE_ROW)])
        records = scan_logs(rpc, from_block=GOLDEN_BLOCK, to_block=GOLDEN_BLOCK)
        assert len(records) == 2
        assert len(set(records)) == 1


class TestMalformedRows:
    # pins: every malformed row the node can send reaches the caller as
    #       SourceError, never as a bare ValueError, KeyError or
    #       AttributeError from the typing itself
    @pytest.mark.parametrize(
        "rows",
        [
            pytest.param([["not", "an", "object"]], id="row-is-a-list"),
            pytest.param(["0xdeadbeef"], id="row-is-a-string"),
            pytest.param([_row(blockNumber=20_450_000)], id="blocknumber-json-int"),
            pytest.param([_row(blockNumber="12abz")], id="blocknumber-not-hex"),
            pytest.param([_row(blockNumber="0x")], id="blocknumber-no-digits"),
            pytest.param([_row(blockNumber=ABSENT)], id="blocknumber-absent"),
            pytest.param([_row(logIndex=ABSENT)], id="logindex-absent"),
            pytest.param([_row(logIndex=42)], id="logindex-json-int"),
            pytest.param([_row(logIndex="0xzz")], id="logindex-not-hex"),
            pytest.param([_row(topics=TRANSFER)], id="topics-is-a-string"),
            pytest.param([_row(topics=ABSENT)], id="topics-absent"),
            pytest.param([_row(topics=[TRANSFER[2:]])], id="topic-64-chars-no-0x"),
            pytest.param([_row(topics=[TRANSFER + "00"])], id="topic-too-long"),
            pytest.param([_row(topics=["0x" + "zz" * 32])], id="topic-not-hex"),
            pytest.param([_row(topics=[7])], id="topic-json-int"),
            pytest.param([_row(data="0xzz")], id="data-not-hex"),
            pytest.param([_row(data="0x123")], id="data-odd-length"),
            pytest.param([_row(data="f4240")], id="data-no-0x"),
            pytest.param([_row(data=1_000_000)], id="data-json-int"),
            pytest.param([_row(address=ABSENT)], id="address-absent"),
            pytest.param([_row(transactionHash=ABSENT)], id="txhash-absent"),
        ],
    )
    def test_a_malformed_row_raises_source_error(self, rows):
        rpc, _ = _scan_rpc(rows)
        with pytest.raises(SourceError):
            scan_logs(rpc, from_block=GOLDEN_BLOCK, to_block=GOLDEN_BLOCK)

    # pins: a PRESENT "removed" that is not a JSON boolean is refused rather
    #       than read through truthiness, so the string "false" never marks a
    #       live log as reorged away
    @pytest.mark.parametrize(
        "sent",
        [
            pytest.param("false", id="string-false"),
            pytest.param("true", id="string-true"),
            pytest.param(1, id="json-int-one"),
            pytest.param(0, id="json-int-zero"),
            pytest.param(None, id="json-null"),
        ],
    )
    def test_a_non_boolean_removed_flag_raises_source_error(self, sent):
        rpc, _ = _scan_rpc([_row(removed=sent)])
        with pytest.raises(SourceError, match="removed"):
            scan_logs(rpc, from_block=GOLDEN_BLOCK, to_block=GOLDEN_BLOCK)

    # pins: an ABSENT "data" key is declared unusable and never defaulted to
    #       the empty payload, because a row the node sent no data for and a
    #       row carrying "0x" say different things
    def test_an_absent_data_key_raises_instead_of_decoding_to_empty_bytes(self):
        rpc, _ = _scan_rpc([_row(data=ABSENT)])
        with pytest.raises(SourceError, match="data"):
            scan_logs(rpc, from_block=GOLDEN_BLOCK, to_block=GOLDEN_BLOCK)

    # pins: a malformed row in a LATER chunk still raises, so the typing
    #       runs on every chunk and not only the first
    def test_a_malformed_row_in_a_later_chunk_still_raises(self):
        rpc, _ = _scan_rpc([BASE_ROW], [_row(blockNumber="12abz")])
        with pytest.raises(SourceError):
            scan_logs(rpc, from_block=1000, to_block=1999, chunk_blocks=500)
