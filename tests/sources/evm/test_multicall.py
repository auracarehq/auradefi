"""Contract tests for Multicall3 aggregate3 (RELEASE_0.2.0 §4, #4).

Every request here is served by an ``httpx.MockTransport``, so the autouse
socket guard in ``tests/conftest.py`` is never bypassed and no cassette is
needed: what is under test is a wire shape and a failure taxonomy, neither
of which a recording carries.

§4's DONE-WHEN is one sentence: "One reverting call must not void the
batch; it must come back as a declared failure for that call alone." Three
tests below split it into its three falsifiable halves plus one, since a
single test that asserted all of it at once would stay green while any one
of them broke. Five calls go out as ONE eth_call; five results come back in
REQUEST order; the fourth is ``CallResult(False, b'')`` and its four
neighbours are untouched.

REQUEST ORDER IS SAFE HERE AND ONLY HERE. An aggregate3 return is an ABI
array whose element *i* IS the answer to call *i*, so nothing is matched.
``rpc.batch`` matches by JSON-RPC id because a node may reorder a
protocol-level batch array. ``tests/sources/evm/test_rpc.py`` pins that one
with a reversed-id fixture; the two disciplines are deliberately different
and must not be unified.

GOLDEN VECTORS, each derived by hand from the layouts §4 pins and hardcoded
here rather than read back from the encoder:

  MULTICALL3_ADDRESS   0xca11bde05977b3631167028862be2a173976ca11, the
      canonical deterministic deployment, byte-identical on every chain.
  0x82ad56cb           keccak256("aggregate3((address,bool,bytes)[])")[:4].
  AGGREGATE3_ONE_CALL  the 260-byte one-call calldata: the selector, the
      head word 0x20, the array length 1, one element offset of 0x20, then
      the element as the address word, the allowFailure word, the constant
      inner offset 0x60, the data length 4, and 313ce567 right-padded.
  block_tag(20_450_000) -> "0x1380ad0"  (16777216+3145728+524288+2560+208).

The return layout is the same shape with the element's inner offset at 0x40
instead of 0x60, and ``_return_blob`` below builds it straight from those
rules. It is a second implementation, written from the release text, so an
agreement between it and the codec means something.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from auradefi.errors import SourceError, ValidationError
from auradefi.sources.evm import multicall as multicall_module
from auradefi.sources.evm.multicall import (
    MULTICALL3_ADDRESS,
    Call,
    CallResult,
    Multicall3,
)
from auradefi.sources.evm.rpc import EvmRpc

URL = "https://node.example.invalid/v1"

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_CHECKSUMMED = "0xA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
#: The Uniswap V2 USDC/WETH pair, the "V2 pair" of §4's acceptance case.
PAIR = "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"
#: The target of the call that reverts. A burn address holds no code, so a
#: call into it is exactly the revert the batch has to survive.
BROKEN = "0x000000000000000000000000000000000000dead"

DECIMALS = bytes.fromhex("313ce567")
TOTAL_SUPPLY = bytes.fromhex("18160ddd")
GET_RESERVES = bytes.fromhex("0902f1ac")
BALANCE_OF = bytes.fromhex("70a08231")

AGGREGATE3_SELECTOR = "0x82ad56cb"
BLOCK = 20_450_000
BLOCK_TAG = "0x1380ad0"


def word(value: int) -> bytes:
    """One 32-byte big-endian word."""
    return (value & ((1 << 256) - 1)).to_bytes(32, "big")


def address_word(address: str) -> bytes:
    """Twelve zero bytes then the 20 address bytes."""
    return bytes(12) + bytes.fromhex(address[2:])


def _pad32(data: bytes) -> bytes:
    return data + bytes(-len(data) % 32)


def _return_blob(results: list[tuple[bool, bytes]]) -> bytes:
    """The ``(bool,bytes)[]`` return layout, built from the pinned rules.

    Head word 0x20, the array length, one offset per element measured from
    just after the length word, then each element as the success word, the
    constant inner offset 0x40, the returndata length, and the returndata
    right-padded to a multiple of 32.
    """
    elements = [
        word(1 if ok else 0) + word(0x40) + word(len(data)) + _pad32(data)
        for ok, data in results
    ]
    offsets, running = [], 32 * len(elements)
    for element in elements:
        offsets.append(word(running))
        running += len(element)
    return word(0x20) + word(len(elements)) + b"".join(offsets) + b"".join(elements)


# --------------------------------------------------------------------
# committed vectors
# --------------------------------------------------------------------

#: One call, USDC.decimals(), allow_failure True. 260 bytes. Identical to
#: the vector committed in tests/sources/evm/codec/test_abi.py, derived
#: independently from the release text, which is the point of pinning it
#: twice: the codec and its one caller agree on the wire or neither ships.
AGGREGATE3_ONE_CALL = (
    "82ad56cb"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "313ce56700000000000000000000000000000000000000000000000000000000"
)

#: The same call with allowFailure FALSE, so the only difference from the
#: vector above is the sixth word. A batch built with this flag hands the
#: revert back to the node, which answers the whole eth_call with an error.
ALLOW_FAILURE_FALSE_CALL = (
    "82ad56cb"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "313ce56700000000000000000000000000000000000000000000000000000000"
)

#: §4's acceptance batch: decimals on USDC, balanceOf on WETH, totalSupply
#: on the V2 pair, a call to a target that reverts, getReserves on the V2
#: pair. Call data of 4, 36, 4, 4 and 4 bytes, so the element offsets are
#: not a constant stride and an encoder that assumed one cannot pass.
#: 1060 bytes.
AGGREGATE3_FIVE_CALLS = (
    "82ad56cb"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000005"
    "00000000000000000000000000000000000000000000000000000000000000a0"
    "0000000000000000000000000000000000000000000000000000000000000140"
    "0000000000000000000000000000000000000000000000000000000000000200"
    "00000000000000000000000000000000000000000000000000000000000002a0"
    "0000000000000000000000000000000000000000000000000000000000000340"
    "000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "313ce56700000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "0000000000000000000000000000000000000000000000000000000000000024"
    "70a08231000000000000000000000000b4e16d0168e52d35cacd2c6185b44281"
    "ec28c9dc00000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000b4e16d0168e52d35cacd2c6185b44281ec28c9dc"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "18160ddd00000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000dead"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "313ce56700000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000b4e16d0168e52d35cacd2c6185b44281ec28c9dc"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "0902f1ac00000000000000000000000000000000000000000000000000000000"
)

def five_calls() -> list[Call]:
    """§4's acceptance batch, built per test rather than at import.

    A module-level list would construct a ``Call`` during collection,
    which turns a refused target into a collection error instead of the
    test failure it is.
    """
    return [
        Call(USDC, DECIMALS),
        Call(WETH, BALANCE_OF + address_word(PAIR)),
        Call(PAIR, TOTAL_SUPPLY),
        Call(BROKEN, DECIMALS),
        Call(PAIR, GET_RESERVES),
    ]


#: The five answers, all distinguishable, so a result read back at the
#: wrong index is a failed assertion and not a coincidence.
DECIMALS_WORD = word(6)
BALANCE_WORD = word(1_234_567_890_123_456_789_012)
SUPPLY_WORD = word(200_000_000_000_000_000_000)
#: reserve0, reserve1 and blockTimestampLast at the pinned block, the same
#: three words the codec's getReserves vector carries.
RESERVES_WORDS = (
    word(52_000_000_000_000)
    + word(14_500_000_000_000_000_000_000)
    + word(1_722_470_000)
)

FIVE_RESULTS = [
    (True, DECIMALS_WORD),
    (True, BALANCE_WORD),
    (True, SUPPLY_WORD),
    (False, b""),
    (True, RESERVES_WORDS),
]

#: An Error(string) revert payload: the 08c379a0 selector, the offset 0x20,
#: the length 29, and "ERC20: insufficient allowance" right-padded. 100
#: bytes. A contract that reverts WITH a reason sends this back, and §4
#: keeps it: the payload is the only thing that says why.
REVERT_PAYLOAD = bytes.fromhex(
    "08c379a0"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "000000000000000000000000000000000000000000000000000000000000001d"
    "45524332303a20696e73756666696369656e7420616c6c6f77616e6365000000"
)


# --------------------------------------------------------------------
# transport doubles
# --------------------------------------------------------------------


def _scripted_client(*responses: object) -> tuple[httpx.Client, list[httpx.Request]]:
    """A client replaying ``responses`` in order; the last one repeats.

    Each entry is a JSON-serialisable body or a ``(status, body)`` pair.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        spec = responses[min(len(seen) - 1, len(responses) - 1)]
        status, body = spec if isinstance(spec, tuple) else (200, spec)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def _tripwire_client() -> tuple[httpx.Client, list[httpx.Request]]:
    """A client that records and refuses every request: proves ZERO HTTP."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise RuntimeError("HTTP attempted where the contract forbids it")

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def _ok(result: str) -> dict:
    """A well-formed JSON-RPC success envelope carrying ``result``."""
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _returns(results: list[tuple[bool, bytes]]) -> dict:
    """An envelope whose result is the aggregate3 blob for ``results``."""
    return _ok("0x" + _return_blob(results).hex())


def _bodies(seen: list[httpx.Request]) -> list:
    return [json.loads(request.content) for request in seen]


def _params(seen: list[httpx.Request]) -> dict:
    """The single posted eth_call's ``[{to, data}, block]`` param array."""
    (body,) = _bodies(seen)
    return {"method": body["method"], "call": body["params"][0], "block": body["params"][1]}


