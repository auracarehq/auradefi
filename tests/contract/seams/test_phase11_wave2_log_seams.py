"""Phase 11 wave-2 seam audit: the boundaries around ``sources/evm/logs.py``.

Nothing here looks inside the scanner. Every assertion crosses a boundary that
no single work order owned:

* the node handle. ``scan_logs(rpc: EvmRpc, ...)`` is annotated with the
  concrete class, and the only thing the seam promises is
  ``eth_get_logs(filter_object) -> list[dict]`` plus the module-level
  ``block_tag``. The binding below is written ONLY from that: one method, and
  an object that raises on every other attribute. An in-repo test hands over a
  real ``EvmRpc``, which has ``eth_call``, ``batch``, ``_post`` and ``url``, so
  a reach for any of them passes there and fails here.

* the filter wire shape. ``rpc.eth_get_logs`` forwards the dict unvalidated,
  which the wave-1 seam file pinned by sending it an int ``fromBlock`` and
  watching it survive. That makes ``logs.py`` the sole author of every key a
  node sees, and the only place to read the authored value is on the far side
  of the handle.

* the chunk arithmetic. Phases 13 and 14 re-derive it. The loop's spans are
  compared against an independent derivation over a grid: every block in the
  range covered exactly once, and ``ceil((to - from + 1) / chunk)`` requests.

* the join with the codec. A ``LogRecord``'s ``topics`` and ``data`` are what
  ``codec/abi.py`` decodes in phases 13 and 14. The padded address topic is
  derived on both sides here and compared.

* the consumers of the typed row. ``assets/models.py::asset_id`` and
  ``decode/models.py::transaction_id`` are what an address and a transaction
  hash off this row become.

Every fixture here can express the pinned behaviour and its negation, and each
test names the input that flips it.
"""

from __future__ import annotations

import json
import math

import pytest

from auradefi.assets.models import asset_id
from auradefi.decode.models import transaction_id
from auradefi.errors import CaipParseError, ValidationError
from auradefi.sources.evm.codec.abi import decode, encode
from auradefi.sources.evm.logs import DEFAULT_CHUNK_BLOCKS, LogRecord, scan_logs
from auradefi.sources.evm.rpc import block_tag


def shout(value: str) -> str:
    """``value`` with its hex body upper cased and its ``0x`` prefix intact.

    ``str.upper`` would give ``0X``, which every pattern in the package
    refuses, so the fixture would test the prefix rule and not the case rule.
    """
    return "0x" + value[2:].upper()


USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_SHOUTING = shout(USDC)
VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TX = "0x" + "b" * 64


class OnlyGetLogs:
    """A node handle with exactly one method, from the declared seam alone.

    Not an ``EvmRpc`` and not a subclass of one. ``pages`` are handed out one
    per request and an exhausted queue answers with an empty list, so a scan
    that sends more requests than the vector expects still finishes and is
    caught by the request count rather than by an index error.
    """

    def __init__(self, pages: list[list[dict]] | None = None) -> None:
        self.pages = list(pages or [])
        self.seen: list[dict] = []

    def eth_get_logs(self, filter_object: dict) -> list[dict]:
        self.seen.append(filter_object)
        return self.pages.pop(0) if self.pages else []

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"scan_logs reached past the seam for rpc.{name}")

    def spans(self) -> list[tuple[str, str]]:
        return [(f["fromBlock"], f["toBlock"]) for f in self.seen]


def row(**overrides: object) -> dict:
    """One well-formed ``eth_getLogs`` row, before any override."""
    base = {
        "address": USDC,
        "topics": [TRANSFER],
        "data": "0x",
        "blockNumber": "0x1380ad0",
        "transactionHash": TX,
        "logIndex": "0x0",
    }
    base.update(overrides)
    return base


def test_the_scan_touches_the_node_only_through_eth_get_logs() -> None:
    """The third-party binding: one method, nothing else.

    A scan that called ``rpc.eth_block_number()`` to resolve a range end, or
    reached for ``rpc.url`` to key a cassette, would pass every in-repo test
    and fail here. Adding any such call flips this to red.
    """
    rpc = OnlyGetLogs([[row()]])
    records = scan_logs(rpc, from_block=20_450_000, to_block=20_450_000)
    assert len(records) == 1
    assert len(rpc.seen) == 1
    with pytest.raises(AssertionError):
        rpc.eth_call


