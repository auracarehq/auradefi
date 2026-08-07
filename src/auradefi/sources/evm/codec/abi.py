"""Static-type ABI codec and selector derivation (RELEASE_0.2.0 §4).

NOT ONE DYNAMIC TYPE. No ``string``, no ``bytes``, no arrays, no nested
tuples. Every value is a single 32-byte word or a fixed sequence of them,
so this module implements exactly ``uint<N>`` and ``int<N>`` for N a
multiple of 8 in 8..256, ``address`` and ``bool``, and raises on anything
else instead of guessing. That refusal is the interesting behaviour: a
codec that silently mis-encodes a type it does not support is the shape
of defect this project cuts releases over.

PINNED WORD FORMAT, which the golden vectors in the mirrored test file
were derived from:

* Every word is 32 bytes, big-endian, head only, with no offsets.
* ``uint<N>``: the big-endian word. Negative or ``>= 2**N`` is refused.
* ``int<N>``: two's complement sign-extended across all 256 bits, so
  ``int24`` of -887272 is
  ``fffffffffffffffffffffffffffffffffffffffffffffffffffffffffff27618``.
  Range checked on ``[-2**(N-1), 2**(N-1)-1]``.
* ``address``: a ``str`` matching ``^0x[0-9a-fA-F]{40}$`` in, 12 zero
  bytes then the 20 address bytes out. Decoding emits ``'0x'`` and 40
  LOWERCASE hex, and refuses a word with any nonzero byte in its high
  12. The lowercase decode is what keeps the Uniswap V3 pinned group id
  ``grp_9b813f4a0ae43e5b`` intact when a pool address returns through
  ``getPool``.
* ``bool``: True or False only, encoding to a word of 1 or 0, and
  decoding refuses any other word. ``bool`` is an ``int`` subclass, so it
  is rejected for the integer types before the ``int`` check and ``1`` is
  rejected for ``bool``, following ``sources/solana/rpc.py``.

TWO NAMED SPECIAL CASES, and only two. Multicall3's own calldata IS
dynamic, a dynamic array of tuples containing ``bytes``, and so is what
it returns. Rather than open the general codec to dynamic types, both
directions are hand-written here under their own names, each with its own
committed vector: :func:`encode_aggregate3` for ``(address,bool,bytes)[]``
and :func:`decode_aggregate3` for ``(bool,bytes)[]``. The general codec
stays static-only.

ERROR BOUNDARY. Every malformed input raises
:class:`~auradefi.errors.ValidationError`, a value of the wrong Python
type included, because at this layer the bytes are caller input.
``reader.py`` and ``multicall.py`` translate that to ``SourceError`` when
the bytes came off the wire, and neither can translate a ValueError from
an unguarded unpack or a TypeError from ``str`` where bytes were due.

Pure module: stdlib, :mod:`auradefi.errors` and :mod:`keccak` only. No
HTTP, no float anywhere, no module-level mutable state.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from auradefi.errors import ValidationError, require_sequence
from auradefi.sources.evm.codec.keccak import keccak256

__all__ = ["decode", "encode", "function_signature", "selector"]

#: One word, and one word as a mask. Reducing modulo the mask is what
#: sign-extends a negative across all 256 bits.
_WORD = 32
_MASK256 = (1 << 256) - 1

#: The address form. Anchored by fullmatch, so a trailing newline cannot
#: slip a 41st character past the width.
_ADDRESS = re.compile("0x[0-9a-fA-F]{40}")

#: Plain ASCII decimal, so a width written with a unicode digit, a sign or
#: an array suffix fails to parse instead of reaching :func:`int`.
_DECIMAL = frozenset("0123456789")


def _word_of(value: int) -> bytes:
    """One 32-byte big-endian word, two's complement for a negative."""
    return (value & _MASK256).to_bytes(_WORD, "big")


def _pad32(data: bytes) -> bytes:
    """``data`` right-padded with zero bytes to a multiple of 32."""
    return data + bytes(-len(data) % _WORD)


def _payload(value: object, member: str) -> bytes:
    """``value`` as bytes: a hex ``str`` is refused here, never decoded."""
    if not isinstance(value, (bytes, bytearray)):
        raise ValidationError(f"{member} takes bytes, got {type(value).__name__}")
    return bytes(value)


def _parse_type(name: object) -> tuple[str, int]:
    """``('uint', 112)``, ``('int', 24)``, ``('address', 0)`` or ``('bool', 0)``.

    Nothing else parses. A width is plain decimal, a multiple of 8 in
    8..256, so ``uint7``, ``uint264``, the bare aliases ``uint`` and
    ``int``, and ``uint256[]`` raise here instead of being encoded as one
    plausible word. A leading zero raises too: ``uint08`` encodes exactly
    as ``uint8`` while hashing to a different selector, which would send
    well-formed arguments to a function the contract does not have. Names
    are case-sensitive, so ``ADDRESS`` is not ``address``. The one
    :func:`int` over a ``str`` is over a checked width, never an amount.
    """
    if not isinstance(name, str):
        raise ValidationError(f"ABI type must be a string: {name!r}")
    if name in ("address", "bool"):
        return (name, 0)
    if name.startswith("uint"):
        kind, digits = "uint", name[4:]
    elif name.startswith("int"):
        kind, digits = "int", name[3:]
    else:
        raise ValidationError(f"unsupported ABI type: {name!r}")
    if not digits or not set(digits) <= _DECIMAL:
        raise ValidationError(f"unsupported ABI type: {name!r}")
    bits = int(digits)
    if digits != str(bits):
        raise ValidationError(f"integer width has a leading zero: {name!r}")
    if bits % 8 or not 8 <= bits <= 256:
        raise ValidationError(
            f"integer width must be a multiple of 8 in 8..256: {name!r}"
        )
    return (kind, bits)