def _multicall(*responses: object) -> tuple[Multicall3, list[httpx.Request]]:
    client, seen = _scripted_client(*responses)
    return Multicall3(EvmRpc(client, URL)), seen


def _tripwire() -> tuple[Multicall3, list[httpx.Request]]:
    client, seen = _tripwire_client()
    return Multicall3(EvmRpc(client, URL)), seen


SOURCE = Path(multicall_module.__file__).read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


# --------------------------------------------------------------------
# the address, the selector and the calldata
# --------------------------------------------------------------------


# pins: the module targets the canonical Multicall3 deployment with the
#       aggregate3 selector, so the address and the first four calldata
#       bytes that reach the wire are both the pinned literals.
def test_the_canonical_address_and_selector_are_what_reaches_the_wire() -> None:
    assert MULTICALL3_ADDRESS == "0xca11bde05977b3631167028862be2a173976ca11"
    assert MULTICALL3_ADDRESS == MULTICALL3_ADDRESS.lower()

    multicall, seen = _multicall(_returns([(True, DECIMALS_WORD)]))
    multicall.aggregate3([Call(USDC, DECIMALS)])

    posted = _params(seen)
    assert posted["call"]["to"] == MULTICALL3_ADDRESS
    assert posted["call"]["data"].startswith(AGGREGATE3_SELECTOR)


