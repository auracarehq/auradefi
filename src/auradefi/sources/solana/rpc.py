"""Solana JSON-RPC transport: lamports, token accounts, signatures (SPEC §3.2, §10).

The HTTP half of the Solana source. ``spl.py`` is pure and knows no
network; this module knows the JSON-RPC envelope and knows nothing about
positions or fiat (SPEC §3.3). ``helius.py`` is deferred. The public RPC
is the only transport here.

Every request is a POST of exactly

    {"jsonrpc": "2.0", "id": 1, "method": <method>, "params": <params>}

to a single URL. NO retry, NO rate limiting, no ``commitment`` param
(out of scope). No network at import time and none in a constructor: the
``httpx.Client`` is REQUIRED and injected so cassettes plug in.

Base58 case is SIGNIFICANT on Solana, no string here is ever lowercased
(docs/internal/DECISIONS.md, asset-id pin), unlike the EVM source which
canonicalizes hex to lower case.

Amounts and slots parse as ``int``, never through float (SPEC rules
#1/#2). ``blockTime`` is carried in upstream SECONDS verbatim; converting
to the ms-epoch house unit is a decode/ledger concern, mirroring the
pinned Etherscan ``timeStamp`` rule.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from auradefi.chains.solana import validate_address
from auradefi.errors import SourceError, require_int, require_str
from auradefi.sources.solana import spl

#: See ``sources/evm/rpc.py`` for why ``httpx.HTTPError`` alone under-catches
#: a send: ``InvalidURL`` is not one of its subclasses and a scheme-less url
#: raises urllib's bare ``ValueError``. Held by
#: ``tests/style/test_transport_doors_catch_every_httpx_root.py``.
_SEND_FAILURES = (httpx.HTTPError, httpx.InvalidURL, ValueError)

DEFAULT_URL = "https://api.mainnet-beta.solana.com"

# Token-2022 accounts are owned by a DIFFERENT program, so one
# getTokenAccountsByOwner call can never return both sets.
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@dataclass(frozen=True, slots=True)
class SignatureRecord:
    """One row of ``getSignaturesForAddress``.

    ``block_time`` is the upstream ``blockTime`` in SECONDS, verbatim, or
    ``None`` when the node reports null. ``failed`` is ``err is not
    None``. The error payload itself is not carried, only the fact.
    """

    signature: str
    slot: int
    block_time: int | None
    failed: bool


def _signature_record(row: object) -> SignatureRecord:
    """One ``getSignaturesForAddress`` row as a record, or ``SourceError``.

    ``bool`` is rejected for the integer members before the ``int``
    check. ``Bool`` is an ``int`` subclass, and a slot of ``True`` is a
    malformed row, never a height. A missing ``err`` reads as success; a
    missing ``blockTime`` reads as ``None``.
    """
    if not isinstance(row, dict):
        raise SourceError(f"signature row must be an object, got {type(row).__name__}")
    signature = row.get("signature")
    if not isinstance(signature, str):
        raise SourceError(f"signature must be a string: {signature!r}")
    slot = row.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int):
        raise SourceError(f"slot must be an int: {slot!r}")
    block_time = row.get("blockTime")
    if block_time is not None and (
        isinstance(block_time, bool) or not isinstance(block_time, int)
    ):
        raise SourceError(f"blockTime must be an int or null: {block_time!r}")
    return SignatureRecord(signature, slot, block_time, row.get("err") is not None)


class SolanaRpc:
    """Solana JSON-RPC over an injected ``httpx.Client``.

    The client is REQUIRED and injected; the constructor performs no I/O.
    Every public method validates its address BEFORE any HTTP, so a bad
    address raises ``ValidationError`` without touching the network.
    """

    def __init__(self, client: httpx.Client, url: str = DEFAULT_URL) -> None:
        """Bind the injected client and endpoint URL. No I/O.

        ``url`` is refused here, not at the send: it is host
        configuration, an unset environment variable arrives as ``None``,
        and httpx answers a non-str url with a bare ``TypeError``.
        """
        self._client = client
        self._url = require_str(url, "url", SourceError)

    def get_balance(self, address: str) -> int:
        """The address's native balance in lamports.

        Calls ``getBalance`` with params ``[address]``. The result must
        be an object whose ``value`` is a non-``bool`` ``int >= 0``. An
        upstream JSON integer, read as an ``int`` and never through
        float.

        Raises:
            ValidationError: on a non-base58 address, before any HTTP.
            SourceError: on any transport, envelope or shape failure.
        """
        validate_address(address)  # ValidationError pre-HTTP
        result = self._call("getBalance", [address])
        if not isinstance(result, dict):
            raise SourceError(
                f"getBalance result must be an object, got {type(result).__name__}"
            )
        value = result.get("value")
        if isinstance(value, bool) or not isinstance(value, int):
            raise SourceError(f"getBalance value must be an int: {value!r}")
        if value < 0:
            raise SourceError(f"getBalance value must be >= 0, got {value}")
        return value

    def get_token_accounts_by_owner(self, address: str) -> list:
        """The owner's SPL token-account rows, UNPARSED, both programs.

        Issues TWO calls in this pinned order, ``[address, {"programId":
        TOKEN_PROGRAM}, {"encoding": "jsonParsed"}]`` then the same with
        :data:`TOKEN_2022_PROGRAM`, and returns their ``result.value``
        lists concatenated in that order. Rows are handed back exactly as
        received: parsing is :mod:`auradefi.sources.solana.spl`'s job.

        Raises:
            ValidationError: on a non-base58 address, before any HTTP.
            SourceError: when either result lacks a ``value`` list.
        """
        validate_address(address)  # ValidationError pre-HTTP
        rows: list = []
        for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
            result = self._call(
                "getTokenAccountsByOwner",
                [address, {"programId": program}, {"encoding": "jsonParsed"}],
            )
            value = result.get("value") if isinstance(result, dict) else None
            if not isinstance(value, list):
                raise SourceError(
                    f"getTokenAccountsByOwner result for {program} "
                    f"has no value list: {result!r}"
                )
            rows.extend(value)
        return rows

    def get_signatures(self, address: str, limit: int = 1000) -> list[SignatureRecord]:
        """The address's signature history, newest first, fully paged.

        The first page sends ``[address, {"limit": limit}]``; every later
        page sends ``[address, {"limit": limit, "before": <the previous
        page's LAST record's signature>}]``. Records append in received
        order. The loop STOPS when a page returns fewer than ``limit``
        rows, zero included, which is the only terminator.

        Each row must be an object carrying a ``signature`` str, a
        non-``bool`` ``int`` ``slot``, a ``blockTime`` that is a
        non-``bool`` ``int`` or ``None``, and ``err``: ``failed`` is
        ``err is not None``.

        Raises:
            ValidationError: on a non-base58 address, before any HTTP.
            SourceError: when a result is not a list or a row is
                malformed.
        """
        validate_address(address)  # ValidationError pre-HTTP
        require_int(limit, "limit", SourceError)
        records: list[SignatureRecord] = []
        before: str | None = None
        while True:
            page: dict = {"limit": limit}
            if before is not None:
                page["before"] = before
            result = self._call("getSignaturesForAddress", [address, page])
            if not isinstance(result, list):
                raise SourceError(
                    "getSignaturesForAddress result must be a list, got "
                    f"{type(result).__name__}"
                )
            records.extend(_signature_record(row) for row in result)
            # A short page ends the walk. An empty page is terminal
            # unconditionally, so a non-positive limit cannot spin.
            if not result or len(result) < limit:
                return records
            before = records[-1].signature

    def _call(self, method: str, params: list) -> object:
        """One JSON-RPC POST; the envelope's ``result`` member, untyped.

        ``result`` is an object for the balance and token-account calls
        and a list for the signature call, so its shape is checked by
        the caller, not here.

        Posts exactly ``{"jsonrpc": "2.0", "id": 1, "method": method,
        "params": params}``.

        Raises:
            SourceError: on ``httpx.HTTPError``, a non-2xx status, a
                non-JSON body, a body that is not an object, an
                ``error`` member (its ``code`` and ``message`` are
                embedded in the exception text), or a missing
                ``result``.
        """
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            response = self._client.post(self._url, json=payload)
        except _SEND_FAILURES as exc:
            raise SourceError(f"solana rpc request failed: {exc!r}") from exc
        if not 200 <= response.status_code < 300:
            raise SourceError(f"solana rpc HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SourceError("solana rpc returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise SourceError(
                f"solana rpc body must be an object, got {type(body).__name__}"
            )
        error = body.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else error
            raise SourceError(
                f"solana rpc {method} error: code={code!r} message={message!r}"
            )
        if "result" not in body:
            raise SourceError(f"solana rpc {method} response carries no result")
        return body["result"]


class SolanaBalances:
    """An address's typed Solana balances, assembled over a :class:`SolanaRpc`.

    Composes the transport with the pure ``spl`` half: validate, fetch
    lamports, fetch both programs' token accounts, then
    ``build_balances(lamports, aggregate_by_mint(parse_token_accounts(rows)))``.
    """

    def __init__(self, rpc: SolanaRpc) -> None:
        """Bind the RPC transport. No I/O."""
        self._rpc = rpc

    def balances(self, address: str) -> list[spl.SolanaBalance]:
        """Native SOL first (when non-zero), then one record per mint.

        Mints are ordered ascending by base58 mint (case-sensitive) and
        zero-raw holdings are omitted. See
        :func:`auradefi.sources.solana.spl.build_balances`.

        Raises:
            ValidationError: on a non-base58 address, before any HTTP.
            SourceError: on any transport, envelope or row failure.
        """
        validate_address(address)  # ValidationError pre-HTTP
        lamports = self._rpc.get_balance(address)
        rows = self._rpc.get_token_accounts_by_owner(address)
        return spl.build_balances(
            lamports, spl.aggregate_by_mint(spl.parse_token_accounts(rows))
        )