def _integer_range(kind: str, bits: int) -> tuple[int, int]:
    """The inclusive range ``kind<bits>`` accepts, both ends."""
    if kind == "uint":
        return (0, (1 << bits) - 1)
    return (-(1 << (bits - 1)), (1 << (bits - 1)) - 1)


def _address_word(value: object) -> bytes:
    """Twelve zero bytes then the 20 address bytes.

    A ``str`` in the pinned form only: raw bytes are refused, so a caller
    cannot skip the format check by pre-encoding.
    """
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value):
        raise ValidationError(f"malformed address: {value!r}")
    return bytes(12) + bytes.fromhex(value[2:])


def _bool_word(value: object, member: str) -> bytes:
    """A word of 1 or 0, for ``True`` or ``False`` and nothing else.

    ``1`` is refused here as firmly as ``True`` is refused for an integer
    type, so both halves of the bool and int confusion are caught.
    """
    if value is not True and value is not False:
        raise ValidationError(f"{member} takes True or False, got {value!r}")
    return _word_of(1 if value else 0)


def _encode_word(type_name: str, value: object) -> bytes:
    """One value as its 32-byte word."""
    kind, bits = _parse_type(type_name)
    if kind == "address":
        return _address_word(value)
    if kind == "bool":
        return _bool_word(value, "bool")
    # bool is an int subclass, so it is rejected BEFORE the int check:
    # True is a malformed amount and never a 1 (sources/solana/rpc.py).
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{type_name} takes an int, got {value!r}")
    low, high = _integer_range(kind, bits)
    if not low <= value <= high:
        raise ValidationError(f"{value} is outside the range of {type_name}")
    return _word_of(value)


def _decode_word(type_name: str, chunk: bytes) -> object:
    """One 32-byte word as its value.

    A word carrying more than its type declares is refused, so a
    ``uint112`` reserve with dirt above bit 112 never reads as a balance.
    """
    kind, bits = _parse_type(type_name)
    raw = int.from_bytes(chunk, "big")
    if kind == "address":
        if raw >> 160:
            raise ValidationError(f"address word has dirty high bytes: {chunk.hex()}")
        return "0x" + chunk[12:].hex()
    if kind == "bool":
        if raw > 1:
            raise ValidationError(f"bool word must be 0 or 1: {chunk.hex()}")
        return raw == 1
    signed = kind == "int" and bool(raw >> 255)
    value = raw - (1 << 256) if signed else raw
    low, high = _integer_range(kind, bits)
    if not low <= value <= high:
        raise ValidationError(f"word does not fit {type_name}: {chunk.hex()}")
    return value


def selector(signature: str) -> bytes:
    """The four-byte function selector for a canonical ``signature``.

    Exactly ``keccak256(signature.encode())[:4]``, over the canonical
    spelling :func:`function_signature` produces: no spaces, no names.
    """
    return keccak256(signature.encode())[:4]


def function_signature(fn: str, arg_types: Sequence[str]) -> str:
    """The canonical signature ``fn(type,type)``, with no spaces.

    Exactly ``f"{fn}({','.join(arg_types)})"``, so an empty ``arg_types``
    gives ``'decimals()'``. Every selector in ``reader.py`` is built here,
    so the canonical spelling is fixed: ``uint256`` never ``uint``.
    """
    require_sequence(arg_types, "arg_types", ValidationError)
    return f"{fn}({','.join(arg_types)})"


def encode(types: Sequence[str], values: Sequence[object]) -> bytes:
    """Encode ``values`` as ``types``, one 32-byte word each.

    Head only, no offsets, no dynamic types. The result is
    ``32 * len(types)`` bytes and carries no selector: a caller prepends
    :func:`selector` itself.

    Raises:
        ValidationError: on unequal ``types`` and ``values`` lengths, an
            unsupported type name, a value of the wrong Python type
            (``True`` for an integer, ``1`` for ``bool``), or one that is
            outside the type's range.
    """
    # Measured with len() below, so the sequence itself is refused
    # first: len() over None or a generator is a TypeError, outside
    # the ValidationError this module promises, and a bare str would
    # be counted per character into a silently wrong word count.
    require_sequence(types, "types", ValidationError)
    require_sequence(values, "values", ValidationError)
    if len(types) != len(values):
        raise ValidationError(
            f"{len(types)} types and {len(values)} values: an argument "
            "dropped at the call site would shift every later word left"
        )
    return b"".join(_encode_word(name, v) for name, v in zip(types, values))


def decode(types: Sequence[str], data: bytes) -> tuple[object, ...]:
    """Decode ``data`` as ``types``, ALWAYS returning a tuple.

    A single type gives a length-1 tuple, never the bare value: the
    unwrap belongs to ``reader.py``, and two layers that both unwrap is a
    defect neither can see alone.

    ``len(data)`` must be exactly ``32 * len(types)`` and each word must
    fit its type: a ``uint112`` word with a bit above 112 is malformed.

    Raises:
        ValidationError: on ``data`` that is not bytes, an unsupported
            type name, a length that is not ``32 * len(types)``, or any
            word that does not fit.
    """
    data = _payload(data, "decode")
    require_sequence(types, "types", ValidationError)
    expected = _WORD * len(types)
    if len(data) != expected:
        raise ValidationError(
            f"{len(types)} types need exactly {expected} bytes, got {len(data)}"
        )
    return tuple(
        _decode_word(name, data[index * _WORD : index * _WORD + _WORD])
        for index, name in enumerate(types)
    )
