"""The JSON-RPC 2.0 envelope vocabulary, with no transport in it.

Split out of :mod:`~auradefi.sources.evm.rpc` at that module's line cap,
on the same seam ``txlist.py`` and ``txfetch.py`` use: the values and the
grammar live in one module, the requests in another. Nothing here opens a
socket or holds a client, so a caller can reason about a block tag or a
batch answer without a transport in scope, and the vectors are readable
on their own.

``block_tag`` is the shared one: ``logs.py``, ``multicall.py`` and
``reader.py`` all pin their reads with it, and a second spelling of a
block parameter would be two wire identities for one height.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from auradefi.errors import SourceError, ValidationError

# A JSON-RPC QUANTITY: the "0x" prefix and at least one hex digit. Bare
# int(x, 16) is too lenient for a wire value ("1bc1", " 0x10 ", "+0x1" and
# "0x1_0" all parse), and the encoding mandates the prefix, so the shape is
# checked before the parse. Mirrors etherscan.py's _DIGITS guard.
_QUANTITY = re.compile(r"0x[0-9a-fA-F]+")


def block_tag(block_number: int | None) -> str:
    """A JSON-RPC block parameter for ``block_number``.

    ``None`` is the string ``"latest"``. Anything else is minimal
    lowercase hex: ``20_450_000`` is ``"0x1380ad0"`` and ``0`` is
    ``"0x0"``, never zero-padded and never uppercase. Zero is a real
    block height, so it is ``"0x0"`` and not ``"latest"``.

    Consumed by ``reader.py``, ``multicall.py`` and ``logs.py``; the
    pinned value for the 0.2.0 golden block is ``"0x1380ad0"``.
    """
    # `is None`, never truthiness: block zero is a real height and its tag
    # is "0x0". hex() is minimal and lowercase by construction.
    if block_number is None:
        return "latest"
    return hex(block_number)


def _is_pair_like(value: object) -> bool:
    """True for a sequence that could be a ``(method, params)`` pair.

    ``str`` and ``bytes`` are sequences to Python and never a pair here,
    so both are excluded: ``batch("ab")`` is a mistake, not two requests.
    """
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _error_text(error: object) -> str:
    """A node ``error`` member as one line carrying its code and message.

    Both halves survive to the caller, as ``SolanaRpc._call`` does. A node
    that sends a non-object still gets its payload reported, as a message.
    """
    code = error.get("code") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    return f"code={code!r} message={message!r}"


def _quantity(value: object, method: str) -> int:
    """A JSON-RPC QUANTITY hex string as an ``int``, via ``int(x, 16)``.

    A raw on-chain amount is a JSON string (SPEC rule #2), so a JSON
    integer is a malformed envelope and not a lenient success, and the
    parse never touches float (rule #1).

    Raises:
        SourceError: on a value that is not ``0x``-prefixed hex.
    """
    if not isinstance(value, str) or _QUANTITY.fullmatch(value) is None:
        raise SourceError(f"malformed {method} quantity: {value!r}")
    return int(value, 16)


@dataclass(frozen=True, slots=True)
class BatchResult:
    """One item of a :meth:`EvmRpc.batch` response, in request order.

    EXACTLY one member is set. A batch item the node answered carries
    ``result`` with ``error`` at ``None``; an item the node refused
    carries ``error``, a human-readable string embedding the JSON-RPC
    ``code`` and ``message``, with ``result`` at ``None``. Rule #8: a
    failed item is DECLARED, never coerced to zero, and this is the only
    failure channel phase 11 has, since ``positions/`` carries no
    ``data_quality`` field and ``sources/`` may not import ``decode``.
    ``multicall.py`` mirrors the shape as ``CallResult`` for ``aggregate3``.
    """

    result: object | None
    error: str | None

    def __post_init__(self) -> None:
        """Refuse a carrier with both members set or neither set."""
        # Membership is `is not None`, so a falsy but real result ("" or 0
        # or []) is a set member. Truthiness here would read an empty word
        # as an unanswered item, which is the coercion rule #8 forbids.
        if (self.result is None) == (self.error is None):
            raise ValidationError(
                "a BatchResult sets exactly one of result/error, got "
                f"result={self.result!r} error={self.error!r}"
            )


def _batch_result(item: dict) -> BatchResult:
    """One matched batch item as a carrier: a value or a declared failure.

    An ``error`` member is a DECLARED failure and never a raise: one
    reverting call must not void the batch. An item carrying neither a
    usable ``result`` nor an ``error`` is declared the same way, since
    voiding four good answers over one empty envelope is the coercion
    rule #8 exists to stop. A JSON ``null`` result is that case, and it
    is declared rather than silently read as an answer.
    """
    error = item.get("error")
    if error is not None:
        return BatchResult(None, _error_text(error))
    result = item.get("result")
    if result is None:
        return BatchResult(None, f"no result in batch item {item.get('id')!r}")
    return BatchResult(result, None)
