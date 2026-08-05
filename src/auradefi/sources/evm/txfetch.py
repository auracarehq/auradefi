"""Etherscan V2 txlist/tokentx history fetchers (SPEC §10 EVM history).

The fetching half of "raw chain bytes -> typed records" for EVM account
history. This module owns HTTP against the single Etherscan V2 endpoint
and NOTHING about parsing. Every row goes through
``auradefi.sources.evm.txlist.parse_normal_row`` /
``parse_tokentx_row`` (txlist.py is the only parsing authority; no
duplicate parsing here) and their ``SourceError`` propagates untouched.

Both fetchers take an INJECTED ``httpx.Client``: this module never
constructs a client and never reads the environment; cassette transports
plug straight in. Each request is a GET on
``https://api.etherscan.io/v2/api`` with query params in EXACTLY this
order: ``chainid``, ``module=account``, ``action=txlist|tokentx``,
``address``, ``startblock``, ``endblock``, ``page``, ``offset``,
``sort``, ``apikey`` (omitted entirely when there is no key).

:func:`fetch_page` is the window-aware public seam: ONE page of ONE
block window, raw dicts, nothing widened or retried. The two whole-history
fetchers page over it, and ``sources.evm.source.EtherscanSource`` answers
the embedding engine's chosen window with it, so the envelope quirks below
are known in exactly one place.

Pagination (July 2026 cut: max 1,000 records per request: paginate
correctly from day one): request ``page=1,2,...`` while a page returns
exactly ``page_size`` rows; stop on the first shorter page; concatenate
records in delivery order.

Envelope ``{status, message, result}``: HTTP 200 with status ``"0"`` and
message ``"No transactions found"`` is an EMPTY history. Return ``()``,
not an error. A non-2xx response, a non-JSON body, and status ``"0"``
with any other message raise ``auradefi.errors.SourceError`` carrying
the Etherscan message. NO retries, NO throttling, no network at import
time.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import httpx

from auradefi.errors import SourceError, ValidationError
from auradefi.sources.evm.txlist import (
    NormalTxRecord,
    TokenTxRecord,
    parse_normal_row,
    parse_tokentx_row,
)

BASE_URL = "https://api.etherscan.io/v2/api"
HEAD_BLOCK = 99_999_999
_NO_TRANSACTIONS = "No transactions found"

_RecordT = TypeVar("_RecordT")


def fetch_txlist(
    client: httpx.Client,
    *,
    chain_id: int,
    address: str,
    api_key: str,
    page_size: int = 1000,
) -> tuple[NormalTxRecord, ...]:
    """All ``module=account&action=txlist`` rows for ``address``, typed.

    Pages ``page=1,2,...`` with ``offset=page_size`` while a page returns
    exactly ``page_size`` rows; stops on the first shorter page; returns
    the parsed records concatenated in delivery order as a tuple.

    Status ``"0"`` + message ``"No transactions found"`` -> ``()``.
    Raises ``ValidationError`` if ``page_size < 1`` (before any request:
    the short-page termination test cannot hold otherwise). Raises
    ``SourceError`` on non-2xx HTTP, a non-JSON body, any other
    status-``"0"`` message (carrying that message), or a malformed row
    (propagated from ``parse_normal_row``).
    """
    _require_valid_page_size(page_size)
    return _fetch_all(
        client,
        chain_id=chain_id,
        address=address,
        api_key=api_key,
        page_size=page_size,
        action="txlist",
        parse_row=parse_normal_row,
    )


def fetch_tokentx(
    client: httpx.Client,
    *,
    chain_id: int,
    address: str,
    api_key: str,
    page_size: int = 1000,
) -> tuple[TokenTxRecord, ...]:
    """All ``module=account&action=tokentx`` rows for ``address``, typed.

    Same request shape, pagination, empty-history and error contract as
    :func:`fetch_txlist`, with ``action=tokentx`` and rows parsed by
    ``parse_tokentx_row`` (contract addresses lowercased there).
    """
    _require_valid_page_size(page_size)
    return _fetch_all(
        client,
        chain_id=chain_id,
        address=address,
        api_key=api_key,
        page_size=page_size,
        action="tokentx",
        parse_row=parse_tokentx_row,
    )


def _require_valid_page_size(page_size: int) -> None:
    """Reject ``page_size < 1`` before any request leaves the process.

    Termination relies on "a page shorter than ``page_size`` is the last
    page"; with ``page_size <= 0`` no page is ever shorter, so pagination
    would loop against the endpoint forever.
    """
    if page_size < 1:
        raise ValidationError(f"page_size must be >= 1, got {page_size}")


def _fetch_all(
    client: httpx.Client,
    *,
    chain_id: int,
    address: str,
    api_key: str,
    page_size: int,
    action: str,
    parse_row: Callable[[dict], _RecordT],
) -> tuple[_RecordT, ...]:
    """Every ``action`` row across pages, parsed in delivery order."""
    records: list[_RecordT] = []
    page = 1
    while True:
        rows = fetch_page(
            client,
            chain_id=chain_id,
            address=address,
            action=action,
            page=page,
            offset=page_size,
            api_key=api_key,
        )
        records.extend(parse_row(row) for row in rows)
        if len(rows) != page_size:
            return tuple(records)
        page += 1


def fetch_page(
    client: httpx.Client,
    *,
    chain_id: int,
    address: str,
    action: str = "txlist",
    start_block: int = 0,
    end_block: int = HEAD_BLOCK,
    page: int = 1,
    offset: int = 1000,
    sort: str = "asc",
    api_key: str | None = None,
    base_url: str = BASE_URL,
) -> list[dict]:
    """ONE page of ONE block window, as raw row dicts; ``[]`` when empty.

    The public seam under both callers: :func:`fetch_txlist` /
    :func:`fetch_tokentx` walk whole histories with it, and
    ``sources.evm.source.EtherscanSource`` answers the embedding engine's
    chosen window with it. Rows come back RAW and unparsed because the two
    callers want different things, typed records there, dicts for the
    decoder seam here, and because parsing authority lives in
    ``txlist.py`` (SPEC §3.2), never in a fetcher.

    Every window parameter is the CALLER's: the engine picks
    ``start_block``/``end_block``/``page``/``sort`` and its budget depends
    on getting exactly the page it asked for, so nothing is widened,
    retried or paginated here.

    ``api_key=None`` omits the ``apikey`` param entirely rather than
    sending it empty: matching :class:`~auradefi.sources.evm.etherscan
    .EtherscanV2`. This is not cosmetic: ``apikey=`` is a DIFFERENT URL,
    so a keyless request would miss a keyless recording and Etherscan
    would answer it differently.

    Returns ``[]`` for a status-``"0"`` ``"No transactions found"``
    envelope. An empty history is not an error. Raises ``SourceError``
    on transport failure, non-2xx HTTP, a non-JSON body, a malformed
    envelope, any other status-``"0"`` message (carrying that message),
    or a ``result`` that is not a list of objects.
    """
    # Insertion order IS the wire order. The param sequence is contractual.
    params = {
        "chainid": str(chain_id),
        "module": "account",
        "action": action,
        "address": address,
        "startblock": str(start_block),
        "endblock": str(end_block),
        "page": str(page),
        "offset": str(offset),
        "sort": sort,
    }
    if api_key is not None:
        params["apikey"] = api_key
    try:
        response = client.get(base_url, params=params)
    except httpx.HTTPError as exc:
        raise SourceError(f"etherscan {action} request failed: {exc!r}") from exc
    if not 200 <= response.status_code < 300:
        raise SourceError(f"etherscan {action} HTTP {response.status_code}")
    try:
        envelope = response.json()
    except ValueError as exc:
        raise SourceError(f"etherscan {action} returned a non-JSON body") from exc
    if not isinstance(envelope, dict):
        raise SourceError(f"etherscan {action} envelope is not an object")
    status = envelope.get("status")
    message = envelope.get("message")
    if status == "0" and message == _NO_TRANSACTIONS:
        return []
    if status != "1":
        raise SourceError(f"etherscan {action} error: message={message!r}")
    rows = envelope.get("result")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SourceError(f"etherscan {action} result is not a list of row objects")
    return rows
