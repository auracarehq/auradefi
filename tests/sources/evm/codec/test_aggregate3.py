"""Golden vectors for the aggregate3 calldata and result layouts.

Split from ``test_abi.py`` when ``codec/abi.py`` itself split at the
400-line cap. These are the vectors for the ONE dynamic shape the codec
implements, and they carry their own reference builders precisely so the
static file next door stays about static words.

WHERE THE VECTORS COME FROM. Every expected value is a hardcoded literal
that the module under test did not produce. The two aggregate3 layouts
are pinned in RELEASE_0.2.0 §4 and reproduced here by
:func:`_aggregate3_return_blob`, a local reference builder whose
faithfulness is asserted against the committed hex before any test
relies on it. ``82ad56cb`` is published in any 4byte directory.
"""

from __future__ import annotations

import pytest

from auradefi.errors import ValidationError
from auradefi.sources.evm.codec.abi import selector
from auradefi.sources.evm.codec.aggregate3 import (
    decode_aggregate3,
    encode_aggregate3,
)

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

#: Mixed case on the way in, to pin the lowercase word on the way out.
VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"

AGGREGATE3_SELECTOR = bytes.fromhex("82ad56cb")

#: Every width the codec supports: multiples of 8 from 8 to 256.
WIDTHS = tuple(range(8, 257, 8))


def word(value: int) -> bytes:
    """One 32-byte big-endian word, two's complement for a negative."""
    return (value & ((1 << 256) - 1)).to_bytes(32, "big")


def address_word(address: str) -> bytes:
    """Twelve zero bytes then the 20 address bytes."""
    return bytes(12) + bytes.fromhex(address[2:])


def _pad32(data: bytes) -> bytes:
    return data + bytes(-len(data) % 32)


def _parse_aggregate3_calldata(blob: bytes) -> tuple[tuple[str, bool, bytes], ...]:
    """Read ``(address,bool,bytes)[]`` calldata back to its triples.

    A local reader over the pinned layout, so the round-trip test below
    is a round trip and not two calls into the same encoder.
    """
    body = blob[4:]
    count = int.from_bytes(body[32:64], "big")
    base = 64
    parsed = []
    for index in range(count):
        offset = int.from_bytes(body[base + 32 * index : base + 32 * index + 32], "big")
        start = base + offset
        target = "0x" + body[start + 12 : start + 32].hex()
        allow = int.from_bytes(body[start + 32 : start + 64], "big") == 1
        length = int.from_bytes(body[start + 96 : start + 128], "big")
        parsed.append((target, allow, body[start + 128 : start + 128 + length]))
    return tuple(parsed)


def _replace_word(blob: bytes, index: int, value: int) -> bytes:
    """``blob`` with word ``index`` overwritten, everything else intact."""
    return blob[: index * 32] + word(value) + blob[index * 32 + 32 :]


#: One call, USDC.decimals(), allow_failure True. 260 bytes.
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

#: Five calls, mixed allow_failure, call data of 4, 36, 68, 4 and 36
#: bytes, so the element offsets are all different and an encoder that
#: assumed a constant stride cannot pass. 1156 bytes.
AGGREGATE3_FIVE_CALLS = (
    "82ad56cb"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000005"
    "00000000000000000000000000000000000000000000000000000000000000a0"
    "0000000000000000000000000000000000000000000000000000000000000140"
    "0000000000000000000000000000000000000000000000000000000000000200"
    "00000000000000000000000000000000000000000000000000000000000002e0"
    "0000000000000000000000000000000000000000000000000000000000000380"
    "000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "313ce56700000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "0000000000000000000000000000000000000000000000000000000000000024"
    "70a08231000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce"
    "3606eb4800000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "0000000000000000000000000000000000000000000000000000000000000044"
    "1698ee82000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce"
    "3606eb48000000000000000000000000c02aaa39b223fe8d0a0e5c4f27ead908"
    "3c756cc200000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "0000000000000000000000000000000000000000000000000000000000000004"
    "0902f1ac00000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "0000000000000000000000000000000000000000000000000000000000000024"
    "2f745c59000000000000000000000000c02aaa39b223fe8d0a0e5c4f27ead908"
    "3c756cc200000000000000000000000000000000000000000000000000000000"
)

