"""Multicall3 ``aggregate3`` calldata: the one dynamic shape this codec has.

Split out of :mod:`~auradefi.sources.evm.codec.abi` at that module's line
cap, and the seam is the honest one rather than the convenient one. `abi`
encodes STATIC words and says so in capitals: not one dynamic type, no
``string``, no ``bytes``, no arrays. ``aggregate3`` takes
``(address,bool,bytes)[]``, which is all three at once. It is here because
Multicall3 is the only reason the codec needs dynamic encoding at all, and
keeping it beside the static words made that module read as though dynamic
support were general. It is not, and nothing else may use these helpers to
pretend otherwise.

Everything here builds on `abi`'s word primitives, so the offsets, the
range checks and the ``ValidationError`` taxonomy are that module's, not a
second copy. The selector is derived at import through this package's own
keccak, so ``82ad56cb`` cannot drift from a caller's spelling of the
signature.
"""

from __future__ import annotations

from collections.abc import Sequence

from auradefi.errors import ValidationError, require_sequence
from auradefi.sources.evm.codec.abi import (
    _WORD,
    _address_word,
    _bool_word,
    _pad32,
    _payload,
    _word_of,
    selector,
)


#: The canonical Multicall3 signature. Its selector is derived through this
#: package's keccak, so it cannot drift from a caller's own spelling.
_AGGREGATE3 = "aggregate3((address,bool,bytes)[])"

#: The offset an element carries to its bytes: a call element has three head
#: words (target, allowFailure, the offset itself) and a result has two.
_CALL_BYTES_OFFSET = 0x60
_RESULT_BYTES_OFFSET = 0x40

#: Derived at import through :func:`selector`, so ``82ad56cb`` is this
#: package's keccak of the signature above and not a copied literal.
_AGGREGATE3_SELECTOR = selector(_AGGREGATE3)


def _dynamic_array(elements: Sequence[bytes]) -> bytes:
    """The array length, one offset per element, then the elements.

    Each offset is measured from just after the length word, as the
    running total ahead of it, so unequal elements share no stride.
    """
    offsets = []
    running = _WORD * len(elements)
    for element in elements:
        offsets.append(_word_of(running))
        running += len(element)
    return _word_of(len(elements)) + b"".join(offsets) + b"".join(elements)


def _call_element(call: object) -> bytes:
    """One CHECKED ``(target, allow_failure, data)`` triple, as its words.

    Every field is read out and checked, never unpacked hopefully. The
    ValueError a three-way unpack raises on a pair, a quadruple or a bare
    string escapes the boundary ``multicall.py`` is told to translate.
    """
    if isinstance(call, (str, bytes)) or not isinstance(call, Sequence):
        raise ValidationError(f"aggregate3 call must be a triple: {call!r}")
    if len(call) != 3:
        raise ValidationError(f"aggregate3 call takes 3 fields, got {len(call)}")
    data = _payload(call[2], "aggregate3 call data")
    head = _address_word(call[0]) + _bool_word(call[1], "allowFailure")
    return head + _word_of(_CALL_BYTES_OFFSET) + _word_of(len(data)) + _pad32(data)


def encode_aggregate3(calls: Sequence[tuple[str, bool, bytes]]) -> bytes:
    """Multicall3 ``aggregate3`` calldata, INCLUDING the selector.

    The first named dynamic special case, for ``(address,bool,bytes)[]``.
    Returns complete calldata beginning with ``82ad56cb``, so a caller
    must not prepend a selector of its own.

    ``calls`` are ``(target, allow_failure, data)`` triples. The pinned
    layout is the selector, then the head word ``0x20`` (the offset to the
    single dynamic argument), then the array length, then one offset word
    per element measured from just after the length word, then each
    element as the address word, the allowFailure word, the constant
    offset ``0x60`` to its bytes, the bytes length, and the bytes
    right-padded to a multiple of 32.

    Raises:
        ValidationError: on a call that is not a three-field sequence, on
            data that is not bytes, on a malformed target address, and on
            an allow_failure flag that is not ``True`` or ``False``: the
            flag occupies a bool word, so the bool rule holds here too.
    """
    require_sequence(calls, "calls", ValidationError)
    elements = [_call_element(call) for call in calls]
    return _AGGREGATE3_SELECTOR + _word_of(0x20) + _dynamic_array(elements)


def _aggregate3_result(data: bytes, start: int) -> tuple[bool, bytes]:
    """One returned ``(bool,bytes)`` element read at ``start``.

    Success word, the inner offset ``0x40`` to the returndata, the
    declared length, then the bytes. Every offset and length is checked
    against what is there, so a crafted response cannot read past it.
    """
    head = start + _RESULT_BYTES_OFFSET + _WORD
    if head > len(data):
        raise ValidationError(
            f"aggregate3 element at {start} runs past {len(data)} bytes"
        )
    success = int.from_bytes(data[start : start + _WORD], "big")
    if success > 1:
        raise ValidationError(f"aggregate3 success word must be 0 or 1: {success}")
    inner = int.from_bytes(data[start + _WORD : start + 2 * _WORD], "big")
    if inner != _RESULT_BYTES_OFFSET:
        raise ValidationError(
            f"aggregate3 element at {start} offsets its bytes to {inner:#x}"
        )
    length = int.from_bytes(data[start + _RESULT_BYTES_OFFSET : head], "big")
    if head + length > len(data):
        raise ValidationError(
            f"aggregate3 returndata of {length} bytes runs past {len(data)}"
        )
    return (success == 1, data[head : head + length])


def decode_aggregate3(data: bytes) -> tuple[tuple[bool, bytes], ...]:
    """Multicall3 ``aggregate3`` return data as (success, returndata).

    The second named dynamic special case, for ``(bool,bytes)[]``. §4's
    acceptance criterion, a reverting call in a batch of five yielding
    four results and one declared failure, needs the return path too.

    The layout mirrors :func:`encode_aggregate3` with the element's inner
    offset at ``0x40`` instead of ``0x60``. Results are plain pairs: a
    failed call is ``(False, b'')``, an empty returndata that is DECLARED
    and never a zero. ``multicall.py`` owns the richer type, not this one.

    Raises:
        ValidationError: on ``data`` that is not bytes or is shorter than
            two words, a first word that is not ``0x20``, a success word
            that is not 0 or 1, or any offset or length that does not fit.
    """
    data = _payload(data, "aggregate3 return")
    if len(data) < 2 * _WORD:
        raise ValidationError(
            f"aggregate3 return needs a head and a length word, got {len(data)} bytes"
        )
    head = int.from_bytes(data[:_WORD], "big")
    if head != 0x20:
        raise ValidationError(f"aggregate3 return head word must be 0x20: {head:#x}")
    count = int.from_bytes(data[_WORD : 2 * _WORD], "big")
    if 2 * _WORD + _WORD * count > len(data):
        raise ValidationError(
            f"aggregate3 declares {count} results, which do not fit {len(data)} bytes"
        )
    results = []
    for index in range(count):
        at = 2 * _WORD + _WORD * index
        offset = int.from_bytes(data[at : at + _WORD], "big")
        results.append(_aggregate3_result(data, 2 * _WORD + offset))
    return tuple(results)
