"""``eth_getLogs`` rows: the typed record and its strict parser.

The parsing authority for :mod:`~auradefi.sources.evm.logs`, split out at
that module's line cap on the seam ``txfetch.py`` and ``txlist.py``
already use in this package: one module issues the requests and owns the
filter, one module owns the row grammar, and there is never a second
place that parses.

Everything here is STRICT. A log is evidence a decoder will attribute
value from, so a row missing a field, carrying a non-hex quantity, or
spelling a topic in 64 characters instead of 66 raises ``SourceError``
rather than degrading. That is the opposite of ``etherscan.py``'s
additive spam-skip on discovery rows, and deliberately so: a skipped
spam token costs a row nobody wanted, a skipped log costs a transaction
its meaning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from auradefi.errors import SourceError

#: A 20-byte EVM address: "0x" and exactly 40 hex digits.
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")

#: A 32-byte log topic: "0x" and exactly 64 hex digits, 66 characters in all.
_TOPIC = re.compile(r"0x[0-9a-fA-F]{64}")

#: A JSON-RPC QUANTITY: the "0x" prefix and at least one hex digit, so "0x"
#: alone is malformed. Mirrors rpc.py's _QUANTITY, which is private to it.
_QUANTITY = re.compile(r"0x[0-9a-fA-F]+")

#: A JSON-RPC DATA payload: "0x" and whole bytes, so an odd digit count is
#: malformed and "0x" alone is the empty payload.
_PAYLOAD = re.compile(r"0x(?:[0-9a-fA-F]{2})*")


@dataclass(frozen=True, slots=True)
class LogRecord:
    """One typed ``eth_getLogs`` row.

    ``address`` and ``transaction_hash`` are lowercase, as everywhere else
    in the EVM source, so two spellings of one contract never compare
    unequal. ``topics`` is a tuple of lowercase ``0x``-prefixed 32-byte
    hex strings, and ``data`` is the decoded payload as ``bytes``, empty
    for ``"0x"``. Tuple topics and bytes data make the whole record
    hashable, which frozen alone would not.

    ``block_number`` and ``log_index`` arrive as hex QUANTITY strings and
    parse with ``int(x, 16)``, never through float (rules #1/#2).
    ``removed`` is the node's reorg flag and reads False when the key is
    absent.

    There is deliberately NO timestamp field: ``eth_getLogs`` returns no
    time, and rule #3 leaves no room for a half-populated one.
    """

    address: str
    topics: tuple[str, ...]
    data: bytes
    block_number: int
    transaction_hash: str
    log_index: int
    removed: bool



def _text(row: dict, key: str) -> str:
    """A row's ``key`` as a lowercased string.

    Raises:
        SourceError: when the key is absent or is not a string. A bare
            ``.lower()`` on the miss leaks ``AttributeError`` past the
            SourceError promise, and half a taxonomy is worse than none.
    """
    value = row.get(key)
    if not isinstance(value, str):
        raise SourceError(f"eth_getLogs row carries no usable {key}: {value!r}")
    return value.lower()


def _int(row: dict, key: str) -> int:
    """A row's ``key`` as an ``int``, parsed from its hex QUANTITY string.

    Raises:
        SourceError: when the key is absent, is a JSON integer rather than
            the string the encoding mandates (rule #2), or is a string
            that is not ``0x`` and at least one hex digit.
    """
    value = row.get(key)
    if not isinstance(value, str) or _QUANTITY.fullmatch(value) is None:
        raise SourceError(f"malformed eth_getLogs {key}: {value!r}")
    return int(value, 16)


def _topics(row: dict) -> tuple[str, ...]:
    """A row's topics as a tuple of lowercase 32-byte hex strings.

    An empty list is an empty tuple: an anonymous event carries no topic0
    and is a real row.

    Raises:
        SourceError: when ``topics`` is absent or is not a list, and on
            any topic that is not a 66-character ``0x`` hex string.
    """
    value = row.get("topics")
    if not isinstance(value, list):
        raise SourceError(f"eth_getLogs row topics must be a list: {value!r}")
    for topic in value:
        if not isinstance(topic, str) or _TOPIC.fullmatch(topic) is None:
            raise SourceError(f"malformed eth_getLogs topic: {topic!r}")
    return tuple(topic.lower() for topic in value)


def _data(row: dict) -> bytes:
    """A row's ``data`` payload as bytes, empty for ``"0x"``.

    Raises:
        SourceError: when ``data`` is absent, is not a string, or is not
            ``0x`` followed by whole hex bytes. An absent payload is
            DECLARED here and never read as the empty word, which is the
            coercion rule #8 forbids: a row with no data and a row
            carrying 32 zero bytes say different things.
    """
    value = row.get("data")
    if not isinstance(value, str) or _PAYLOAD.fullmatch(value) is None:
        raise SourceError(f"malformed eth_getLogs data: {value!r}")
    return bytes.fromhex(value[2:])


def _removed(row: dict) -> bool:
    """A row's reorg flag, False when the node sends no ``removed`` key.

    Raises:
        SourceError: on a present value that is not a JSON boolean. The
            flag decides whether a consumer keeps the row at all, so a
            string "false" read through truthiness would drop a live log.
    """
    value = row.get("removed", False)
    if not isinstance(value, bool):
        raise SourceError(f"eth_getLogs removed must be a boolean: {value!r}")
    return value


def _record(row: object) -> LogRecord:
    """One node row as a :class:`LogRecord`.

    Raises:
        SourceError: on a row that is not an object and on any member the
            helpers above refuse.
    """
    if not isinstance(row, dict):
        raise SourceError(
            f"eth_getLogs row must be an object, got {type(row).__name__}"
        )
    return LogRecord(
        address=_text(row, "address"),
        topics=_topics(row),
        data=_data(row),
        block_number=_int(row, "blockNumber"),
        transaction_hash=_text(row, "transactionHash"),
        log_index=_int(row, "logIndex"),
        removed=_removed(row),
    )
