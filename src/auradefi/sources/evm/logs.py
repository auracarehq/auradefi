"""``eth_getLogs`` over a block range, chunked, as typed rows.

RELEASE_0.2.0 §4. Before this module the package could ask a node for a
balance and for the result of a call, and could not ask it what had
happened. Event scanning is that missing read, and the decode protocol
handlers in phases 13 and 14 are its callers.

The module owns two things ``rpc.py`` deliberately does not: the chunking
arithmetic, and the wire shape of the filter object. ``eth_get_logs``
forwards a filter dict unvalidated and hands back the rows exactly as
received, so every key a node sees is authored here.

CHUNKING, pinned. The range is INCLUSIVE at both ends. Chunk ``k`` covers

    [from_block + k*chunk_blocks,
     min(from_block + (k+1)*chunk_blocks - 1, to_block)]

so a full chunk holds exactly ``chunk_blocks`` blocks, the next chunk
starts on the block after the previous chunk's last, no block is asked
for twice, and no block is skipped. The request count is
``ceil((to_block - from_block + 1) / chunk_blocks)``, which is why a
range that is an exact multiple of the width sends no trailing empty
request. A later phase that re-derives this arithmetic must match it.

Rows accumulate in RECEIVED order across chunks, and a chunk that comes
back empty contributes nothing and does not end the scan. This is where
a range scan parts company with ``sources/solana/rpc.py::get_signatures``,
where a short page ends the walk: a signature walk discovers its end from
the page it just read, while a block range knows its end before the first
request, so an empty chunk in the middle is ordinary.

Every argument is checked before any HTTP, and a refusal there is
:class:`auradefi.errors.ValidationError`: the caller's own range, chunk
width, topics and address are wrong, and no node was involved. A row the
node sent that cannot be typed is :class:`auradefi.errors.SourceError`,
which is the same split ``etherscan.py`` draws.

All timestamps in this package are millisecond-epoch integers (rule #3)
and ``eth_getLogs`` returns none, so :class:`LogRecord` carries no
timestamp field. Inventing one from a second call belongs to whoever
needs it and not to this module.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from auradefi.errors import ValidationError, require_int
from auradefi.sources.evm.logrows import _ADDRESS, LogRecord, _record
from auradefi.sources.evm.rpc import EvmRpc, block_tag

#: Blocks per ``eth_getLogs`` request when a caller names no width. Public
#: node providers cap a log range, and 2,000 blocks is roughly eight hours
#: of Ethereum mainnet: wide enough that a day's scan is a handful of
#: requests, narrow enough to stay inside the common limits.
DEFAULT_CHUNK_BLOCKS = 2_000

# Every shape below is checked with fullmatch before anything parses it, the
# guard etherscan.py's _DIGITS and rpc.py's _QUANTITY already use: int(x, 16)
# is too lenient for a wire value ("1bc1", " 0x10 ", "+0x1" and "0x1_0" all
# parse), and the ValueError a lenient parse eventually raises would escape
# the SourceError promise.



def _check_scan(from_block: int, to_block: int, chunk_blocks: int) -> None:
    """Refuse a range or a chunk width this module cannot scan.

    Every comparison below is against an int literal, so each argument is
    type-checked first: a ``str`` compares as a TypeError and a ``float``
    clears all three and dies later at ``block_tag``'s ``hex()``, both of
    them outside the promise this function is named in.

    Raises:
        ValidationError: on an argument that is not an int, a negative
            ``from_block``, an inverted range, and a ``chunk_blocks`` of
            zero or below.
    """
    require_int(from_block, "from_block", ValidationError)
    require_int(to_block, "to_block", ValidationError)
    require_int(chunk_blocks, "chunk_blocks", ValidationError)
    if from_block < 0:
        raise ValidationError(f"scan_logs from_block is below zero: {from_block!r}")
    if from_block > to_block:
        raise ValidationError(
            "scan_logs scans an inclusive ordered range, got from_block "
            f"{from_block!r} above to_block {to_block!r}"
        )
    # A width of zero never advances and a negative one walks backwards out
    # of the range, so neither describes a scan that ends.
    if chunk_blocks <= 0:
        raise ValidationError(
            f"scan_logs chunk_blocks is not positive: {chunk_blocks!r}"
        )


def _chunks(
    from_block: int, to_block: int, chunk_blocks: int
) -> Iterator[tuple[int, int]]:
    """The inclusive ``[low, high]`` spans of the scan, in ascending order.

    The pinned arithmetic, in code once and here in words: chunk ``k`` is
    ``[from + k*chunk, min(from + (k+1)*chunk - 1, to)]`` and there are
    ``ceil((to - from + 1) / chunk)`` of them. The ``- 1`` is what makes a
    full chunk hold exactly ``chunk_blocks`` blocks and the next chunk
    start on the block after this one's last. Drop it and every boundary
    block is asked for twice, and a span that is an exact multiple of the
    width sends a trailing request over ground already scanned.
    """
    span = to_block - from_block + 1
    # An integer ceiling. span / chunk_blocks through float loses the last
    # chunk of a range wide enough to exceed float's exact integers, and
    # rule #1 keeps float out of arithmetic anything depends on.
    count = (span + chunk_blocks - 1) // chunk_blocks
    for index in range(count):
        low = from_block + index * chunk_blocks
        yield low, min(low + chunk_blocks - 1, to_block)


def _checked_address(value: object) -> str:
    """One lowercased filter address.

    Raises:
        ValidationError: on anything but a ``0x``-prefixed 40-hex string.
    """
    if not isinstance(value, str) or _ADDRESS.fullmatch(value) is None:
        raise ValidationError(
            f"scan_logs needs a 0x-prefixed 40-hex address: {value!r}"
        )
    return value.lower()


def _address_filter(address: str | Sequence[str] | None) -> str | list[str] | None:
    """The filter's ``address`` member, or ``None`` for an omitted key.

    A ``str`` stays a string on the wire and is never wrapped in a
    one-element list. Both spellings are legal JSON-RPC, and sending the
    one the caller did not write makes a recorded request harder to read
    back against the code that made it.

    Raises:
        ValidationError: on a container that is not a sequence of strings
            and on any entry that is not an address.
    """
    if address is None:
        return None
    if isinstance(address, str):
        return _checked_address(address)
    # bytes is a Sequence to Python and never a list of addresses here.
    if not isinstance(address, Sequence) or isinstance(address, bytes):
        raise ValidationError(
            f"scan_logs address must be a string or a sequence of them: {address!r}"
        )
    return [_checked_address(entry) for entry in address]


def _topic_slot(entry: object) -> object:
    """One position of the filter's ``topics`` array, as it goes on the wire.

    A ``str`` is itself, ``None`` is the JSON ``null`` that matches any topic
    in that position, and a list or tuple is a nested list, the encoding's
    topic OR. An EMPTY OR list is legal and widens: an empty rule set is the
    wildcard ``null`` already means, so the slot matches every topic.

    Raises:
        ValidationError: on anything else, including a ``None`` inside an
            OR list, which the encoding gives no meaning to.
    """
    if entry is None or isinstance(entry, str):
        return entry
    if isinstance(entry, (list, tuple)) and all(
        isinstance(item, str) for item in entry
    ):
        return list(entry)
    raise ValidationError(
        "a scan_logs topic is a string, a list or tuple of strings, or None "
        f"for a wildcard slot: {entry!r}"
    )


def _topic_filter(topics: Sequence[object]) -> list[object]:
    """The filter's ``topics`` array, empty when the caller named none.

    Raises:
        ValidationError: on ``topics`` that is not a sequence and on any
            slot :func:`_topic_slot` refuses. ``rpc.py`` forwards the
            filter unvalidated, so a filter this module cannot author is
            stopped here or not at all.
    """
    # A str is a Sequence of characters, and iterating one yields 66 slots
    # from a caller who meant a single topic0.
    if not isinstance(topics, Sequence) or isinstance(topics, (str, bytes)):
        raise ValidationError(f"scan_logs topics must be a sequence: {topics!r}")
    return [_topic_slot(entry) for entry in topics]


def scan_logs(
    rpc: EvmRpc,
    *,
    from_block: int,
    to_block: int,
    address: str | Sequence[str] | None = None,
    topics: Sequence[object] = (),
    chunk_blocks: int = DEFAULT_CHUNK_BLOCKS,
) -> list[LogRecord]:
    """Typed logs for the INCLUSIVE range, scanned one chunk at a time.

    Each chunk is one ``eth_getLogs`` through :meth:`EvmRpc.eth_get_logs`,
    carrying a filter object built here:

    * ``fromBlock`` and ``toBlock``, minimal lowercase hex from
      :func:`auradefi.sources.evm.rpc.block_tag`.
    * ``address`` ONLY when it is not None. A ``str`` emits the lowercased
      string; a sequence emits a list of lowercased strings.
    * ``topics`` ONLY when non-empty, as a JSON array where a ``str`` is
      itself, a tuple or list is a nested list (a topic OR), and ``None``
      is ``null`` (a wildcard slot).

    ``address=None`` and ``topics=()`` omit their key. An empty SEQUENCE is a
    different argument, goes out as written, and WIDENS the query instead of
    emptying it: ``address=[]`` sends ``"address": []``, which a node reads as
    no address filter at all, and ``topics=([],)`` sends ``"topics": [[]]``,
    whose empty rule set go-ethereum treats as the wildcard ``null`` means, so
    an empty OR slot and a ``None`` slot are one query once decoded. Either
    spelling scans every log in the range, and rewriting one to an omitted key
    would hide the caller's mistake behind a request that means the same thing.

    Returns:
        The rows of every chunk, concatenated in received order. An empty
        chunk contributes nothing and does not end the scan.

    Raises:
        ValidationError: before any HTTP, on ``from_block > to_block``, a
            negative ``from_block``, ``chunk_blocks <= 0``, a topic entry
            that is not a str, a str list or tuple, or None, and an
            address that is not a ``0x``-prefixed 40-hex string.
            A wrong TYPE for any of the three block arguments is refused
            here too. Each guard below is a comparison against an int
            literal, so a ``str`` or ``None`` would otherwise pass
            straight through the promise it is named in and die at
            ``hex()`` with a builtin's message.
        SourceError: on a row that is not an object, a missing or non-hex
            ``blockNumber`` or ``logIndex``, a ``topics`` that is not a
            list, a topic that is not a 66-character ``0x`` hex string, a
            non-hex ``data``, and a missing ``address`` or
            ``transactionHash``.
    """
    # Every argument is checked before the first chunk is built, so a
    # caller's mistake costs no request. A guard inside the loop refuses
    # the range only after a node has already answered part of it.
    _check_scan(from_block, to_block, chunk_blocks)
    address_filter = _address_filter(address)
    topic_filter = _topic_filter(topics)

    records: list[LogRecord] = []
    for low, high in _chunks(from_block, to_block, chunk_blocks):
        filter_object: dict[str, object] = {
            "fromBlock": block_tag(low),
            "toBlock": block_tag(high),
        }
        # `is not None` for the address and truthiness for the topics:
        # address=None and topics=() are the only arguments that omit a key.
        if address_filter is not None:
            filter_object["address"] = address_filter
        if topic_filter:
            filter_object["topics"] = topic_filter
        # extend, in received order, and no early exit on an empty chunk:
        # the range knows its end before the first request, so a chunk with
        # no logs in it is ordinary and the next chunk still goes out.
        records.extend(_record(row) for row in rpc.eth_get_logs(filter_object))
    return records