CHUNK_VECTORS = (
    (
        20_449_000,
        20_450_000,
        500,
        [("0x13806e8", "0x13808db"), ("0x13808dc", "0x1380acf"), ("0x1380ad0", "0x1380ad0")],
    ),
    (20_450_000, 20_450_000, DEFAULT_CHUNK_BLOCKS, [("0x1380ad0", "0x1380ad0")]),
    (1000, 1999, 500, [("0x3e8", "0x5db"), ("0x5dc", "0x7cf")]),
    (1000, 1999, 5000, [("0x3e8", "0x7cf")]),
)


@pytest.mark.parametrize(("low", "high", "width", "expected"), CHUNK_VECTORS)
def test_the_pinned_spans_are_what_the_node_is_asked_for(
    low: int, high: int, width: int, expected: list[tuple[str, str]]
) -> None:
    """The chunk arithmetic, read off the wire rather than off the loop.

    The third vector is the exact-multiple case: two requests and no trailing
    empty third. Dropping the ``- 1`` from the span end makes the first vector
    ask for 0x13808db twice and fails the second pair.
    """
    rpc = OnlyGetLogs()
    scan_logs(rpc, from_block=low, to_block=high, chunk_blocks=width)
    assert rpc.spans() == expected
    assert len(rpc.seen) == math.ceil((high - low + 1) / width)


def test_the_spans_cover_every_block_once_across_a_grid_of_ranges() -> None:
    """An independent derivation of the same arithmetic, run against the loop.

    Phases 13 and 14 re-derive this. The comparison is not the formula but its
    consequences: the blocks asked for, in order, are exactly the blocks in the
    range, and the request count is the ceiling. An off-by-one at either end
    breaks the coverage list on the first row.
    """
    for low in (0, 1, 7, 1000, 20_449_000):
        for span in (1, 2, 3, 499, 500, 501, 1000, 2001):
            for width in (1, 2, 3, 7, 500, DEFAULT_CHUNK_BLOCKS):
                high = low + span - 1
                rpc = OnlyGetLogs()
                scan_logs(rpc, from_block=low, to_block=high, chunk_blocks=width)
                covered = [
                    block
                    for start, end in rpc.spans()
                    for block in range(int(start, 16), int(end, 16) + 1)
                ]
                assert covered == list(range(low, high + 1)), (low, high, width)
                assert len(rpc.seen) == math.ceil(span / width), (low, high, width)


def test_the_block_bounds_go_out_as_tags_and_parse_back_to_the_range() -> None:
    """``block_tag`` on the way out, ``int(tag, 16)`` on the way back.

    The wave-1 seam file pinned that ``rpc.eth_get_logs`` forwards an int
    ``fromBlock`` untouched, so the conversion is this module's to make. Zero
    is the case that separates ``is None`` from truthiness: block zero is
    ``"0x0"`` and never ``"latest"``.
    """
    rpc = OnlyGetLogs()
    scan_logs(rpc, from_block=0, to_block=0)
    assert rpc.seen[0]["fromBlock"] == "0x0" == block_tag(0)
    assert rpc.seen[0]["toBlock"] == "0x0"
    assert "latest" not in rpc.seen[0].values()
    rpc = OnlyGetLogs()
    scan_logs(rpc, from_block=20_450_000, to_block=20_450_000)
    assert rpc.seen[0]["fromBlock"] == block_tag(20_450_000) == "0x1380ad0"


def test_the_filter_carries_only_the_keys_the_caller_named() -> None:
    """The omit-when-absent rule, on the far side of the handle.

    ``address=None`` and ``topics=()`` omit their key entirely. A scanner that
    sent ``"address": null`` would be refused by a node that validates its
    filter, and no in-repo assertion on the returned rows would notice.
    """
    rpc = OnlyGetLogs()
    scan_logs(rpc, from_block=1, to_block=1)
    assert set(rpc.seen[0]) == {"fromBlock", "toBlock"}
    rpc = OnlyGetLogs()
    scan_logs(rpc, from_block=1, to_block=1, address=USDC_SHOUTING)
    assert rpc.seen[0]["address"] == USDC
    rpc = OnlyGetLogs()
    scan_logs(rpc, from_block=1, to_block=1, address=(USDC_SHOUTING, shout(WETH)))
    assert rpc.seen[0]["address"] == [USDC, WETH]


