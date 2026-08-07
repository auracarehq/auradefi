"""Etherscan V2 EVM balance source (SPEC §3.2, §3.3, §10 EVM row).

One API key, 50+ chain ids, every request a GET on a single ``/v2/api``
base URL. A source turns raw chain bytes into typed records: this module
knows HTTP and the Etherscan envelope, and knows nothing about positions
or fiat (SPEC §3.3).

Request flow for :meth:`EtherscanV2.balances`: the CAIP-2 ``chain_id``
reference ``N`` becomes the ``chainid`` query param, and the input address
is lowercased before use:

1. ``module=account&action=balance&address=<addr>&tag=latest``: native
   balance as a wei string.
2. Discovery: ``module=account&action=tokentx&address=<addr>&startblock=0&
   endblock=99999999&page=<n from 1>&offset=<page_size>&sort=asc``,
   fetch the next page while a page returned exactly ``page_size`` rows
   (July 2026 cut: max 1,000 records per request, paginate correctly
   from day one). Collect DISTINCT ``contractAddress`` values, lowercased.
   Rows whose ``tokenDecimal`` is not a base-10 integer string are skipped
   additively: a bad spam row never crashes the scan.
3. ``module=account&action=tokenbalance&contractaddress=<c>&address=<addr>
   &tag=latest`` for each discovered contract, in ascending lexicographic
   order of the lowercased contract address (deterministic for cassettes).

``apikey=<api_key>`` is appended only when ``api_key`` is not ``None``.
NO retry, NO rate limiting (out of Phase 1 scope). No network at import
time. Amounts parse via ``int()`` from the result STRINGS, never through
float (SPEC rules #1/#2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from auradefi.chains.evm import chain_id_from_caip2
from auradefi.errors import SourceError, require_str
from auradefi.money.quantity import Quantity

#: See ``sources/evm/rpc.py`` for why ``httpx.HTTPError`` alone under-catches
#: a send: ``InvalidURL`` is not one of its subclasses and a scheme-less url
#: raises urllib's bare ``ValueError``. Held by
#: ``tests/style/test_transport_doors_catch_every_httpx_root.py``.
_SEND_FAILURES = (httpx.HTTPError, httpx.InvalidURL, ValueError)

_NO_TRANSACTIONS = "No transactions found"
_NATIVE_DECIMALS = 18

# Unsigned base-10 digit strings only. Bare int() is too lenient for API
# amounts ("1_0", " 10 ", "+1" all parse); the envelope contract is digits.
_DIGITS = re.compile(r"[0-9]+")


@dataclass(frozen=True, slots=True)
class BalanceRecord:
    """One typed balance owned by an address on one EVM chain.

    Native coin: ``caip19`` is ``f"eip155:{N}/slip44:60"``, ``symbol`` is
    ``"ETH"``, ``contract_address`` is ``None``, ``quantity`` is
    ``Quantity(int(wei_string), 18)``.

    ERC-20 token: ``caip19`` is ``f"eip155:{N}/erc20:{contract}"`` with the
    contract address lowercased (DECISIONS pinned canonicalization),
    ``symbol`` from the discovery row's ``tokenSymbol``, ``decimals`` from
    ``int(tokenDecimal)``, ``contract_address`` the lowercased contract.
    """

    caip19: str
    symbol: str | None
    quantity: Quantity
    contract_address: str | None


def _parse_discovery_row(row: object) -> tuple[str, str | None, int] | None:
    """``(lowercased contract, symbol, decimals)`` or ``None`` to skip.

    Additive by design: a row that is not a dict, lacks a string
    ``contractAddress``, or whose ``tokenDecimal`` is not a base-10
    integer string is skipped: a bad spam row never crashes the scan.
    A non-string ``tokenSymbol`` degrades to ``None``, not a skip.
    """
    if not isinstance(row, dict):
        return None
    contract = row.get("contractAddress")
    decimals = row.get("tokenDecimal")
    if not isinstance(contract, str) or not contract:
        return None
    if not isinstance(decimals, str) or _DIGITS.fullmatch(decimals) is None:
        return None
    symbol = row.get("tokenSymbol")
    return contract.lower(), symbol if isinstance(symbol, str) else None, int(decimals)


class EtherscanV2:
    """Etherscan V2 balance source over an injected ``httpx.Client``.

    The client is REQUIRED and injected so cassettes plug in; the
    constructor performs no I/O. ``api_key=None`` omits the ``apikey``
    query param entirely.
    """

    def __init__(
        self,
        client: httpx.Client,
        api_key: str | None = None,
        base_url: str = "https://api.etherscan.io/v2/api",
        page_size: int = 1000,
    ) -> None:
        """Bind the injected client and request parameters. No I/O.

        ``base_url`` is refused here rather than at the send. It is host
        configuration, an unset environment variable arrives as ``None``,
        and httpx answers a non-str url with a bare ``TypeError`` that no
        ``except AuradefiError`` catches.
        """
        self._client = client
        self._api_key = api_key
        self._base_url = require_str(base_url, "base_url", SourceError)
        self._page_size = page_size

    def balances(self, chain_id: str, address: str) -> list[BalanceRecord]:
        """All non-zero balances for ``address`` on ``chain_id``.

        ``chain_id`` is CAIP-2: the namespace must be ``"eip155"`` with a
        base-10 integer reference, else ``ValidationError`` is raised
        BEFORE any HTTP. The input ``address`` is lowercased before use.

        Envelope ``{status, message, result}``: status ``"1"`` is success;
        status ``"0"`` with message ``"No transactions found"`` is an
        EMPTY discovery result, not an error; any other status ``"0"``,
        any non-2xx HTTP, and any malformed body raise ``SourceError``
        (auradefi.errors).

        Records with ``quantity.raw == 0`` are omitted (native included
        only when non-zero). Return order: native first, then tokens
        ascending by lowercased contract address.
        """
        reference = chain_id_from_caip2(chain_id)  # ValidationError pre-HTTP
        address = require_str(address, "address", SourceError).lower()

        records: list[BalanceRecord] = []
        wei = self._native_wei(reference, address)
        if wei != 0:
            records.append(
                BalanceRecord(
                    caip19=f"eip155:{reference}/slip44:60",
                    symbol="ETH",
                    quantity=Quantity(wei, _NATIVE_DECIMALS),
                    contract_address=None,
                )
            )

        tokens = self._discover_tokens(reference, address)
        for contract in sorted(tokens):
            symbol, decimals = tokens[contract]
            raw = self._token_raw(reference, address, contract)
            if raw == 0:
                continue
            records.append(
                BalanceRecord(
                    caip19=f"eip155:{reference}/erc20:{contract}",
                    symbol=symbol,
                    quantity=Quantity(raw, decimals),
                    contract_address=contract,
                )
            )
        return records

    def _get(self, params: dict[str, str]) -> dict:
        """One GET on ``base_url``; the parsed envelope dict or SourceError.

        ``apikey`` is appended only when ``api_key`` is not ``None``.
        Non-2xx status, transport failure, a non-JSON body, and a body
        that is not an object carrying ``status`` and ``result`` all
        raise ``SourceError``.
        """
        if self._api_key is not None:
            params = {**params, "apikey": self._api_key}
        try:
            response = self._client.get(self._base_url, params=params)
        except _SEND_FAILURES as exc:
            raise SourceError(f"etherscan request failed: {exc!r}") from exc
        if not 200 <= response.status_code < 300:
            raise SourceError(f"etherscan HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SourceError("etherscan returned a non-JSON body") from exc
        if not isinstance(body, dict) or "status" not in body or "result" not in body:
            raise SourceError("etherscan envelope is missing status/result")
        return body

    @staticmethod
    def _success_result(envelope: dict, action: str) -> object:
        """The ``result`` of a status-``"1"`` envelope; else SourceError."""
        if envelope["status"] != "1":
            raise SourceError(
                f"etherscan {action} error: message={envelope.get('message')!r} "
                f"result={envelope.get('result')!r}"
            )
        return envelope["result"]

    @staticmethod
    def _amount(value: object) -> int:
        """An unsigned base-10 amount string as ``int``, never via float."""
        if not isinstance(value, str) or _DIGITS.fullmatch(value) is None:
            raise SourceError(f"malformed amount in etherscan result: {value!r}")
        return int(value)

    def _native_wei(self, reference: int, address: str) -> int:
        """The native balance in wei via ``action=balance``."""
        envelope = self._get(
            {
                "chainid": str(reference),
                "module": "account",
                "action": "balance",
                "address": address,
                "tag": "latest",
            }
        )
        return self._amount(self._success_result(envelope, "balance"))

    def _discover_tokens(
        self, reference: int, address: str
    ) -> dict[str, tuple[str | None, int]]:
        """DISTINCT lowercased contracts -> ``(symbol, decimals)``.

        Pages ``action=tokentx`` from page 1, fetching the next page while
        a page returned exactly ``page_size`` rows. First-seen row wins
        for a contract's symbol/decimals. Status ``"0"`` with message
        ``"No transactions found"`` ends discovery empty-handed; any
        other non-``"1"`` status raises ``SourceError``.
        """
        found: dict[str, tuple[str | None, int]] = {}
        page = 1
        while True:
            envelope = self._get(
                {
                    "chainid": str(reference),
                    "module": "account",
                    "action": "tokentx",
                    "address": address,
                    "startblock": "0",
                    "endblock": "99999999",
                    "page": str(page),
                    "offset": str(self._page_size),
                    "sort": "asc",
                }
            )
            if (
                envelope["status"] == "0"
                and envelope.get("message") == _NO_TRANSACTIONS
            ):
                return found
            rows = self._success_result(envelope, "tokentx")
            if not isinstance(rows, list):
                raise SourceError("etherscan tokentx result is not a list")
            for row in rows:
                parsed = _parse_discovery_row(row)
                if parsed is not None and parsed[0] not in found:
                    found[parsed[0]] = (parsed[1], parsed[2])
            if len(rows) != self._page_size:
                return found
            page += 1

    def _token_raw(self, reference: int, address: str, contract: str) -> int:
        """The raw ERC-20 balance via ``action=tokenbalance``."""
        envelope = self._get(
            {
                "chainid": str(reference),
                "module": "account",
                "action": "tokenbalance",
                "contractaddress": contract,
                "address": address,
                "tag": "latest",
            }
        )
        return self._amount(self._success_result(envelope, "tokenbalance"))
