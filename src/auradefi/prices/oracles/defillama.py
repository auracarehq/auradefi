"""DefiLlama current-price oracle (SPEC §3.2; §6.3 — being Plaid-shaped
forces us to own a price oracle).

Keyless, no retry, no rate limiting. The injected ``httpx.Client`` is the
only I/O path; import performs none. Conforms STRUCTURALLY to
``prices.inquirer.PriceOracle`` without importing it.

Pinned request layout (deterministic URLs so cassettes replay):
``GET {base_url}/prices/current/{coins}`` where ``coins`` is the
deduplicated, lexicographically sorted key set joined by ``','``, at most
``CHUNK_SIZE`` keys per request, chunks issued in global sorted order.

Pinned price conversion: ``Decimal(str(price))`` — NEVER ``Decimal(price)``
from the raw float.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

import httpx

from auradefi.errors import SourceError, ValidationError
from auradefi.money.fiat import Money

CHUNK_SIZE = 100

# CAIP-19 eip155 chain reference -> DefiLlama chain slug (pinned).
EVM_SLUGS: dict[int, str] = {
    1: "ethereum",
    10: "optimism",
    56: "bsc",
    137: "polygon",
    8453: "base",
    42161: "arbitrum",
}

# Chains whose slip44:60 native asset IS ether (pinned). BSC's and
# Polygon's natives are BNB/POL — deliberately absent.
NATIVE_ETH_CHAINS: frozenset[int] = frozenset({1, 10, 8453, 42161})


def coin_key(caip19: str) -> str | None:
    """DefiLlama coin key for a CAIP-19 id, or ``None`` if unmapped.

    Pinned mapping (pure — no I/O, no registry):
      * ``eip155:{N}/erc20:0x…`` -> ``'{slug}:0x…'`` with the address
        lowercased, for ``N`` in ``EVM_SLUGS``.
      * ``eip155:{N}/slip44:60`` -> ``'coingecko:ethereum'`` for ``N`` in
        ``NATIVE_ETH_CHAINS``.
      * Everything else -> ``None``.
    """
    chain_part, separator, asset_part = caip19.partition("/")
    if not separator:
        return None
    chain_namespace, _, chain_reference = chain_part.partition(":")
    if chain_namespace != "eip155" or not chain_reference.isdecimal():
        return None
    chain_id = int(chain_reference)
    asset_namespace, _, asset_reference = asset_part.partition(":")
    if (
        asset_namespace == "erc20"
        and chain_id in EVM_SLUGS
        and asset_reference.startswith("0x")
    ):
        return f"{EVM_SLUGS[chain_id]}:{asset_reference.lower()}"
    if (
        asset_namespace == "slip44"
        and asset_reference == "60"
        and chain_id in NATIVE_ETH_CHAINS
    ):
        return "coingecko:ethereum"
    return None


def chunk_keys(keys: Sequence[str]) -> list[list[str]]:
    """Deduplicate and lexicographically sort ``keys``, then split into
    chunks of at most ``CHUNK_SIZE`` preserving the global sorted order.

    Empty input yields ``[]``. Pure — the request layout, unit-testable
    without HTTP.
    """
    ordered = sorted(set(keys))
    return [
        ordered[start : start + CHUNK_SIZE]
        for start in range(0, len(ordered), CHUNK_SIZE)
    ]


class DefiLlamaOracle:
    """Current USD prices from DefiLlama's keyless ``coins.llama.fi``.

    ``client`` is REQUIRED and injected — the oracle never constructs a
    transport of its own, never retries, never rate-limits. Structurally a
    ``prices.inquirer.PriceOracle``; does not import it.
    """

    def __init__(
        self, client: httpx.Client, base_url: str = "https://coins.llama.fi"
    ) -> None:
        """Bind the injected client and base URL. Performs no I/O."""
        self._client = client
        self._base_url = base_url.rstrip("/")

    def usd_prices(self, caip19s: Sequence[str]) -> dict[str, Money]:
        """Current USD price for each priceable input CAIP-19.

        Unmapped ids contribute no request key and are absent from the
        result; if NO input maps, returns ``{}`` with zero HTTP. Keys
        absent from the response's ``coins`` object are unpriced and
        absent from the result. Response keys are reverse-mapped onto the
        caller's ids (ids sharing one key each receive its price).

        Amounts are ``Decimal(str(price))`` wrapped in ``Money(…, 'USD')``.
        A non-2xx response or a malformed body raises ``SourceError``.
        """
        ids_by_key: dict[str, list[str]] = {}
        for caip19 in caip19s:
            key = coin_key(caip19)
            if key is not None:
                ids_by_key.setdefault(key, []).append(caip19)
        if not ids_by_key:
            return {}

        result: dict[str, Money] = {}
        for chunk in chunk_keys(list(ids_by_key)):
            coins = self._fetch_coins(chunk)
            for key in chunk:
                quote = coins.get(key)
                if quote is None:
                    continue
                price = _quoted_price(key, quote)
                for caip19 in ids_by_key[key]:
                    result[caip19] = price
        return result

    def _fetch_coins(self, chunk: list[str]) -> dict:
        """GET one chunk's ``/prices/current/{coins}`` and return the
        response's ``coins`` object; ``SourceError`` on non-2xx or a body
        that is not JSON with a ``coins`` mapping."""
        url = f"{self._base_url}/prices/current/{','.join(chunk)}"
        response = self._client.get(url)
        if not response.is_success:
            raise SourceError(
                f"DefiLlama returned HTTP {response.status_code} for {url}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise SourceError(f"DefiLlama returned non-JSON body for {url}") from exc
        coins = body.get("coins") if isinstance(body, dict) else None
        if not isinstance(coins, dict):
            raise SourceError(f"DefiLlama body has no 'coins' object for {url}")
        return coins


def _quoted_price(key: str, quote: object) -> Money:
    """``Money(Decimal(str(price)), 'USD')`` from one ``coins`` entry;
    ``SourceError`` if the entry has no usable finite ``price``."""
    try:
        return Money(Decimal(str(quote["price"])), "USD")
    except (TypeError, KeyError, InvalidOperation, ValidationError) as exc:
        raise SourceError(f"DefiLlama quote for {key!r} is malformed") from exc