AGGREGATE3_FIVE_CALLS_INPUT = [
    (USDC, True, bytes.fromhex("313ce567")),
    (WETH, False, bytes.fromhex("70a08231") + address_word(USDC)),
    (USDC, True, bytes.fromhex("1698ee82") + address_word(USDC) + address_word(WETH)),
    (WETH, False, bytes.fromhex("0902f1ac")),
    (USDC, True, bytes.fromhex("2f745c59") + address_word(WETH)),
]

#: One successful result whose returndata is the uint256 word for 6,
#: which is what USDC.decimals() returns through Multicall3.
AGGREGATE3_SUCCESS_RETURN = (
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000006"
)

#: One reverted result. The returndata is empty and the failure is
#: declared in the success word, so a reader never reads it as a zero.
AGGREGATE3_FAILURE_RETURN = (
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


def _aggregate3_return_blob(results: list[tuple[bool, bytes]]) -> bytes:
    """The ``(bool,bytes)[]`` return layout, built from the pinned rules.

    Head word 0x20, array length, one offset per element measured from
    just after the length word, then each element as the success word,
    the constant inner offset 0x40, the bytes length, and the bytes
    right-padded to a multiple of 32.
    """
    elements = [
        word(1 if ok else 0) + word(0x40) + word(len(data)) + _pad32(data)
        for ok, data in results
    ]
    offsets = []
    running = 32 * len(elements)
    for element in elements:
        offsets.append(word(running))
        running += len(element)
    return word(0x20) + word(len(elements)) + b"".join(offsets) + b"".join(elements)


def _parse_aggregate3_calldata(blob: bytes) -> tuple[tuple[str, bool, bytes], ...]:
    """Read ``(address,bool,bytes)[]`` calldata back to its triples.

    A local reader over the pinned layout, so the round-trip test below
    is a round trip and not two calls into the same encoder.
    """
    body = blob[4:]
    count = int.from_bytes(body[32:64], "big")
    base = 64
    parsed = []
    for index in range(count):
        offset = int.from_bytes(body[base + 32 * index : base + 32 * index + 32], "big")
        start = base + offset
        target = "0x" + body[start + 12 : start + 32].hex()
        allow = int.from_bytes(body[start + 32 : start + 64], "big") == 1
        length = int.from_bytes(body[start + 96 : start + 128], "big")
        parsed.append((target, allow, body[start + 128 : start + 128 + length]))
    return tuple(parsed)


def _replace_word(blob: bytes, index: int, value: int) -> bytes:
    """``blob`` with word ``index`` overwritten, everything else intact."""
    return blob[: index * 32] + word(value) + blob[index * 32 + 32 :]



# --------------------------------------------------------------------
# aggregate3: the two named dynamic special cases
# --------------------------------------------------------------------


# pins: one aggregate3 call encodes to the committed 260-byte vector, so the
#       head offset, the length, the element offset and the 0x60 are all right.
def test_the_one_call_aggregate3_encode_vector() -> None:
    calldata = encode_aggregate3([(USDC, True, bytes.fromhex("313ce567"))])
    assert calldata.hex() == AGGREGATE3_ONE_CALL
    assert len(calldata) == 260


# pins: aggregate3 calldata INCLUDES the 4-byte selector, so multicall.py must
#       not prepend one and a caller can put the result straight on the wire.
def test_aggregate3_calldata_begins_with_the_multicall3_selector() -> None:
    calldata = encode_aggregate3([(USDC, True, bytes.fromhex("313ce567"))])
    assert calldata[:4] == AGGREGATE3_SELECTOR
    assert calldata[:4] == selector("aggregate3((address,bool,bytes)[])")
    assert (len(calldata) - 4) % 32 == 0


# pins: an empty call list encodes a well-formed empty array, so a batch that
#       filtered down to nothing produces the selector, 0x20 and a zero length.
def test_an_empty_call_list_encodes_an_empty_array() -> None:
    calldata = encode_aggregate3([])
    assert len(calldata) == 68
    assert calldata[:4] == AGGREGATE3_SELECTOR
    assert calldata[4:36] == word(0x20)
    assert calldata[36:68] == word(0)


# pins: five calls with call data of three different lengths encode to the
#       committed vector, so each element offset is measured, not strided.
def test_the_five_call_aggregate3_encode_vector() -> None:
    calldata = encode_aggregate3(AGGREGATE3_FIVE_CALLS_INPUT)
    assert calldata.hex() == AGGREGATE3_FIVE_CALLS
    assert len(calldata) == 1156


# pins: target, allow_failure and call data survive the encode exactly, so a
#       batch of mixed flags and lengths is not reordered or truncated.
def test_a_five_call_round_trip_preserves_target_flag_and_data() -> None:
    calldata = encode_aggregate3(AGGREGATE3_FIVE_CALLS_INPUT)
    parsed = _parse_aggregate3_calldata(calldata)
    assert parsed == tuple(AGGREGATE3_FIVE_CALLS_INPUT)
    assert [len(data) for _, _, data in parsed] == [4, 36, 68, 4, 36]
    assert [flag for _, flag, _ in parsed] == [True, False, True, False, True]


# pins: call data is right-padded to a multiple of 32 and its true length is
#       carried in the length word, so 36 bytes occupy 64 and read back as 36.
def test_call_data_is_padded_but_its_declared_length_is_exact() -> None:
    data = bytes.fromhex("70a08231") + address_word(USDC)
    calldata = encode_aggregate3([(WETH, False, data)])
    assert len(calldata) == 4 + 32 + 32 + 32 + 32 * 4 + 64
    assert calldata[-64:] == data + bytes(28)
    assert _parse_aggregate3_calldata(calldata) == ((WETH, False, data),)


# pins: the local reference builder reproduces both committed return vectors,
#       so every decode test built on it rests on the pinned layout.
def test_the_reference_return_builder_matches_the_committed_vectors() -> None:
    success = _aggregate3_return_blob([(True, bytes.fromhex("00" * 31 + "06"))])
    assert success.hex() == AGGREGATE3_SUCCESS_RETURN
    failure = _aggregate3_return_blob([(False, b"")])
    assert failure.hex() == AGGREGATE3_FAILURE_RETURN


# pins: a successful result decodes to (True, returndata) with the bytes
#       trimmed to their declared length and the padding dropped.
def test_the_aggregate3_success_decode_vector() -> None:
    results = decode_aggregate3(bytes.fromhex(AGGREGATE3_SUCCESS_RETURN))
    assert results == ((True, bytes.fromhex("00" * 31 + "06")),)
    assert type(results) is tuple
    assert len(results[0][1]) == 32


# pins: a reverted call decodes to (False, b''), an empty returndata that is
#       DECLARED, so a reader never reads the failure as a zero balance.
def test_the_aggregate3_failure_decode_vector() -> None:
    results = decode_aggregate3(bytes.fromhex(AGGREGATE3_FAILURE_RETURN))
    assert results == ((False, b""),)
    assert results[0][0] is False
    assert results[0][1] == b""
    assert results[0][1] != word(0)


# pins: an empty return array decodes to the empty tuple, so a batch of no
#       calls comes back as no results and not as an error.
def test_an_empty_return_array_decodes_to_an_empty_tuple() -> None:
    assert decode_aggregate3(_aggregate3_return_blob([])) == ()


# pins: five results with mixed success and three different returndata
#       lengths decode in order, each paired with its own success flag.
def test_five_results_decode_in_order_with_their_own_flags() -> None:
    results = [
        (True, bytes.fromhex("00" * 31 + "06")),
        (False, b""),
        (True, word(52000000000000) + word(14500000000000000000000) + word(1722470000)),
        (True, b""),
        (False, bytes.fromhex("08c379a0")),
    ]
    assert decode_aggregate3(_aggregate3_return_blob(results)) == tuple(results)


# pins: the aggregate3 return type is plain pairs of (bool, bytes), so
#       abi.py never reaches for multicall.py's richer result type.
def test_aggregate3_results_are_plain_bool_and_bytes_pairs() -> None:
    for ok, data in decode_aggregate3(bytes.fromhex(AGGREGATE3_SUCCESS_RETURN)):
        assert type(ok) is bool
        assert type(data) is bytes


# --------------------------------------------------------------------
# refusals: the dynamic special cases
# --------------------------------------------------------------------


# pins: return data shorter than the head word and the length word is refused,
#       so a truncated response never decodes to an empty result list.
def test_decode_aggregate3_refuses_data_shorter_than_two_words() -> None:
    with pytest.raises(ValidationError):
        decode_aggregate3(b"")
    with pytest.raises(ValidationError):
        decode_aggregate3(bytes(63))
    with pytest.raises(ValidationError):
        decode_aggregate3(word(0x20))


# pins: a head word other than 0x20 is refused, so a response laid out to a
#       different offset is caught before any element is read.
def test_decode_aggregate3_refuses_a_head_word_that_is_not_0x20() -> None:
    valid = bytes.fromhex(AGGREGATE3_SUCCESS_RETURN)
    assert decode_aggregate3(valid) == ((True, bytes.fromhex("00" * 31 + "06")),)
    with pytest.raises(ValidationError):
        decode_aggregate3(_replace_word(valid, 0, 0x40))
    with pytest.raises(ValidationError):
        decode_aggregate3(_replace_word(valid, 0, 0))


# pins: an element offset that points past the end of the data is refused, so
#       a crafted offset cannot read past the response.
def test_decode_aggregate3_refuses_an_element_offset_past_the_end() -> None:
    valid = bytes.fromhex(AGGREGATE3_SUCCESS_RETURN)
    with pytest.raises(ValidationError):
        decode_aggregate3(_replace_word(valid, 2, 0x1000))


# pins: a declared bytes length longer than what remains is refused, so a
#       short read is never padded out into a plausible returndata.
def test_decode_aggregate3_refuses_a_bytes_length_longer_than_the_data() -> None:
    valid = bytes.fromhex(AGGREGATE3_SUCCESS_RETURN)
    # Word 5 is the element's bytes length. The element carries 32 bytes.
    assert int.from_bytes(valid[160:192], "big") == 0x20
    with pytest.raises(ValidationError):
        decode_aggregate3(_replace_word(valid, 5, 0x40))
    with pytest.raises(ValidationError):
        decode_aggregate3(_replace_word(valid, 5, 1 << 32))


# pins: a success word that is not 0 or 1 is refused, so a dirty word never
#       reads as a successful call.
def test_decode_aggregate3_refuses_a_success_word_that_is_not_zero_or_one() -> None:
    valid = bytes.fromhex(AGGREGATE3_SUCCESS_RETURN)
    # Word 3 is the element's success flag.
    assert int.from_bytes(valid[96:128], "big") == 1
    with pytest.raises(ValidationError):
        decode_aggregate3(_replace_word(valid, 3, 2))
    with pytest.raises(ValidationError):
        decode_aggregate3(_replace_word(valid, 3, (1 << 256) - 1))


# pins: an array length the data cannot hold is refused, so a huge count does
#       not send the decoder reading offsets that are not there.
def test_decode_aggregate3_refuses_an_array_length_that_cannot_fit() -> None:
    valid = bytes.fromhex(AGGREGATE3_SUCCESS_RETURN)
    with pytest.raises(ValidationError):
        decode_aggregate3(_replace_word(valid, 1, 1 << 32))
    # Eight elements need 256 bytes of offsets after the length word, and
    # only 160 bytes follow it, so this cannot be read at all.
    with pytest.raises(ValidationError):
        decode_aggregate3(_replace_word(valid, 1, 8))


# pins: a malformed target address is refused by the special case too, so the
#       address rule holds inside aggregate3 as well as outside it.
def test_encode_aggregate3_refuses_a_malformed_target() -> None:
    with pytest.raises(ValidationError):
        encode_aggregate3([("0xABC", True, b"")])
    with pytest.raises(ValidationError):
        encode_aggregate3([(USDC[2:], True, b"")])
    with pytest.raises(ValidationError):
        encode_aggregate3(
            [(USDC, True, b""), ("0x" + "z" * 40, True, b"")]
        )


# pins: allow_failure must be True or False, so a 1 is refused and the bool
#       rule holds inside the special case.
def test_encode_aggregate3_refuses_a_non_bool_allow_failure() -> None:
    with pytest.raises(ValidationError):
        encode_aggregate3([(USDC, 1, b"")])  # type: ignore[list-item]
    with pytest.raises(ValidationError):
        encode_aggregate3([(USDC, "true", b"")])  # type: ignore[list-item]


# --------------------------------------------------------------------