# pins: one Call posts the committed 260-byte aggregate3 calldata verbatim,
#       with no selector prepended on top of the one abi already includes.
def test_the_one_call_encode_vector_is_posted_verbatim() -> None:
    multicall, seen = _multicall(_returns([(True, DECIMALS_WORD)]))
    multicall.aggregate3([Call(USDC, DECIMALS)])

    posted = _params(seen)
    assert posted["method"] == "eth_call"
    assert posted["call"]["data"] == "0x" + AGGREGATE3_ONE_CALL
    # 260 bytes, so a second selector prepended would make it 264.
    assert len(bytes.fromhex(posted["call"]["data"][2:])) == 260


# pins: all five calls go out inside ONE eth_call carrying the committed
#       five-call array, never five requests and never a JSON-RPC batch.
def test_five_calls_are_one_eth_call_carrying_the_five_call_array() -> None:
    multicall, seen = _multicall(_returns(FIVE_RESULTS))
    multicall.aggregate3(five_calls())

    assert len(seen) == 1
    posted = _params(seen)
    assert posted["method"] == "eth_call"
    assert posted["call"]["data"] == "0x" + AGGREGATE3_FIVE_CALLS


# --------------------------------------------------------------------
# §4's DONE-WHEN: one revert, four survivors
# --------------------------------------------------------------------