def test_the_topic_nesting_is_the_encodings_and_survives_json() -> None:
    """OR slots nest, wildcard slots are null, and the whole dict serialises.

    ``rpc.eth_get_logs`` hands the dict to ``httpx``'s ``json=``, so a value
    this module invents that ``json.dumps`` cannot render raises a TypeError
    from inside the transport, past the SourceError promise. A tuple would
    serialise as an array and pass; a set would not.
    """
    topic1 = "0x" + encode(("address",), (VITALIK,)).hex()
    topic2 = "0x" + encode(("address",), (USDC,)).hex()
    rpc = OnlyGetLogs()
    scan_logs(
        rpc,
        from_block=1,
        to_block=1,
        address=USDC,
        topics=(TRANSFER, None, (topic1, topic2)),
    )
    assert rpc.seen[0]["topics"] == [TRANSFER, None, [topic1, topic2]]
    assert json.loads(json.dumps(rpc.seen[0])) == rpc.seen[0]
    assert json.dumps(rpc.seen[0]).count("null") == 1


REFUSED_BEFORE_ANY_REQUEST = (
    ("inverted range", {"from_block": 10, "to_block": 9}),
    ("negative from", {"from_block": -1, "to_block": 10}),
    ("zero width", {"from_block": 1, "to_block": 10, "chunk_blocks": 0}),
    ("negative width", {"from_block": 1, "to_block": 10, "chunk_blocks": -1}),
    ("address that is not one", {"from_block": 1, "to_block": 1, "address": "0xABC"}),
    ("address in a list", {"from_block": 1, "to_block": 1, "address": [USDC, "nope"]}),
    ("topic that is an int", {"from_block": 1, "to_block": 1, "topics": (1,)}),
    ("topic OR holding None", {"from_block": 1, "to_block": 1, "topics": ((TRANSFER, None),)}),
    ("topics that is a string", {"from_block": 1, "to_block": 1, "topics": TRANSFER}),
)


@pytest.mark.parametrize(
    ("name", "kwargs"), REFUSED_BEFORE_ANY_REQUEST, ids=[n for n, _ in REFUSED_BEFORE_ANY_REQUEST]
)
def test_a_caller_mistake_reaches_no_node(name: str, kwargs: dict) -> None:
    """Validation before any HTTP, counted on the far side of the handle.

    The scanner states the property outright: a caller's mistake costs no
    request. Moving any of these checks inside the chunk loop leaves the first
    request already sent, which the count catches. A well-formed argument set
    flips every row to one request.
    """
    rpc = OnlyGetLogs()
    with pytest.raises(ValidationError):
        scan_logs(rpc, **kwargs)
    assert rpc.seen == []


def test_a_topic_the_node_will_refuse_still_costs_a_request() -> None:
    """The gap in that same property, recorded rather than asserted away.

    ``_TOPIC`` is compiled in the module and applied to every topic coming
    BACK, and the outbound address is checked against ``_ADDRESS`` before any
    request. An outbound topic is checked only for being a ``str``, so a caller
    who passes a 42-character address where the 66-character padded topic
    belongs (the two are one keystroke apart in a handler that has both) buys a
    round trip and a node-side error instead of a ValidationError. The work
    order pins only the str check, so this is a gap and not a contradiction.
    Adding a shape check to the outbound slot flips this test red.
    """
    rpc = OnlyGetLogs()
    scan_logs(rpc, from_block=1, to_block=1, topics=(VITALIK,))
    assert rpc.seen[0]["topics"] == [VITALIK]
    assert len(rpc.seen) == 1


def test_rows_accumulate_in_received_order_across_an_empty_middle_chunk() -> None:
    """Three chunks, the middle one empty, and the third still requested.

    A range scan knows its end before the first request, so an empty chunk is
    ordinary. Copying ``get_signatures``' short-page break into this loop drops
    the third chunk's rows and the third request, which both assertions catch.
    """
    first = [row(logIndex="0x0"), row(logIndex="0x1")]
    third = [row(logIndex="0x2", blockNumber="0x1380ad1")]
    rpc = OnlyGetLogs([first, [], third])
    records = scan_logs(rpc, from_block=1000, to_block=2499, chunk_blocks=500)
    assert len(rpc.seen) == 3
    assert [r.log_index for r in records] == [0, 1, 2]
    assert [r.block_number for r in records] == [20_450_000, 20_450_000, 20_450_001]


