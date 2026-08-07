"""Golden vectors for the static ABI codec (RELEASE_0.2.0 §4).

WHERE THE VECTORS COME FROM. Every expected value below is a hardcoded
literal, and none of it was produced by the module under test.

* The fifteen selectors were derived with a throwaway keccak256 written
  from the pinned construction in ``codec/keccak.py``'s docstring and
  structured differently from it (one flat 25-lane list indexed
  ``x + 5y``). That throwaway was corroborated against
  ``hashlib.sha3_256`` for every message length from 0 to 299 by swapping
  its pad byte from 0x01 to 0x06, which is the only difference between
  the two functions. Twelve of the fifteen are also published in any
  4byte directory, so a reader can check them without this repository.
* The word-level vectors follow from the pinned format alone: 32-byte
  big-endian, address right-aligned behind 12 zero bytes, ``int<N>`` in
  two's complement sign-extended across all 256 bits.
* The ``getReserves`` word group is the shape at the pinned block, and
  its three decoded values are stated in the release.
* The aggregate3 layouts moved to ``test_aggregate3.py`` with the code
  they cover, so this file is static words only.

The refusal set is the load-bearing part of this file. A codec that
silently mis-encodes ``uint256[]`` as one word, or reads a ``uint112``
word with dirt above bit 112 as a reserve, produces a portfolio that is
wrong and confident. Each unsupported spelling gets its own assertion.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from auradefi.errors import ValidationError
from auradefi.sources.evm.codec import abi
from auradefi.sources.evm.codec.abi import (
    decode,
    encode,
    function_signature,
    selector,
)
from auradefi.sources.evm.codec.keccak import keccak256

# --------------------------------------------------------------------
# fixtures and local reference builders
# --------------------------------------------------------------------

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


# --------------------------------------------------------------------
# committed vectors
# --------------------------------------------------------------------

#: signature -> four-byte selector. Derived as described in the module
#: docstring; twelve are published in any 4byte directory.
SELECTORS = [
    ("balanceOf(address)", "70a08231"),
    ("decimals()", "313ce567"),
    ("totalSupply()", "18160ddd"),
    ("token0()", "0dfe1681"),
    ("token1()", "d21220a7"),
    ("getReserves()", "0902f1ac"),
    ("allPairsLength()", "574f2ba3"),
    ("allPairs(uint256)", "1e3dd18b"),
    ("slot0()", "3850c7bd"),
    ("positions(uint256)", "99fbab88"),
    ("getPool(address,address,uint24)", "1698ee82"),
    ("tokenOfOwnerByIndex(address,uint256)", "2f745c59"),
    ("getUserAccountData(address)", "bf92857c"),
    ("getExchangeRate()", "e6aa216c"),
    ("aggregate3((address,bool,bytes)[])", "82ad56cb"),
]

GETPOOL_CALLDATA = (
    "1698ee82"
    "000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    "000000000000000000000000c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    "0000000000000000000000000000000000000000000000000000000000000bb8"
)

POSITIONS_CALLDATA = (
    "99fbab88"
    "00000000000000000000000000000000000000000000000000000000000debd9"
)

#: The getReserves word group at the pinned block: reserve0, reserve1
#: and blockTimestampLast as uint112, uint112, uint32.
GETRESERVES_WORDS = (
    "00000000000000000000000000000000000000000000000000002f4b31874000"
    "0000000000000000000000000000000000000000000003120bec57b51c100000"
    "0000000000000000000000000000000000000000000000000000000066aace70"
)
GETRESERVES_VALUES = (52000000000000, 14500000000000000000000, 1722470000)






#: Every spelling the codec must refuse. Dynamic types, arrays, tuples,
#: bad widths, the Solidity aliases whose canonical form is uint256 and
#: int256, the fixed-point family, two wrong-case names, and the three
#: leading-zero widths, which are the only entries here that would encode
#: to a perfectly well-formed word if the codec normalised them.
UNSUPPORTED_TYPES = [
    "string",
    "bytes",
    "bytes32",
    "uint256[]",
    "uint256[2]",
    "(uint256,uint256)",
    "tuple",
    "uint7",
    "uint264",
    "uint0",
    "int",
    "uint",
    "fixed128x18",
    "ufixed",
    "function",
    "ADDRESS",
    "Bool",
    "uint08",
    "uint0256",
    "int024",
]

#: A value that WOULD be valid if the name beside it were accepted. Without
#: this, a codec that case-folded its type names would still raise on the
#: value and the wrong-case entries above would prove nothing.
VALID_VALUE_FOR_WRONG_CASE = {"ADDRESS": USDC, "Bool": True}

#: A leading-zero width beside the canonical spelling it would normalise
#: to. Both members of each pair encode to the SAME word, so the word is no
#: evidence at all: what separates them is the selector taken over the
#: signature, which is why the padded spelling has to be refused outright.
LEADING_ZERO_WIDTHS = [
    ("uint08", "uint8"),
    ("uint0256", "uint256"),
    ("int024", "int24"),
]


# --------------------------------------------------------------------
# selectors and canonical signatures
# --------------------------------------------------------------------


# pins: each of the fifteen signatures this release calls resolves to its
#       published four-byte selector, so a call reaches the function named.
@pytest.mark.parametrize(("signature", "expected"), SELECTORS)
def test_the_fifteen_published_selectors(signature: str, expected: str) -> None:
    assert selector(signature).hex() == expected


# pins: a selector is exactly the first four bytes of the digest, so it is
#       four bytes of type bytes and never the whole 32.
def test_a_selector_is_four_bytes() -> None:
    result = selector("getReserves()")
    assert type(result) is bytes
    assert len(result) == 4


# pins: selector derives from keccak256 of the ASCII signature, so it tracks
#       this package's keccak and is not a lookup table of known names.
def test_a_selector_is_the_first_four_digest_bytes_of_the_ascii_signature() -> None:
    for signature, _ in SELECTORS:
        assert selector(signature) == keccak256(signature.encode())[:4]
    # A name nobody has tabulated, to close the lookup-table reading.
    assert selector("auradefiNeverCalledThis(uint256,address)") == (
        keccak256(b"auradefiNeverCalledThis(uint256,address)")[:4]
    )


# pins: argument types join on a comma with NO space, which is the spelling
#       keccak is taken over.
def test_function_signature_joins_argument_types_with_no_spaces() -> None:
    assert function_signature("getPool", ("address", "address", "uint24")) == (
        "getPool(address,address,uint24)"
    )
    assert function_signature("tokenOfOwnerByIndex", ("address", "uint256")) == (
        "tokenOfOwnerByIndex(address,uint256)"
    )


# pins: a function with no arguments spells as bare parentheses, so decimals
#       is decimals() and never decimals( ) or decimals(void).
def test_function_signature_of_a_niladic_function_is_bare_parentheses() -> None:
    assert function_signature("decimals", ()) == "decimals()"
    assert function_signature("getReserves", []) == "getReserves()"


# pins: the two seams compose, so a signature built here selects to the same
#       four bytes reader.py will put on the wire.
def test_a_signature_built_here_selects_to_the_published_bytes() -> None:
    assert selector(
        function_signature("getPool", ("address", "address", "uint24"))
    ).hex() == "1698ee82"
    assert selector(function_signature("decimals", ())).hex() == "313ce567"
    assert selector(
        function_signature("aggregate3", ("(address,bool,bytes)[]",))
    ).hex() == "82ad56cb"


# --------------------------------------------------------------------
# static encoding
# --------------------------------------------------------------------


# pins: an address encodes right-aligned behind 12 zero bytes and lowercased,
#       so a checksummed input and its lowercase form give the same word.
def test_an_address_encodes_to_a_right_aligned_lowercase_word() -> None:
    assert encode(("address",), (VITALIK,)).hex() == (
        "000000000000000000000000d8da6bf26964af9d7eed9e03e53415d37aa96045"
    )
    assert encode(("address",), (VITALIK,)) == encode(("address",), (VITALIK.lower(),))


# pins: a three-argument static call encodes head-only with no offset word,
#       so the getPool calldata is the selector followed by exactly 3 words.
def test_the_getpool_calldata_vector() -> None:
    calldata = selector("getPool(address,address,uint24)") + encode(
        ("address", "address", "uint24"), (USDC, WETH, 3000)
    )
    assert calldata == bytes.fromhex(GETPOOL_CALLDATA)
    assert len(calldata) == 4 + 96


# pins: a uint256 argument encodes as its big-endian word, so positions(912345)
#       is the selector followed by 0x0debd9 right-aligned.
def test_the_positions_calldata_vector() -> None:
    calldata = selector("positions(uint256)") + encode(("uint256",), (912345,))
    assert calldata == bytes.fromhex(POSITIONS_CALLDATA)


# pins: int<N> is two's complement SIGN-EXTENDED across all 256 bits, so an
#       int24 of -887272 fills the high bytes with ff and is not zero-padded.
def test_a_negative_int24_sign_extends_across_the_whole_word() -> None:
    assert encode(("int24",), (-887272,)).hex() == (
        "fffffffffffffffffffffffffffffffffffffffffffffffffffffffffff27618"
    )
    assert encode(("int24",), (-1,)).hex() == "ff" * 32


# pins: a positive int24 encodes as the plain big-endian word, so the sign
#       extension above is applied to negatives alone.
def test_a_positive_int24_encodes_as_a_plain_word() -> None:
    encoded = encode(("int24",), (194470,)).hex()
    assert encoded.endswith("02f7a6")
    assert encoded == "0" * 58 + "02f7a6"


# pins: every int24 tick round trips through decode with its sign intact, so
#       an implementation that drops the sign bit on the way back fails here.
@pytest.mark.parametrize("tick", [-887272, -1, 0, 194470, 887272])
def test_an_int24_round_trips_through_decode(tick: int) -> None:
    assert decode(("int24",), encode(("int24",), (tick,))) == (tick,)


# pins: a bool encodes to a word of exactly 1 or 0, so True is never 0xff and
#       False is never absent.
def test_a_bool_encodes_to_a_one_or_zero_word() -> None:
    assert encode(("bool",), (True,)) == word(1)
    assert encode(("bool",), (False,)) == word(0)


# pins: a niladic call encodes to empty calldata, so decimals() is the four
#       selector bytes with nothing appended.
def test_encoding_no_types_gives_no_bytes() -> None:
    assert encode((), ()) == b""
    assert selector("decimals()") + encode((), ()) == bytes.fromhex("313ce567")


# pins: every width that is a multiple of 8 from 8 to 256 is supported for
#       uint, and its maximum encodes and decodes exactly.
@pytest.mark.parametrize("bits", WIDTHS)
def test_every_uint_width_carries_its_maximum(bits: int) -> None:
    largest = (1 << bits) - 1
    encoded = encode((f"uint{bits}",), (largest,))
    assert len(encoded) == 32
    assert encoded == word(largest)
    assert decode((f"uint{bits}",), encoded) == (largest,)


# pins: every width that is a multiple of 8 from 8 to 256 is supported for
#       int, and both ends of its two's complement range survive a round trip.
@pytest.mark.parametrize("bits", WIDTHS)
def test_every_int_width_carries_both_ends_of_its_range(bits: int) -> None:
    lowest = -(1 << (bits - 1))
    highest = (1 << (bits - 1)) - 1
    assert decode((f"int{bits}",), encode((f"int{bits}",), (lowest,))) == (lowest,)
    assert decode((f"int{bits}",), encode((f"int{bits}",), (highest,))) == (highest,)
    assert encode((f"int{bits}",), (lowest,)) == word(lowest)


# pins: several values of mixed types encode in order into one word each, so
#       the head is packed in argument order with no offsets between.
def test_a_mixed_argument_list_encodes_one_word_per_argument_in_order() -> None:
    encoded = encode(
        ("address", "uint256", "bool", "int24"), (USDC, 6, True, -1)
    )
    assert len(encoded) == 128
    assert encoded[0:32] == address_word(USDC)
    assert encoded[32:64] == word(6)
    assert encoded[64:96] == word(1)
    assert encoded[96:128] == b"\xff" * 32


# --------------------------------------------------------------------
# static decoding
# --------------------------------------------------------------------


# pins: the getReserves word group decodes to the pinned reserves, so a
#       uint112 pair packed beside a uint32 is read at full precision.
def test_the_getreserves_word_group_decodes_to_the_pinned_reserves() -> None:
    assert decode(
        ("uint112", "uint112", "uint32"), bytes.fromhex(GETRESERVES_WORDS)
    ) == GETRESERVES_VALUES


# pins: an address word decodes to '0x' and 40 LOWERCASE hex, which is what
#       keeps the Uniswap V3 pinned group id stable when a pool comes back.
def test_an_address_word_decodes_to_lowercase_hex() -> None:
    (decoded,) = decode(("address",), address_word(USDC))
    assert decoded == USDC
    assert decoded == decoded.lower()
    assert len(decoded) == 42
    (checksummed,) = decode(("address",), address_word(VITALIK.lower()))
    assert checksummed == VITALIK.lower()
    assert checksummed != VITALIK


# pins: decode ALWAYS returns a tuple, even for one type, because the
#       length-1 unwrap belongs to reader.py and two unwraps would lose data.
def test_decode_always_returns_a_tuple_even_for_one_type() -> None:
    result = decode(("uint256",), bytes.fromhex("00" * 31 + "06"))
    assert type(result) is tuple
    assert result == (6,)
    assert result != 6
    assert len(result) == 1


# pins: decoding no types over no data gives the empty tuple, so a niladic
#       call with no return value does not error.
def test_decoding_no_types_gives_an_empty_tuple() -> None:
    assert decode((), b"") == ()


# pins: a bool word decodes to the Python singleton, so a caller can use `is`
#       and never meets 1 where True was promised.
def test_a_bool_word_decodes_to_the_python_singleton() -> None:
    (yes,) = decode(("bool",), word(1))
    (no,) = decode(("bool",), word(0))
    assert yes is True
    assert no is False


# --------------------------------------------------------------------
# refusals: unsupported type names
# --------------------------------------------------------------------


# pins: encode refuses every type outside uint<N>, int<N>, address and bool,
#       so a dynamic type is never silently written as one word.
@pytest.mark.parametrize("type_name", UNSUPPORTED_TYPES)
def test_encode_refuses_an_unsupported_type(type_name: str) -> None:
    # 0 is a valid value for every supported integer type, and the two
    # wrong-case names get a value their lowercase form would accept, so
    # the only thing that can raise here is the type name itself.
    value = VALID_VALUE_FOR_WRONG_CASE.get(type_name, 0)
    with pytest.raises(ValidationError):
        encode((type_name,), (value,))


# pins: decode refuses every type outside uint<N>, int<N>, address and bool,
#       so an unsupported spelling never reads a word as a plausible number.
@pytest.mark.parametrize("type_name", UNSUPPORTED_TYPES)
def test_decode_refuses_an_unsupported_type(type_name: str) -> None:
    # A zero word decodes cleanly under every supported type, so the type
    # name is again the only possible cause.
    with pytest.raises(ValidationError):
        decode((type_name,), bytes(32))


# pins: a width that is not a multiple of 8, or is outside 8..256, is refused
#       for both signed and unsigned, so uint7 and int264 never encode.
@pytest.mark.parametrize("bits", [0, 1, 4, 7, 9, 12, 100, 255, 257, 264, 512])
def test_a_bad_integer_width_is_refused(bits: int) -> None:
    with pytest.raises(ValidationError):
        encode((f"uint{bits}",), (0,))
    with pytest.raises(ValidationError):
        encode((f"int{bits}",), (0,))


# pins: a width written with a leading zero is REFUSED rather than normalised
#       to its canonical spelling, so uint08 never encodes as uint8.
@pytest.mark.parametrize(("padded", "canonical"), LEADING_ZERO_WIDTHS)
def test_a_leading_zero_width_is_refused_and_never_normalised(
    padded: str, canonical: str
) -> None:
    # Why refusing beats normalising: the two spellings share a word, so the
    # word raises no alarm, while the signature hashes somewhere else
    # entirely. Normalising would send well-formed arguments to a selector
    # the contract does not carry.
    assert selector(f"allPairs({padded})") != selector(f"allPairs({canonical})")
    # The canonical spelling beside it accepts the same value happily, so the
    # leading zero is the only thing under test below.
    assert encode((canonical,), (5,)) == word(5)
    with pytest.raises(ValidationError):
        encode((padded,), (5,))
    with pytest.raises(ValidationError):
        decode((padded,), word(5))


# --------------------------------------------------------------------
# refusals: encoding
# --------------------------------------------------------------------


# pins: unequal types and values are refused, so an argument dropped at the
#       call site cannot shift every later word one place left.
def test_encode_refuses_a_types_and_values_length_mismatch() -> None:
    with pytest.raises(ValidationError):
        encode(("uint256", "uint256"), (1,))
    with pytest.raises(ValidationError):
        encode(("uint256",), (1, 2))
    with pytest.raises(ValidationError):
        encode((), (1,))


# pins: a uint value at or above 2**N is refused, so a uint8 of 256 does not
#       wrap to zero.
def test_encode_refuses_an_unsigned_value_that_is_too_large() -> None:
    with pytest.raises(ValidationError):
        encode(("uint8",), (256,))
    with pytest.raises(ValidationError):
        encode(("uint112",), (1 << 112,))
    with pytest.raises(ValidationError):
        encode(("uint256",), (1 << 256,))


# pins: a negative value is refused for an unsigned type, so -1 does not
#       encode as the maximum uint256.
def test_encode_refuses_a_negative_unsigned_value() -> None:
    with pytest.raises(ValidationError):
        encode(("uint8",), (-1,))
    with pytest.raises(ValidationError):
        encode(("uint256",), (-1,))


# pins: a signed value outside [-2**(N-1), 2**(N-1)-1] is refused at both
#       ends, so int8 accepts -128 and 127 and refuses 128 and -129.
def test_encode_refuses_a_signed_value_outside_its_range() -> None:
    assert encode(("int8",), (127,)) == word(127)
    assert encode(("int8",), (-128,)) == word(-128)
    with pytest.raises(ValidationError):
        encode(("int8",), (128,))
    with pytest.raises(ValidationError):
        encode(("int8",), (-129,))


# pins: bool is an int subclass, so True is refused for an integer type
#       instead of encoding as 1.
def test_encode_refuses_a_bool_for_an_integer_type() -> None:
    with pytest.raises(ValidationError):
        encode(("uint256",), (True,))
    with pytest.raises(ValidationError):
        encode(("uint8",), (False,))
    with pytest.raises(ValidationError):
        encode(("int24",), (True,))


# pins: 1 is refused for a bool, so the int/bool confusion is caught in both
#       directions and not only the one that looks wrong.
def test_encode_refuses_an_int_for_a_bool() -> None:
    with pytest.raises(ValidationError):
        encode(("bool",), (1,))
    with pytest.raises(ValidationError):
        encode(("bool",), (0,))


# pins: a string is refused for a bool, so 'true' off a config file does not
#       encode as truthiness.
def test_encode_refuses_a_string_for_a_bool() -> None:
    with pytest.raises(ValidationError):
        encode(("bool",), ("true",))
    with pytest.raises(ValidationError):
        encode(("bool",), ("",))


# pins: an address must match ^0x[0-9a-fA-F]{40}$, so a short one, an
#       unprefixed one and a non-hex one are all refused.
@pytest.mark.parametrize(
    "bad",
    [
        "0xABC",
        "d8da6bf26964af9d7eed9e03e53415d37aa96045",
        "0x" + "g" * 40,
        "0x" + "a" * 39,
        "0x" + "a" * 41,
        "",
        "0x",
    ],
)
def test_encode_refuses_a_malformed_address(bad: str) -> None:
    with pytest.raises(ValidationError):
        encode(("address",), (bad,))


# pins: a str amount is refused for an integer type, so nothing in this codec
#       ever calls int() over a string that arrived as an amount.
def test_encode_refuses_a_string_amount() -> None:
    with pytest.raises(ValidationError):
        encode(("uint256",), ("6",))
    with pytest.raises(ValidationError):
        encode(("uint256",), ("0x6",))


# pins: an address value must be a str, so the 20 raw bytes are refused and a
#       caller cannot bypass the format check by pre-encoding.
def test_encode_refuses_a_non_string_address() -> None:
    with pytest.raises(ValidationError):
        encode(("address",), (bytes.fromhex(USDC[2:]),))
    with pytest.raises(ValidationError):
        encode(("address",), (0,))


# --------------------------------------------------------------------
# refusals: decoding
# --------------------------------------------------------------------


# pins: len(data) must be exactly 32 * len(types), so a truncated word and a
#       trailing extra word are both refused.
def test_decode_refuses_data_that_is_not_thirty_two_bytes_per_type() -> None:
    with pytest.raises(ValidationError):
        decode(("uint256",), bytes(31))
    with pytest.raises(ValidationError):
        decode(("uint256",), bytes(64))
    with pytest.raises(ValidationError):
        decode(("uint256",), b"")
    with pytest.raises(ValidationError):
        decode(("uint112", "uint112", "uint32"), bytes(64))


# pins: a bool word other than 0 or 1 is refused, so 2 does not read as True.
def test_decode_refuses_a_bool_word_that_is_not_zero_or_one() -> None:
    with pytest.raises(ValidationError):
        decode(("bool",), word(2))
    with pytest.raises(ValidationError):
        decode(("bool",), word(-1))


# pins: an address word with a nonzero byte in its top 12 is refused, so
#       dirty high bytes are never truncated away into a valid-looking address.
def test_decode_refuses_an_address_word_with_dirty_high_bytes() -> None:
    dirty = bytes.fromhex("00" * 11 + "01" + USDC[2:])
    assert len(dirty) == 32
    with pytest.raises(ValidationError):
        decode(("address",), dirty)
    with pytest.raises(ValidationError):
        decode(("address",), bytes.fromhex("ff" + "00" * 11 + USDC[2:]))


# pins: a uint word wider than its declared N is refused, so a uint112 word
#       with bit 112 set is malformed and is not masked down to 112 bits.
def test_decode_refuses_an_over_wide_unsigned_word() -> None:
    with pytest.raises(ValidationError):
        decode(("uint112",), word(1 << 112))
    with pytest.raises(ValidationError):
        decode(("uint8",), word(256))
    with pytest.raises(ValidationError):
        decode(("uint32",), word(1 << 32))


# pins: the largest value each width permits still decodes, so the over-wide
#       refusal above is on the right side of the boundary.
def test_the_largest_value_each_width_permits_still_decodes() -> None:
    assert decode(("uint112",), word((1 << 112) - 1)) == ((1 << 112) - 1,)
    assert decode(("uint8",), word(255)) == (255,)
    assert decode(("uint256",), word((1 << 256) - 1)) == ((1 << 256) - 1,)


# pins: a signed word outside the declared range is refused at both ends, so
#       an int24 word carrying 2**23 is malformed and not read as negative.
def test_decode_refuses_a_signed_word_outside_its_range() -> None:
    with pytest.raises(ValidationError):
        decode(("int24",), word(1 << 23))
    with pytest.raises(ValidationError):
        decode(("int24",), word(-(1 << 23) - 1))
    assert decode(("int24",), word((1 << 23) - 1)) == ((1 << 23) - 1,)
    assert decode(("int24",), word(-(1 << 23))) == (-(1 << 23),)


# module purity
# --------------------------------------------------------------------


# pins: the module imports only the stdlib, auradefi.errors and this
#       package's keccak, so the no-new-dependency rule holds for the codec.
def test_the_module_imports_only_the_stdlib_errors_and_keccak() -> None:
    """Walks the whole AST, so a function-local ``import eth_abi`` fails."""
    tree = ast.parse(Path(abi.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    allowed = {"auradefi.errors", "auradefi.sources.evm.codec.keccak"}
    foreign = {
        name
        for name in imported
        if name not in allowed and name.split(".")[0] not in sys.stdlib_module_names
    }
    assert not foreign, f"forbidden imports: {sorted(foreign)}"
    assert "auradefi.sources.evm.codec.keccak" in imported, (
        "the selector must come from this package's keccak"
    )


# pins: no float and no eval appear anywhere in the module, so a 32-byte word
#       is never routed through a type that cannot hold 2**256 - 1.
def test_the_module_contains_no_float_and_no_eval() -> None:
    tree = ast.parse(Path(abi.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and type(node.value) is float:
            offenders.append(f"float literal on line {node.lineno}")
        if isinstance(node, ast.Name) and node.id in {"float", "eval", "exec"}:
            offenders.append(f"{node.id} on line {node.lineno}")
    assert not offenders, "\n".join(offenders)


# pins: the public surface is exactly the four documented functions, so
#       callers cannot start depending on a helper as if it were interface.
def test_the_public_surface_is_the_four_documented_functions() -> None:
    assert abi.__all__ == [
        "decode",
        "encode",
        "function_signature",
        "selector",
    ]
    public = sorted(
        name
        for name, value in vars(abi).items()
        if not name.startswith("_")
        and callable(value)
        and getattr(value, "__module__", None) == abi.__name__
    )
    assert public == sorted(abi.__all__)