# pins: five calls return five results in REQUEST order, each carrying the
#       exact decoded word its own call answered.
def test_five_results_come_back_in_request_order() -> None:
    multicall, _ = _multicall(_returns(FIVE_RESULTS))

    results = multicall.aggregate3(five_calls())

    assert isinstance(results, tuple)
    assert len(results) == 5
    assert results[0].data == DECIMALS_WORD
    assert results[1].data == BALANCE_WORD
    assert results[2].data == SUPPLY_WORD
    assert results[3].data == b""
    assert results[4].data == RESERVES_WORDS
    # The words read as their values, so the order pin is legible: 6
    # decimals, a balance, a supply, nothing, and three reserve words.
    assert int.from_bytes(results[0].data, "big") == 6
    assert int.from_bytes(results[1].data, "big") == 1_234_567_890_123_456_789_012
    assert int.from_bytes(results[4].data[:32], "big") == 52_000_000_000_000


# pins: the ONE reverting call in a batch of five is a declared failure for
#       that call alone, and its four neighbours still carry success True.
def test_one_reverting_call_is_a_declared_failure_and_voids_nothing_else() -> None:
    multicall, _ = _multicall(_returns(FIVE_RESULTS))

    results = multicall.aggregate3(five_calls())

    assert results[3] == CallResult(False, b"")
    assert results[3].success is False
    survivors = [results[0], results[1], results[2], results[4]]
    assert [r.success for r in survivors] == [True, True, True, True]
    assert all(r.data for r in survivors)


# pins: a declared failure is a CallResult and never an exception, a None
#       or a zero, so the batch returns normally with the failure inside it.
def test_a_declared_failure_is_a_call_result_and_not_a_raise() -> None:
    multicall, _ = _multicall(_returns(FIVE_RESULTS))

    results = multicall.aggregate3(five_calls())

    assert all(isinstance(r, CallResult) for r in results)
    assert results[3] is not None
    assert results[3].data != word(0)
    assert results[3].data == b""


# pins: a revert that carries a payload keeps those exact bytes, so the
#       reason survives to the caller instead of being discarded.
def test_a_revert_payload_comes_back_byte_for_byte() -> None:
    multicall, _ = _multicall(_returns([(False, REVERT_PAYLOAD)]))

    (result,) = multicall.aggregate3([Call(BROKEN, DECIMALS)])

    assert result == CallResult(False, REVERT_PAYLOAD)
    assert result.data == REVERT_PAYLOAD
    assert len(result.data) == 100
    assert result.data[:4] == bytes.fromhex("08c379a0")
    assert result.data[68:97] == b"ERC20: insufficient allowance"


# pins: a failed call whose returndata IS a zero word keeps that word,
#       so no failure is flattened to empty and no zero is invented for
#       one that came back empty.
def test_a_failure_carries_the_returndata_the_payload_held_and_no_other() -> None:
    payloads: list[bytes] = [b"", word(0), word(6), REVERT_PAYLOAD, b"\x00"]
    multicall, _ = _multicall(_returns([(False, p) for p in payloads]))

    results = multicall.aggregate3([Call(BROKEN, DECIMALS)] * len(payloads))

    assert [r.success for r in results] == [False] * len(payloads)
    assert [r.data for r in results] == payloads
    # The zero-word failure is NOT the empty one, and the empty one is NOT
    # a zero word: the two stay distinguishable, which is what rule 8 buys.
    assert results[0].data != results[1].data


# --------------------------------------------------------------------
# block pinning and the empty batch
# --------------------------------------------------------------------


# pins: a block_number is sent as its minimal lowercase hex tag, so a
#       historical read is pinned to that block and not to the head.
def test_a_block_number_is_sent_as_its_hex_tag() -> None:
    multicall, seen = _multicall(_returns([(True, DECIMALS_WORD)]))

    multicall.aggregate3([Call(USDC, DECIMALS)], block_number=BLOCK)

    assert _params(seen)["block"] == BLOCK_TAG
    assert _params(seen)["block"] == "0x1380ad0"


