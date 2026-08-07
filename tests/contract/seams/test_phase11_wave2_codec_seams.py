"""Phase 11 wave-2 seam audit: the boundaries around ``codec/abi.py``.

Nothing here looks inside the codec. Every assertion crosses a boundary that
no single work order owned:

* the wave-4 recorded wire. ``tests/golden/test_phase11_reader.py`` was
  hand-packed before ``abi.py`` existed: nineteen ``(to, calldata, result)``
  triples plus their decoded values in ``DICT_READS``, and two hand-laid
  Multicall3 payloads. Those are the OTHER derivation of every word the codec
  produces and consumes. The wave-1 seam file compared the four-byte
  selectors; this one compares the argument words, the return words and both
  aggregate3 layouts. The golden is imported as data so the values compared
  are the ones that gate carries, never a copy that could drift.

* the registry the codec is called through. RELEASE_0.2.0 section 4
  enumerates the whole ``ContractReader`` call surface as a table of argument
  and return types. ``reader.py`` (wave 3) carries it as data. The copy below
  was transcribed from that table, so if the codec cannot spell a type the
  table names, this file goes red before ``reader.py`` is written.

* the length-1 unwrap. ``decode`` always returns a tuple and the unwrap lives
  in ``reader.py``. That is only checkable from outside: applied here ONCE,
  against ``DICT_READS``, which stores a bare scalar for a single-return
  function and a tuple for a multi-return one.

* the address, in three derivations. ``chains/evm.py::normalize_address`` is
  the package's canonical EVM address, ``logs.py`` compiled its own pattern,
  and ``abi.py`` compiled a third. They are compared by behaviour over a
  corpus, not by reading the three regexes.

* the pinned identifiers. ``grp_9b813f4a0ae43e5b`` is the value the codec
  docstring names as the thing its lowercase decode protects.

Every fixture here can express the pinned behaviour and its negation, and
each test names the input that flips it.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from auradefi.chains.evm import normalize_address
from auradefi.errors import ValidationError
from auradefi.positions.models import group_id_for, position_id
from auradefi.sources.evm.codec.abi import (
    decode,
    encode,
    function_signature,
    selector,
)
from auradefi.sources.evm.codec.aggregate3 import (
    decode_aggregate3,
    encode_aggregate3,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_PATH = REPO_ROOT / "tests" / "golden" / "test_phase11_reader.py"
ABI_PATH = REPO_ROOT / "src" / "auradefi" / "sources" / "evm" / "codec" / "abi.py"

#: RELEASE_0.2.0 section 4's call-surface table, transcribed as
#: ``fn -> (arg_types, return_types)``. Return types are always a tuple here,
#: because that is what ``decode`` is declared to take and to give back. The
#: ``positions`` row is section 4's "12-tuple, uint96/address/uint24/int24/
#: uint128/uint256", spelled out in the order the NonfungiblePositionManager
#: returns it.
REGISTRY: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "balanceOf": (("address",), ("uint256",)),
    "decimals": ((), ("uint8",)),
    "totalSupply": ((), ("uint256",)),
    "token0": ((), ("address",)),
    "token1": ((), ("address",)),
    "getReserves": ((), ("uint112", "uint112", "uint32")),
    "allPairsLength": ((), ("uint256",)),
    "allPairs": (("uint256",), ("address",)),
    "slot0": (
        (),
        ("uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"),
    ),
    "positions": (
        ("uint256",),
        (
            "uint96",
            "address",
            "address",
            "address",
            "uint24",
            "int24",
            "int24",
            "uint128",
            "uint256",
            "uint256",
            "uint128",
            "uint128",
        ),
    ),
    "getPool": (("address", "address", "uint24"), ("address",)),
    "tokenOfOwnerByIndex": (("address", "uint256"), ("uint256",)),
    "getUserAccountData": (("address",), ("uint256",) * 6),
    "getExchangeRate": ((), ("uint256",)),
}

V3_POOL = "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8"
VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
TARGET = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def golden() -> object:
    """The wave-4 gate module, imported for its recorded wire only."""
    spec = importlib.util.spec_from_file_location("_wave2_golden_wire", GOLDEN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unwrap(values: tuple[object, ...]) -> object:
    """The length-1 unwrap the seam places in ``reader.py``, applied once.

    Written here so the codec side and the consumer side are visibly two
    different pieces of code. If ``decode`` ever unwraps as well, this
    returns a scalar's first element and every comparison below fails.
    """
    return values[0] if len(values) == 1 else values


def test_every_recorded_calldata_is_the_selector_and_words_the_codec_emits() -> None:
    """The nineteen hand-packed request bodies, re-derived through the codec.

    One side is ``tests/golden/test_phase11_reader.py``'s FIXTURE, packed word
    by word before this codec existed. The other is
    ``selector(function_signature(fn, arg_types)) + encode(arg_types, args)``
    over the same reads. A checksummed argument, a missed zero pad or a
    ``uint24`` written as ``uint`` flips this to red.
    """
    module = golden()
    recorded = {(to, calldata) for to, calldata, _ in module.FIXTURE}
    rebuilt = set()
    for (address, fn, args), _value in module.DICT_READS.items():
        arg_types, _returns = REGISTRY[fn]
        body = selector(function_signature(fn, arg_types)) + encode(arg_types, args)
        rebuilt.add((address, "0x" + body.hex()))
    assert rebuilt == recorded
    assert len(rebuilt) == 19


def test_every_recorded_result_decodes_to_the_hand_written_fixture_value() -> None:
    """The other half of the same boundary: the recorded return words.

    ``DICT_READS`` is what the phase-4 goldens already resolve against, so it
    is an independent statement of what those words mean. The comparison is
    exact and typed: ``getReserves`` must come back as three ints in that
    order, ``slot0``'s seventh word as ``True`` and not ``1``, and
    ``positions``' operator as the lowercase zero address.
    """
    module = golden()
    by_call = {(to, calldata): result for to, calldata, result in module.FIXTURE}
    checked = 0
    for (address, fn, args), expected in module.DICT_READS.items():
        arg_types, return_types = REGISTRY[fn]
        body = selector(function_signature(fn, arg_types)) + encode(arg_types, args)
        result = by_call[(address, "0x" + body.hex())]
        decoded = decode(return_types, bytes.fromhex(result[2:]))
        assert unwrap(decoded) == expected, f"{fn}{args} at {address}"
        checked += 1
    assert checked == 19


def test_slot0_and_positions_keep_their_python_types_across_the_boundary() -> None:
    """Equality is not enough where ``True == 1`` and ``1 == True``.

    ``slot0`` ends in a ``bool`` and ``positions`` starts with a ``uint96``
    that happens to be zero. A codec that returned ``1`` for the unlocked flag
    would satisfy the equality above and fail here, which is the case the
    phase 13 and 14 handlers would inherit.
    """
    module = golden()
    slot0 = module.DICT_READS[(V3_POOL, "slot0", ())]
    by_call = {(to, calldata): result for to, calldata, result in module.FIXTURE}
    body = selector(function_signature("slot0", ())) + encode((), ())
    decoded = decode(REGISTRY["slot0"][1], bytes.fromhex(by_call[(V3_POOL, "0x" + body.hex())][2:]))
    assert decoded[6] is True
    assert isinstance(slot0[6], bool)
    assert [type(v) for v in decoded] == [type(v) for v in slot0]


def test_decode_never_unwraps_so_exactly_one_layer_can() -> None:
    """A single type gives a length-1 tuple, on both a scalar and an address.

    Two layers that both unwrap is a defect neither can see alone, and the
    only place to see it is here. Flips to red the moment ``decode`` returns
    the bare value.
    """
    word = bytes(31) + b"\x06"
    assert decode(("uint256",), word) == (6,)
    assert decode(("uint256",), word) != 6
    assert decode(("address",), bytes(12) + bytes.fromhex(TARGET[2:])) == (TARGET,)
    assert unwrap(decode(("uint256",), word)) == 6


def test_the_hand_packed_five_call_calldata_is_what_the_codec_emits() -> None:
    """``encode_aggregate3`` against the wave-4 batch, packed by hand.

    The vector carries elements of three different data lengths (4, 36 and 4
    bytes), so an offset table computed with a fixed stride rather than a
    running total produces a plausible payload and fails here. It also proves
    the selector is INCLUDED: prepending one in ``multicall.py`` would double
    it, and stripping it here would shift every offset.
    """
    module = golden()
    calls = [
        (module.USDC, True, bytes.fromhex("313ce567")),
        (
            module.WETH,
            True,
            bytes.fromhex("70a08231") + encode(("address",), (VITALIK,)),
        ),
        (module.V2_PAIR, True, bytes.fromhex("18160ddd")),
        (module.REVERTER, True, bytes.fromhex("313ce567")),
        (module.V2_PAIR, True, bytes.fromhex("0902f1ac")),
    ]
    calldata = encode_aggregate3(calls)
    assert "0x" + calldata.hex() == module.FIVE_CALL_CALLDATA
    assert calldata[:4].hex() == "82ad56cb"


def test_the_hand_packed_five_call_results_are_what_the_codec_reads() -> None:
    """``decode_aggregate3`` against both wave-4 return payloads.

    The fourth call is the reverter. In the first payload it comes back with
    empty returndata and in the second with an ``Error(string)`` body, and in
    both the failure is DECLARED rather than defaulted: ``(False, b'')`` is a
    declared empty payload and never a zero. Section 4's acceptance criterion,
    four results and one declared failure, is exactly this.
    """
    module = golden()
    plain = decode_aggregate3(bytes.fromhex(module.FIVE_CALL_RESULT[2:]))
    assert [ok for ok, _ in plain] == [True, True, True, False, True]
    assert plain[3] == (False, b"")
    assert decode(("uint8",), plain[0][1]) == (6,)
    assert decode(("uint112", "uint112", "uint32"), plain[4][1]) == (
        52_000_000_000_000,
        14_500_000_000_000_000_000_000,
        1_722_470_000,
    )
    with_payload = decode_aggregate3(
        bytes.fromhex(module.FIVE_CALL_RESULT_WITH_PAYLOAD[2:])
    )
    assert with_payload[3][0] is False
    assert with_payload[3][1].hex() == module.REVERT_PAYLOAD
    assert [ok for ok, _ in with_payload] == [True, True, True, False, True]


def test_the_codec_imports_no_transport_and_owns_no_result_type() -> None:
    """The layering inside the package, read off the file rather than assumed.

    A codec importing ``multicall.py`` for its result type would invert the
    layering the seam pins, and every in-repo test would still pass. Adding
    ``from auradefi.sources.evm.multicall import CallResult`` flips this red.
    """
    imported = set()
    for node in ast.walk(ast.parse(ABI_PATH.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported == {
        "__future__",
        "auradefi.errors",
        "auradefi.sources.evm.codec.keccak",
        "collections.abc",
        "re",
    }
    results = decode_aggregate3(bytes.fromhex("00" * 31 + "20" + "00" * 32))
    assert results == ()
    assert type(decode_aggregate3(bytes.fromhex("00" * 31 + "20" + "00" * 32))) is tuple


ADDRESS_CORPUS = (
    ("0x" + "a" * 40, True),
    ("0x" + "A" * 40, True),
    ("0X" + "a" * 40, False),
    ("0x" + "a" * 39, False),
    ("0x" + "a" * 41, False),
    ("0x" + "a" * 40 + "\n", False),
    (" 0x" + "a" * 40, False),
    ("0x" + "g" * 40, False),
    ("0x١" + "a" * 38, False),
    ("d8da" * 10, False),
    ("0x", False),
    ("", False),
)


@pytest.mark.parametrize(("candidate", "accepted"), ADDRESS_CORPUS)
def test_the_codec_and_the_canonical_address_accept_the_same_strings(
    candidate: str, accepted: bool
) -> None:
    """Two derivations of "an EVM address", compared by running both.

    ``chains/evm.py::normalize_address`` is the package's canonical form and
    ``abi.py`` compiled its own pattern. A codec that accepted a bare 40-hex
    string would encode a plausible word for an argument the rest of the
    package refuses, which is how a wrong target reaches a node. Every row
    flips the assertion by construction: the first two are the only accepted
    ones.
    """
    codec_ok = True
    try:
        encode(("address",), (candidate,))
    except ValidationError:
        codec_ok = False
    canonical_ok = True
    try:
        normalize_address(candidate)
    except ValidationError:
        canonical_ok = False
    assert codec_ok is accepted
    assert canonical_ok is accepted


def test_a_pool_address_off_the_wire_reaches_the_pinned_identifiers() -> None:
    """``getPool``'s decode, carried through to the ids the goldens pin.

    The decoded address is the group key for Uniswap V3 and the contract for
    ``position_id``. Both pinned values are reproduced from the address the
    codec returns, so a decode that dropped the ``0x``, kept the high twelve
    bytes or emitted 20 raw bytes changes both ids and fails here.
    """
    module = golden()
    by_call = {(to, calldata): result for to, calldata, result in module.FIXTURE}
    arg_types, return_types = REGISTRY["getPool"]
    body = selector(function_signature("getPool", arg_types)) + encode(
        arg_types, (module.USDC, module.WETH, 3000)
    )
    (pool,) = decode(return_types, bytes.fromhex(by_call[(module.V3_FACTORY, "0x" + body.hex())][2:]))
    assert pool == V3_POOL
    assert group_id_for("uniswap-v3", "eip155:1", pool) == "grp_9b813f4a0ae43e5b"
    assert (
        position_id("uniswap-v3", "eip155:1", module.V3_MANAGER, "912345")
        == "pos_447985e390bf1d89"
    )


def test_the_pinned_group_id_survives_a_codec_that_decoded_upper_case() -> None:
    """The seam's claim about the group id, checked rather than believed.

    ``abi.py`` says its lowercase decode "is what keeps the Uniswap V3 pinned
    group id grp_9b813f4a0ae43e5b intact". It is not: ``group_id_for`` runs
    ``_lower_0x`` over the group key itself, the V3 adapter lowercases
    ``token0``/``token1`` before building asset ids, and ``eth_call``
    lowercases its target. So the id is protected twice and the codec's case
    is load bearing for nothing downstream. This test records that, and goes
    red the day a consumer stops normalising, which is when the claim would
    become true and would need a test of its own.
    """
    shouting = "0x" + V3_POOL[2:].upper()
    assert group_id_for("uniswap-v3", "eip155:1", shouting) == "grp_9b813f4a0ae43e5b"
    assert position_id("uniswap-v3", "eip155:1", shouting) == position_id(
        "uniswap-v3", "eip155:1", V3_POOL
    )


REFUSED_AS_DECLARED = (
    ("unsupported type", lambda: encode(("string",), ("hello",))),
    ("array type", lambda: encode(("uint256[]",), ((1,),))),
    ("length mismatch", lambda: encode(("uint256", "uint256"), (1,))),
    ("out of range", lambda: encode(("uint8",), (256,))),
    ("bool for an int", lambda: encode(("uint256",), (True,))),
    ("int for a bool", lambda: encode(("bool",), (1,))),
    ("malformed address", lambda: encode(("address",), ("0xABC",))),
    ("short data", lambda: decode(("uint256",), bytes(31))),
    ("dirty address word", lambda: decode(("address",), b"\x01" + bytes(31))),
    ("bool word of two", lambda: decode(("bool",), bytes(31) + b"\x02")),
    ("truncated aggregate3", lambda: decode_aggregate3(bytes(32))),
    ("aggregate3 head", lambda: decode_aggregate3(bytes(64))),
)


@pytest.mark.parametrize(("name", "call"), REFUSED_AS_DECLARED, ids=[n for n, _ in REFUSED_AS_DECLARED])
def test_the_declared_refusals_arrive_as_validation_error(name: str, call: object) -> None:
    """The half of the error boundary that holds.

    ``reader.py`` and ``multicall.py`` translate ValidationError into
    SourceError when the bytes came off the wire, so anything the codec
    refuses through another class escapes that translation. These twelve
    inputs are refused as declared. Flips to red if any of them starts
    raising something else, or nothing.
    """
    with pytest.raises(ValidationError):
        call()


LEAKS_PAST_THE_DECLARED_ERROR = (
    ("call tuple of two", lambda: encode_aggregate3([(TARGET, True)])),
    ("call tuple of four", lambda: encode_aggregate3([(TARGET, True, b"", b"")])),
    ("call that is a string", lambda: encode_aggregate3(["0xdeadbeef"])),
    ("call data as a hex string", lambda: encode_aggregate3([(TARGET, True, "313ce567")])),
    ("call data as an int", lambda: encode_aggregate3([(TARGET, True, 4)])),
    ("aggregate3 return as text", lambda: decode_aggregate3("00" * 64)),
    ("decode over text", lambda: decode(("uint256",), "x" * 32)),
)


@pytest.mark.parametrize(
    ("name", "call"),
    LEAKS_PAST_THE_DECLARED_ERROR,
    ids=[n for n, _ in LEAKS_PAST_THE_DECLARED_ERROR],
)
def test_every_malformed_input_arrives_as_validation_error(name: str, call: object) -> None:
    """The declared error boundary, over the inputs the acceptance list missed.

    The seam reads: "abi.py raises ValidationError for every malformed input;
    reader.py and multicall.py translate that to SourceError when the bytes
    came off the wire". A ValueError from an unguarded three-way unpack or a
    TypeError from ``str + bytes`` is neither, so a ``multicall.py`` that
    catches ValidationError as instructed still lets these reach its caller
    raw. ``rpc.py`` already guards the identical shape on the same wave-1
    boundary: "Each entry is CHECKED, never unpacked hopefully". Passing a
    well-formed ``(target, allow_failure, bytes)`` triple flips every row
    green.
    """
    with pytest.raises(ValidationError):
        call()
