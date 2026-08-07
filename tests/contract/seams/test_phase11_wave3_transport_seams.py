"""Phase 11 wave-3 seam audit: the two transports, side by side.

``reader.py`` reads one contract per ``eth_call``. ``multicall.py`` reads many
through one ``aggregate3``. ``rpc.batch`` reads many through one JSON-RPC
array. The three were built by three separate orders, they never import each
other, and the phase order states plainly that they must NOT be unified: an
``aggregate3`` array is positional by construction, a JSON-RPC batch is matched
by ``id``, and the two failure carriers (``CallResult`` and ``BatchResult``)
are deliberately different types.

Deliberately different is exactly the condition under which two carriers drift.
So this file puts the SAME five reads down all three paths and compares:

* the calldata. The reader derives it from the registry; the multicall path
  carries whatever a caller hands it. A later phase batching the reader's reads
  will build ``Call.data`` by the reader's formula, so the two derivations are
  compared here, byte for byte, off the captured request bodies.

* the values. Four reads succeed on every path and must decode to the same
  numbers, with the length-1 unwrap applied exactly once and only by the
  reader.

* the failure INDEX. One read reverts. Element 3 of the aggregate3 array,
  item 3 of the batch (asked as id 4, answered second) and the SourceError the
  reader raises must all be about the same call. An implementation that read
  the aggregate3 array by offset order, or the batch by array position, moves
  one of them and this file names which.

* the block tag, which both modules take from ``rpc.block_tag`` and neither
  may re-derive.

* the address door. ``multicall.py`` compiled its own address pattern rather
  than importing ``chains/evm.py``'s, which makes it the fourth derivation of
  the canonical EVM address in this package. They are compared over a corpus,
  by behaviour, not by reading the regexes.

The aggregate3 response below is packed by a function written here from the
ABI layout, not by the codec that reads it, so the two are independent. Each
fixture names the input that flips its assertion.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import httpx
import pytest

from auradefi.chains.evm import normalize_address
from auradefi.errors import SourceError, ValidationError
from auradefi.sources.evm.codec.abi import decode, encode, function_signature, selector
from auradefi.sources.evm.multicall import (
    MULTICALL3_ADDRESS,
    Call,
    CallResult,
    Multicall3,
)
from auradefi.sources.evm.reader import SIGNATURES, EvmContractReader
from auradefi.sources.evm.rpc import BatchResult, EvmRpc, block_tag

NODE_URL = "https://evm-node.invalid/rpc"
BLOCK = 20_450_000
BLOCK_TAG = "0x1380ad0"

VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
V2_PAIR = "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"
REVERTER = "0x000000000000000000000000000000000000dead"

#: The five reads, in request order, with the pinned answers the wave-4 golden
#: carries for the same contracts. Index 3 is the one that reverts.
READS: tuple[tuple[str, str, tuple[object, ...]], ...] = (
    (USDC, "decimals", ()),
    (WETH, "balanceOf", (VITALIK,)),
    (V2_PAIR, "totalSupply", ()),
    (REVERTER, "decimals", ()),
    (V2_PAIR, "getReserves", ()),
)
VALUES: tuple[object, ...] = (
    6,
    2_000_000_000_000_000_000,
    850_000_000_000_000_000,
    None,
    (52_000_000_000_000, 14_500_000_000_000_000_000_000, 1_722_470_000),
)
FAILED = 3
REVERT_PAYLOAD = bytes.fromhex("08c379a0" + f"{32:064x}" + f"{4:064x}") + b"oops"


def word(value: int) -> bytes:
    """One 32-byte big-endian word."""
    return value.to_bytes(32, "big")


def result_hex(fn: str, value: object) -> str:
    """The words a node returns for ``fn``, packed from the pinned value."""
    values = value if isinstance(value, tuple) else (value,)
    return "0x" + b"".join(word(int(v)) for v in values).hex()


def aggregate3_response(results: Sequence[tuple[bool, bytes]]) -> str:
    """A ``(bool,bytes)[]`` return blob, packed here from the ABI layout.

    Written from the encoding rules and not from ``codec/abi.py``, so the
    decode this drives is checked against an independent derivation: head
    offset ``0x20``, the count, one element offset per result measured from
    just after the count word, then each element as its success word, the
    constant inner offset ``0x40``, the returndata length and the bytes padded
    up to a whole number of words.
    """
    elements = [
        word(1 if ok else 0)
        + word(0x40)
        + word(len(payload))
        + payload
        + bytes(-len(payload) % 32)
        for ok, payload in results
    ]
    offsets = b""
    running = 32 * len(elements)
    for element in elements:
        offsets += word(running)
        running += len(element)
    return "0x" + (
        word(0x20) + word(len(elements)) + offsets + b"".join(elements)
    ).hex()


class Node:
    """One ``eth_call`` node. Serves a ``(to, data)`` table, reverts a set."""

    def __init__(
        self,
        answers: dict[tuple[str, str], str],
        reverting: frozenset[str] = frozenset(),
    ) -> None:
        self.answers = answers
        self.reverting = reverting
        self.posted: list[dict] = []

    def rpc(self) -> EvmRpc:
        return EvmRpc(
            httpx.Client(transport=httpx.MockTransport(self._handle)), NODE_URL
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.posted.append(body)
        call = body["params"][0]
        if call["to"] in self.reverting:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": "execution reverted"},
                },
            )
        key = (call["to"], call["data"])
        assert key in self.answers, f"the fixture holds no read for {key}"
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": self.answers[key]}
        )


def single_node() -> Node:
    """A node answering the four readable members of :data:`READS`."""
    answers = {}
    for (address, fn, args), value in zip(READS, VALUES, strict=True):
        if value is None:
            continue
        arg_types = SIGNATURES[fn][0]
        calldata = selector(function_signature(fn, arg_types)) + encode(arg_types, args)
        answers[(address, "0x" + calldata.hex())] = result_hex(fn, value)
    return Node(answers, reverting=frozenset({REVERTER}))


def read_one_by_one() -> tuple[list[object], list[str], SourceError]:
    """The reader path: five separate ``eth_call`` posts, one of them refused.

    Returns the decoded values for the four that answered, the calldata each
    read posted in request order, and the SourceError the reverting one raised.
    """
    node = single_node()
    reader = EvmContractReader(node.rpc(), block_number=BLOCK)
    values: list[object] = []
    failure: SourceError | None = None
    for index, (address, fn, args) in enumerate(READS):
        if index == FAILED:
            with pytest.raises(SourceError) as raised:
                reader.call(address, fn, args)
            failure = raised.value
            continue
        values.append(reader.call(address, fn, args))
    assert failure is not None
    assert len(node.posted) == len(READS)
    assert all(body["params"][1] == BLOCK_TAG for body in node.posted)
    return values, [body["params"][0]["data"] for body in node.posted], failure


def test_both_batch_transports_carry_the_readers_calldata_unchanged() -> None:
    # pins: the calldata is one derivation. What the reader posts for a read is
    #       exactly what a Call must carry for the same read, and exactly what
    #       a JSON-RPC batch item must carry. Nothing re-derives a selector.
    # Flip: prepend a second selector inside the multicall path, or change one
    #       registry arg type, and the element bytes stop matching.
    _, calldata, _ = read_one_by_one()
    # Pinned literals, so the comparison does not rest on the codec that built
    # both sides: these are the four-byte selectors and the one argument word.
    assert calldata[0] == "0x313ce567"
    assert calldata[1] == "0x70a08231" + f"{int(VITALIK, 16):064x}"
    assert calldata[2] == "0x18160ddd"
    assert calldata[4] == "0x0902f1ac"
    calls = [
        Call(address, bytes.fromhex(data[2:]))
        for (address, _, _), data in zip(READS, calldata, strict=True)
    ]
    node = Node({}, reverting=frozenset())
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": aggregate3_response([(True, b"")] * len(READS)),
            },
        )

    rpc = EvmRpc(httpx.Client(transport=httpx.MockTransport(handler)), NODE_URL)
    Multicall3(rpc).aggregate3(calls, block_number=BLOCK)
    assert len(posted) == 1
    wire = posted[0]["params"][0]["data"]
    assert wire.startswith("0x82ad56cb")
    for (address, _, _), data in zip(READS, calldata, strict=True):
        assert address[2:] in wire, address
        assert data[2:] in wire, data
    assert posted[0]["params"][0]["to"] == MULTICALL3_ADDRESS
    assert posted[0]["params"][1] == BLOCK_TAG
    assert node.posted == []


def test_the_three_transports_agree_on_the_values_and_on_which_call_failed() -> None:
    # pins: the failure INDEX and the four values, across the one-call path,
    #       the positional aggregate3 array and the id-matched JSON-RPC batch.
    #       The batch is answered in reversed id order and the aggregate3
    #       elements are packed in request order, so a path that matched by the
    #       wrong discipline reports a different index here.
    # Flip: move the (False, b'') element to index 2 in the packed response and
    #       the cross-path index assertion goes red naming both indices.
    values, calldata, failure = read_one_by_one()
    assert "-32000" in str(failure)
    assert "execution reverted" in str(failure)

    payloads = [
        (False, b"")
        if index == FAILED
        else (True, bytes.fromhex(result_hex(fn, VALUES[index])[2:]))
        for index, (_, fn, _) in enumerate(READS)
    ]

    def aggregate_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": aggregate3_response(payloads),
            },
        )

    rpc = EvmRpc(
        httpx.Client(transport=httpx.MockTransport(aggregate_handler)), NODE_URL
    )
    aggregated = Multicall3(rpc).aggregate3(
        [
            Call(address, bytes.fromhex(data[2:]))
            for (address, _, _), data in zip(READS, calldata, strict=True)
        ],
        block_number=BLOCK,
    )

    def batch_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, list)
        answers = []
        for item in reversed(body):
            index = item["id"] - 1
            if index == FAILED:
                answers.append(
                    {
                        "jsonrpc": "2.0",
                        "id": item["id"],
                        "error": {"code": -32000, "message": "execution reverted"},
                    }
                )
            else:
                answers.append(
                    {
                        "jsonrpc": "2.0",
                        "id": item["id"],
                        "result": result_hex(READS[index][1], VALUES[index]),
                    }
                )
        return httpx.Response(200, json=answers)

    batch_rpc = EvmRpc(
        httpx.Client(transport=httpx.MockTransport(batch_handler)), NODE_URL
    )
    batched = batch_rpc.batch(
        [
            ("eth_call", [{"to": address, "data": data}, BLOCK_TAG])
            for (address, _, _), data in zip(READS, calldata, strict=True)
        ]
    )

    assert len(aggregated) == len(READS) == len(batched)
    assert [r.success for r in aggregated] == [
        item.error is None for item in batched
    ]
    assert [index for index, r in enumerate(aggregated) if not r.success] == [FAILED]
    assert [index for index, r in enumerate(batched) if r.error is not None] == [FAILED]

    reader_values = iter(values)
    for index, (_, fn, _) in enumerate(READS):
        if index == FAILED:
            continue
        return_types = SIGNATURES[fn][1]
        words = decode(return_types, aggregated[index].data)
        # The length-1 unwrap lives in reader.py alone, so it is applied HERE
        # by hand for the batched paths: `decode` gives a tuple either way.
        unwrapped = words[0] if len(return_types) == 1 else words
        expected = VALUES[index]
        assert unwrapped == expected, fn
        assert next(reader_values) == expected, fn
        assert batched[index].result == result_hex(fn, expected), fn
        assert decode(return_types, bytes.fromhex(batched[index].result[2:])) == words


def test_the_two_failure_carriers_declare_a_failure_and_never_coerce_one() -> None:
    # pins: the two carriers are siblings, not twins. CallResult declares with
    #       success False and keeps the returndata byte for byte; BatchResult
    #       declares with a non-None error and refuses to hold both members.
    #       Neither substitutes a zero for a call that did not answer.
    # Flip: have the aggregate3 path replace an empty returndata with a zero
    #       word and the payload equality below goes red.
    payloads = [
        (True, word(6)),
        (False, REVERT_PAYLOAD),
        (False, b""),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": aggregate3_response(payloads),
            },
        )

    rpc = EvmRpc(httpx.Client(transport=httpx.MockTransport(handler)), NODE_URL)
    results = Multicall3(rpc).aggregate3([Call(USDC, bytes.fromhex("313ce567"))] * 3)
    assert results == (
        CallResult(True, word(6)),
        CallResult(False, REVERT_PAYLOAD),
        CallResult(False, b""),
    )
    assert results[1].data == REVERT_PAYLOAD
    assert results[2].data == b""
    assert results[2].data != word(0)
    with pytest.raises(Exception):
        results[0].success = False  # type: ignore[misc]

    declared = BatchResult(None, "code=-32000 message='execution reverted'")
    assert declared.result is None and declared.error is not None
    with pytest.raises(ValidationError):
        BatchResult("0x", "both members set")
    with pytest.raises(ValidationError):
        BatchResult(None, None)
    # Opposite polarity, deliberately. A failure is `success is False` on one
    # carrier and `error is not None` on the other, so code that reads either
    # field as a truthy "this one is fine" flag inverts one of the two.
    assert CallResult(False, b"").success is False
    assert declared.error is not None
    assert bool(CallResult(False, b"").success) != bool(declared.error)


def test_the_reader_and_the_multicall_put_the_same_block_tag_on_the_wire() -> None:
    # pins: one derivation of the block parameter, rpc.block_tag, used by both
    #       modules. Zero is a real height and reads 0x0, never latest, and the
    #       tag is minimal lowercase hex with no padding.
    # Flip: zero-pad the tag in either module, or map 0 to latest, and the
    #       equality against the other module and against block_tag goes red.
    expected = {None: "latest", 0: "0x0", 1: "0x1", BLOCK: BLOCK_TAG}
    for block_number, tag in expected.items():
        assert block_tag(block_number) == tag

        read_node = Node({(USDC, "0x313ce567"): "0x" + word(6).hex()})
        EvmContractReader(read_node.rpc(), block_number=block_number).call(
            USDC, "decimals"
        )

        posted: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            posted.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": aggregate3_response([(True, word(6))]),
                },
            )

        rpc = EvmRpc(httpx.Client(transport=httpx.MockTransport(handler)), NODE_URL)
        Multicall3(rpc).aggregate3(
            [Call(USDC, bytes.fromhex("313ce567"))], block_number=block_number
        )

        assert read_node.posted[0]["params"][1] == tag
        assert posted[0]["params"][1] == tag
        assert read_node.posted[0]["params"][1] == posted[0]["params"][1]


#: One corpus, four address doors. Each entry is (value, accepted).
ADDRESS_CORPUS: tuple[tuple[object, bool], ...] = (
    (USDC, True),
    ("0x" + USDC[2:].upper(), True),
    ("0xA0b86991c6218B36c1d19D4a2e9Eb0cE3606eB48", True),
    (USDC.upper(), False),
    (USDC[:-1], False),
    (USDC + "a", False),
    (USDC[2:], False),
    (USDC + "\n", False),
    (" " + USDC, False),
    ("0x" + "g" * 40, False),
    ("0x" + "１" * 40, False),
    ("", False),
    (None, False),
    (123, False),
    (bytes.fromhex(USDC[2:]), False),
)


def test_the_multicall_address_door_agrees_with_the_canonical_evm_normaliser() -> None:
    # pins: four derivations of "an EVM address" answering identically over one
    #       corpus. chains/evm.py is the canonical one; multicall.py compiled
    #       its own for Call.target and for the deployment override, and abi.py
    #       compiled a third for the address word. All refuse with the same
    #       exception class and all lowercase on the way through.
    # Flip: relax any one of them to accept a missing 0x prefix, or to skip the
    #       lowercase, and the row for that input disagrees.
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": aggregate3_response([(True, word(6))]),
            },
        )

    rpc = EvmRpc(httpx.Client(transport=httpx.MockTransport(handler)), NODE_URL)

    def deployment(value: object) -> str:
        """The address a Multicall3 override actually posts to."""
        posted.clear()
        Multicall3(rpc, address=value).aggregate3(  # type: ignore[arg-type]
            [Call(USDC, bytes.fromhex("313ce567"))]
        )
        return str(posted[0]["params"][0]["to"])

    for value, accepted in ADDRESS_CORPUS:
        canonical: str | None
        try:
            canonical = normalize_address(value)  # type: ignore[arg-type]
        except ValidationError:
            canonical = None
        assert (canonical is not None) is accepted, value

        for door in (lambda v: Call(v, b"").target, deployment):  # type: ignore[arg-type]
            try:
                got: str | None = door(value)
            except ValidationError:
                got = None
            assert got == canonical, (value, got, canonical)

        try:
            encoded: bytes | None = encode(("address",), (value,))
        except ValidationError:
            encoded = None
        assert (encoded is not None) is accepted, value
        if accepted:
            assert encoded == encode(("address",), (canonical,))
            assert decode(("address",), encoded) == (canonical,)

    assert MULTICALL3_ADDRESS == normalize_address(MULTICALL3_ADDRESS)