def test_the_typed_row_is_the_shape_phases_thirteen_and_fourteen_consume() -> None:
    """The ``LogRecord`` field set, its types, and what it deliberately lacks.

    Hashability is the property frozen alone would not give: a list of topics
    or a str payload would satisfy ``==`` and fail ``hash``, and a consumer
    deduplicating rows in a set is the caller that finds out. The absent
    timestamp is a field, not an oversight: ``eth_getLogs`` returns no time and
    every timestamp in this package is a millisecond epoch int.
    """
    rpc = OnlyGetLogs([[row(address=USDC_SHOUTING, transactionHash=shout(TX))]])
    (record,) = scan_logs(rpc, from_block=1, to_block=1)
    assert record == LogRecord(
        address=USDC,
        topics=(TRANSFER,),
        data=b"",
        block_number=20_450_000,
        transaction_hash=TX,
        log_index=0,
        removed=False,
    )
    assert isinstance(hash(record), int)
    assert {record, record} == {record}
    assert not hasattr(record, "timestamp")
    with pytest.raises(AttributeError):
        record.address = USDC


def test_a_transfer_log_decodes_through_the_codec_end_to_end() -> None:
    """The join phases 13 and 14 will make, made here.

    One side packs the indexed sender and recipient as address words through
    ``abi.encode``; the other reads them back off a ``LogRecord`` through
    ``abi.decode``. The padded topic is 66 characters, which is exactly what
    the row typing demands, and the amount arrives as an int with no float in
    the path. Feeding a 42-character address as topic1 breaks the row typing
    instead of producing a plausible sender.
    """
    topic_from = "0x" + encode(("address",), (VITALIK,)).hex()
    topic_to = "0x" + encode(("address",), (shout(USDC),)).hex()
    payload = "0x" + encode(("uint256",), (1_500_000,)).hex()
    assert len(topic_from) == 66
    rpc = OnlyGetLogs([[row(topics=[TRANSFER, topic_from, topic_to], data=payload)]])
    (record,) = scan_logs(rpc, from_block=1, to_block=1, topics=(TRANSFER,))
    assert record.topics[1] == topic_from
    (sender,) = decode(("address",), bytes.fromhex(record.topics[1][2:]))
    (recipient,) = decode(("address",), bytes.fromhex(record.topics[2][2:]))
    (amount,) = decode(("uint256",), record.data)
    assert (sender, recipient, amount) == (VITALIK, USDC, 1_500_000)
    assert isinstance(amount, int) and not isinstance(amount, bool)


def test_a_topic_the_caller_filtered_on_is_not_the_topic_that_comes_back() -> None:
    """The one asymmetry in the round trip, recorded.

    The filter's ``address`` is lowercased on the way out and the row's
    ``topics`` are lowercased on the way in, but an outbound topic is passed
    through as written. A handler that filters on a checksummed or uppercase
    topic and then compares ``record.topics[0]`` against the value it filtered
    on never matches, and the scan looks empty rather than wrong. Nodes compare
    topics as bytes, so the query itself is unaffected. Lowercasing the
    outbound slot flips both halves of this test.
    """
    rpc = OnlyGetLogs([[row(topics=[shout(TRANSFER)])]])
    (record,) = scan_logs(rpc, from_block=1, to_block=1, topics=(shout(TRANSFER),))
    assert rpc.seen[0]["topics"] == [shout(TRANSFER)]
    assert record.topics[0] == TRANSFER
    assert record.topics[0] != rpc.seen[0]["topics"][0]


def test_a_row_that_cannot_be_typed_reaches_no_consumer_as_a_plausible_value() -> None:
    """What an untyped address and transaction hash become downstream.

    ``address`` and ``transaction_hash`` are lowercased and are otherwise
    whatever the node sent, while ``topics`` in the same row must be
    66-character hex. So a broken node's address arrives at
    ``assets/models.py::asset_id`` and is refused there, as CaipParseError,
    a class the scanner's own taxonomy does not name and a caller catching
    SourceError does not catch. The transaction hash is worse: it is a
    preimage, so ``transaction_id`` hashes it into a well-formed id that names
    nothing. The work order pins SourceError only for a MISSING address or
    hash, so this is a gap and not a contradiction. Adding a shape check to
    either field flips the first assertion to SourceError at the scan.
    """
    rpc = OnlyGetLogs([[row(address="not-an-address", transactionHash="oops")]])
    (record,) = scan_logs(rpc, from_block=1, to_block=1)
    assert (record.address, record.transaction_hash) == ("not-an-address", "oops")
    with pytest.raises(CaipParseError):
        asset_id([f"eip155:1/erc20:{record.address}"])
    poisoned = transaction_id("eip155:1", record.transaction_hash, "acct_1")
    assert poisoned.startswith("txn_") and len(poisoned) == 20
    assert poisoned != transaction_id("eip155:1", TX, "acct_1")