# pins: no block_number asks the node for "latest", never a hex tag and
#       never an omitted third param.
def test_no_block_number_asks_for_latest() -> None:
    multicall, seen = _multicall(_returns([(True, DECIMALS_WORD)]))

    multicall.aggregate3([Call(USDC, DECIMALS)], block_number=None)

    assert _params(seen)["block"] == "latest"


# pins: block zero is a real height and is sent as "0x0", never folded to
#       "latest" by a truthiness test on the block number.
def test_block_zero_is_a_height_and_not_latest() -> None:
    multicall, seen = _multicall(_returns([(True, DECIMALS_WORD)]))

    multicall.aggregate3([Call(USDC, DECIMALS)], block_number=0)

    assert _params(seen)["block"] == "0x0"


# pins: an empty batch returns () and issues ZERO requests, so a refresh
#       with nothing to read costs no node call.
def test_an_empty_batch_returns_nothing_and_issues_no_request() -> None:
    multicall, seen = _tripwire()

    assert multicall.aggregate3(()) == ()
    assert seen == []


# --------------------------------------------------------------------
# the entry guard: a batch the caller got wrong
# --------------------------------------------------------------------


#: One factory per case, built inside the test rather than at import. A
#: module-level generator would be spent by whichever run reached it first,
#: and a module-level Call would turn a refused target into a collection
#: error, which is the same reason five_calls() is a function.
NOT_A_SEQUENCE = [
    pytest.param(lambda: None, id="none"),
    pytest.param(lambda: (c for c in []), id="generator"),
    pytest.param(lambda: 5, id="int"),
    pytest.param(lambda: {"a": 1}, id="dict"),
    pytest.param(lambda: {1, 2}, id="set"),
    pytest.param(lambda: "ab", id="str"),
    pytest.param(lambda: "", id="empty-str"),
    pytest.param(lambda: b"", id="empty-bytes"),
]

#: A list and a tuple ARE sequences, so these reach the element check. The
#: position carried in each row is 1-based, which is what tells a caller
#: which of five calls it got wrong.
NOT_A_CALL = [
    pytest.param(lambda: [(USDC, DECIMALS, True)], 1, "tuple", id="raw-triple"),
    pytest.param(lambda: [Call(USDC, DECIMALS), None], 2, "NoneType", id="none-second"),
    pytest.param(
        lambda: (Call(USDC, DECIMALS), Call(WETH, DECIMALS), "x"),
        3,
        "str",
        id="str-third",
    ),
]


# pins: a calls argument that is not a sequence of Call is refused with
#       ValidationError on entry and before any HTTP, and the empty string
#       and the empty bytes are refused there too, so neither is read as an
#       empty batch by the len() short circuit below the guard.
@pytest.mark.parametrize("build", NOT_A_SEQUENCE)
def test_a_batch_that_is_not_a_sequence_is_refused_before_any_http(
    build: Callable[[], object],
) -> None:
    multicall, seen = _tripwire()

    with pytest.raises(ValidationError):
        multicall.aggregate3(build())

    assert seen == []


# pins: a batch element that is not a Call is refused with ValidationError
#       before any HTTP, naming that element's 1-based position and its
#       type, so a raw (target, flag, data) triple never reaches the encoder
#       as an AttributeError off .target.
@pytest.mark.parametrize(("build", "position", "kind"), NOT_A_CALL)
def test_a_batch_element_that_is_not_a_call_is_refused_before_any_http(
    build: Callable[[], object], position: int, kind: str
) -> None:
    multicall, seen = _tripwire()

    with pytest.raises(ValidationError) as excinfo:
        multicall.aggregate3(build())

    text = str(excinfo.value)
    assert f"call {position}" in text
    assert kind in text
    assert seen == []


# pins: constructing a Multicall3 performs no I/O, so a cassette or a
#       mock transport can be bound long before the first read.
def test_the_constructor_performs_no_io() -> None:
    client, seen = _tripwire_client()

    Multicall3(EvmRpc(client, URL))
    Multicall3(EvmRpc(client, URL), address="0x" + "11" * 20)

    assert seen == []


# --------------------------------------------------------------------
# the address override
# --------------------------------------------------------------------


# pins: an address override is the target the calldata is posted to,
#       instead of the canonical deployment.
def test_an_address_override_is_the_posted_target() -> None:
    override = "0x" + "11" * 20
    client, seen = _scripted_client(_returns([(True, DECIMALS_WORD)]))
    multicall = Multicall3(EvmRpc(client, URL), address=override)

    multicall.aggregate3([Call(USDC, DECIMALS)])

    assert _params(seen)["call"]["to"] == override
    assert _params(seen)["call"]["to"] != MULTICALL3_ADDRESS
    # The calldata is unchanged by the override: only the target moves.
    assert _params(seen)["call"]["data"] == "0x" + AGGREGATE3_ONE_CALL


# pins: a checksummed address override is lowercased when it is bound, so
#       the module holds one casing whatever the host configured.
def test_an_address_override_is_lowercased_when_it_is_bound() -> None:
    override = "0xCA11BDE05977B3631167028862BE2A173976CA11"
    client, _ = _tripwire_client()

    multicall = Multicall3(EvmRpc(client, URL), address=override)

    # Read off the bound attribute rather than the wire on purpose:
    # rpc.eth_call lowercases its own `to`, so a wire assertion here would
    # pass even for a module that kept the checksummed form, and a pin that
    # cannot fail is not a pin. Flip it by dropping the .lower() in
    # __init__ and this assertion goes red while every wire test stays
    # green, which is exactly the gap being covered.
    assert multicall._address == override.lower()
    assert multicall._address == MULTICALL3_ADDRESS


# --------------------------------------------------------------------
# the failure taxonomy
# --------------------------------------------------------------------


# pins: fewer decoded results than calls raises SourceError naming both
#       counts, so a short array is never zipped silently against the
#       first four calls.
def test_fewer_results_than_calls_raises_naming_both_counts() -> None:
    multicall, _ = _multicall(_returns(FIVE_RESULTS[:4]))

    with pytest.raises(SourceError) as excinfo:
        multicall.aggregate3(five_calls())

    text = str(excinfo.value)
    assert "4" in text and "5" in text


# pins: MORE decoded results than calls raises too, so the count guard is
#       an inequality and not a "did we get at least enough" test.
def test_more_results_than_calls_raises_naming_both_counts() -> None:
    multicall, _ = _multicall(_returns([*FIVE_RESULTS, (True, word(1))]))

    with pytest.raises(SourceError) as excinfo:
        multicall.aggregate3(five_calls())

    text = str(excinfo.value)
    assert "6" in text and "5" in text


# pins: a JSON-RPC error for the WHOLE eth_call, which is what a revert
#       under allow_failure False produces, raises SourceError carrying
#       the node's message rather than becoming a declared failure.
def test_a_whole_call_node_error_raises_carrying_the_message() -> None:
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32000, "message": "execution reverted"},
    }
    multicall, seen = _multicall(envelope)

    with pytest.raises(SourceError) as excinfo:
        multicall.aggregate3([Call(USDC, DECIMALS, allow_failure=False)])

    assert "execution reverted" in str(excinfo.value)
    # The flag really did go out as a zero word, so this is the
    # allow_failure False path and not some other refusal.
    assert _params(seen)["call"]["data"] == "0x" + ALLOW_FAILURE_FALSE_CALL


# pins: a result that is not hex, or is hex with an odd digit count, or is
#       empty is refused by the shape guard itself, so it never reaches
#       bytes.fromhex, whose ValueError is outside this door's taxonomy.
@pytest.mark.parametrize(
    "result",
    ["oops", "0xzz", "0x123", ""],
    ids=["not-hex", "prefixed-non-hex", "odd-digit-count", "empty"],
)
def test_a_result_that_is_not_prefixed_hex_raises(result: str) -> None:
    multicall, _ = _multicall(_ok(result))

    with pytest.raises(SourceError) as excinfo:
        multicall.aggregate3([Call(USDC, DECIMALS)])

    # Refused at the door, not downstream. The empty string in particular
    # survives bytes.fromhex and would be turned back by the codec with a
    # SourceError of its own, so the class alone cannot tell the two apart
    # and an absent __cause__ is what says the guard did the turning.
    assert excinfo.value.__cause__ is None
    assert "not 0x hex" in str(excinfo.value)


# pins: an unprefixed but otherwise valid blob is refused by the shape
#       guard itself, before result[2:] can eat its first two hex digits,
#       so the refusal names the shape and carries no cause.
def test_an_unprefixed_blob_is_refused_before_the_prefix_strip() -> None:
    # The happy-path payload with only its "0x" removed: even digit count,
    # every character hex, decodes cleanly the moment it is prefixed. The
    # missing prefix is therefore the only thing it can be refused for.
    unprefixed = _return_blob([(True, DECIMALS_WORD)]).hex()
    multicall, _ = _multicall(_ok(unprefixed))

    with pytest.raises(SourceError) as excinfo:
        multicall.aggregate3([Call(USDC, DECIMALS)])

    # The discriminator, and the reason `pytest.raises(SourceError)` alone
    # is not enough here: BOTH sides of this branch end in SourceError. A
    # guard that made the prefix optional would let the blob through,
    # strip "00" off the head word, shift every word left by a byte and
    # land in the codec, which raises SourceError too, chained from the
    # abi ValidationError. Refused at the door means nothing was decoded,
    # which is what an absent __cause__ and the shape wording say.
    assert excinfo.value.__cause__ is None
    assert "not 0x hex" in str(excinfo.value)

    # The control arm, which is what makes the fixture able to be wrong:
    # the same bytes WITH the prefix are accepted and decode to the answer.
    accepting, _ = _multicall(_ok("0x" + unprefixed))
    assert accepting.aggregate3([Call(USDC, DECIMALS)]) == (
        CallResult(True, DECIMALS_WORD),
    )


# pins: bytes that came off the wire and do not decode raise SourceError
#       with the abi ValidationError as the cause, so the reason survives
#       in the traceback and the taxonomy stays SourceError at this door.
def test_a_truncated_payload_raises_chained_from_the_abi_error() -> None:
    truncated = _return_blob(FIVE_RESULTS)[:96]
    multicall, _ = _multicall(_ok("0x" + truncated.hex()))

    with pytest.raises(SourceError) as excinfo:
        multicall.aggregate3(five_calls())

    assert isinstance(excinfo.value.__cause__, ValidationError)


# --------------------------------------------------------------------
# the Call carrier
# --------------------------------------------------------------------


# pins: a target that is not a 0x-prefixed 40-hex string is refused at
#       construction with ValidationError, before any HTTP.
@pytest.mark.parametrize(
    "target",
    [
        "0xABC",
        "0x",
        "",
        "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        USDC + "0",
        USDC[:-1],
        "0xg0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        USDC + "\n",
        None,
        123,
    ],
)
def test_a_malformed_target_is_refused_before_any_http(target: object) -> None:
    _, seen = _tripwire_client()

    with pytest.raises(ValidationError):
        Call(target, DECIMALS)

    assert seen == []


# pins: a checksummed target is lowercased at construction and reaches the
#       wire lowercased, which is what keeps one address one key.
def test_a_mixed_case_target_is_lowercased_at_construction() -> None:
    call = Call(USDC_CHECKSUMMED, DECIMALS)
    assert call.target == USDC

    multicall, seen = _multicall(_returns([(True, DECIMALS_WORD)]))
    multicall.aggregate3([call])

    assert _params(seen)["call"]["data"] == "0x" + AGGREGATE3_ONE_CALL


# pins: allow_failure defaults to True, so the isolated-failure behaviour
#       is what a caller gets without asking for it.
def test_allow_failure_defaults_to_true_and_goes_out_as_a_one_word() -> None:
    call = Call(USDC, DECIMALS)
    assert call.allow_failure is True

    multicall, seen = _multicall(_returns([(True, DECIMALS_WORD)]))
    multicall.aggregate3([call])

    # The sixth word of the calldata is the allowFailure flag.
    body = bytes.fromhex(_params(seen)["call"]["data"][2:])[4:]
    assert body[128:160] == word(1)


# pins: allow_failure False goes out as a zero word, so the caller really
#       can ask the node to void the whole batch on a revert.
def test_allow_failure_false_goes_out_as_a_zero_word() -> None:
    multicall, seen = _multicall(_returns([(True, DECIMALS_WORD)]))

    multicall.aggregate3([Call(USDC, DECIMALS, allow_failure=False)])

    assert _params(seen)["call"]["data"] == "0x" + ALLOW_FAILURE_FALSE_CALL
    body = bytes.fromhex(_params(seen)["call"]["data"][2:])[4:]
    assert body[128:160] == word(0)


# pins: Call and CallResult are frozen and slotted, so a decoded batch
#       cannot be edited in place and neither carries a __dict__.
def test_call_and_call_result_are_frozen_and_slotted() -> None:
    call = Call(USDC, DECIMALS)
    result = CallResult(False, b"")

    with pytest.raises(dataclasses.FrozenInstanceError):
        call.target = WETH
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.success = True
    assert not hasattr(call, "__dict__")
    assert not hasattr(result, "__dict__")
    assert call == Call(USDC, DECIMALS)
    assert result == CallResult(False, b"")
    assert result != CallResult(True, b"")


# --------------------------------------------------------------------
# structure: the seams consumed, and the zero never substituted
# --------------------------------------------------------------------


def _identifiers() -> set[str]:
    """Every Name id and Attribute attr in the module, prose excluded."""
    names = {n.id for n in ast.walk(TREE) if isinstance(n, ast.Name)}
    return names | {n.attr for n in ast.walk(TREE) if isinstance(n, ast.Attribute)}


def _imported() -> set[str]:
    """Every module path the module imports."""
    paths: set[str] = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            paths.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            paths.add(node.module)
    return paths


# pins: the module consumes the four declared seams and builds its own
#       encoding for none of them.
def test_the_declared_seams_are_the_ones_consumed() -> None:
    used = _identifiers()
    for seam in ("encode_aggregate3", "decode_aggregate3", "eth_call", "block_tag"):
        assert seam in used, f"{seam} is a declared seam and must be consumed"


# pins: the module opens no transport of its own and reaches into no
#       forbidden layer, so the injected EvmRpc is the only way out.
def test_the_module_imports_only_its_declared_layers() -> None:
    imported = _imported()
    # aggregate3 moved out of codec/abi.py when that module split at its
    # line cap; the calldata seam is the same one, under its own name.
    assert "auradefi.sources.evm.codec.aggregate3" in imported
    assert "auradefi.sources.evm.rpc" in imported
    forbidden = [
        path
        for path in imported
        if path.split(".")[0] in {"httpx", "requests", "urllib", "socket", "aiohttp"}
        or path.startswith(("auradefi.decode", "auradefi.positions", "auradefi.prices"))
    ]
    assert not forbidden, f"multicall.py must not import {forbidden}"


# pins: every CallResult the module builds is built from the decoded pair
#       and never from a literal, so no zero and no empty bytes can be
#       substituted for what the wire actually carried.
def test_a_call_result_is_never_built_from_a_literal() -> None:
    constructions = [
        node
        for node in ast.walk(TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "CallResult"
    ]
    assert constructions, "multicall.py must build its CallResults"
    for node in constructions:
        arguments = list(node.args) + [keyword.value for keyword in node.keywords]
        literals = [ast.dump(a) for a in arguments if isinstance(a, ast.Constant)]
        assert not literals, f"a CallResult built from a literal: {literals}"


# pins: only classes from errors.py are raised, so no local exception
#       type escapes the taxonomy an embedding host catches on.
def test_only_declared_error_classes_are_raised() -> None:
    raised = {
        node.exc.func.id
        for node in ast.walk(TREE)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
    }
    assert raised <= {"SourceError", "ValidationError", "NotImplementedError"}
