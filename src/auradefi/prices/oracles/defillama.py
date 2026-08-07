"""DefiLlama price oracle, current and historical (SPEC §3.2; §6.3: being
Plaid-shaped forces us to own a price oracle).

Keyless, no retry, no rate limiting. The injected ``httpx.Client`` is the
only I/O path; import performs none. Conforms STRUCTURALLY to
``prices.inquirer.PriceOracle`` and to ``prices.inquirer
.HistoricalPriceOracle`` without importing either.

Pinned request layout (deterministic URLs so cassettes replay):
``GET {base_url}/prices/current/{coins}`` where ``coins`` is the
deduplicated, lexicographically sorted key set joined by ``','``, at most
``CHUNK_SIZE`` keys per request, chunks issued in global sorted order.

Pinned historical request layout, the same key set at a past instant:
``GET {base_url}/prices/historical/{unix_seconds}/{coins}``, with the SAME
deduplicated lexicographically sorted keys joined by ``','``, at most
``CHUNK_SIZE`` per request, chunks issued in global sorted order. Pinned
conversion ``unix_seconds = at_ms // 1000``: floor division, the
sub-second remainder is dropped, so 1620000000999 and 1620000000000 build
one URL.

This oracle NEVER buckets. It puts ``at_ms`` on the wire verbatim, and
``prices/historian.py`` is the only bucketer, which is what keeps a
recorded historical URL deterministic. It never declares an unreachable
instant either: DefiLlama reaches every instant its feed carries, and an
instant it holds no mark for comes back as an absent response key, which
is "not listed" rather than "cannot reach".

Pinned price conversion: ``Decimal(str(price))``, NEVER ``Decimal(price)``
from the raw float.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

import httpx

from auradefi.errors import (
    SourceError,
    ValidationError,
    require_int,
    require_sequence,
    require_str,
)
from auradefi.money.fiat import Money

#: Every root a send can raise, which is not the same set as ``httpx.HTTPError``.
#:
#: ``httpx.InvalidURL`` descends from ``Exception``, not ``HTTPError``, and
#: fires on a non-printable character anywhere in the URL, including one that
#: arrived in a caller-supplied address. A scheme-less url such as
#: ``localhost:8545``, the likeliest local-node typo, surfaces instead as
#: urllib's bare ``ValueError("unknown url type")`` from cookie extraction.
#: A door catching only ``HTTPError`` therefore keeps its documented
#: ``SourceError`` promise for the network and breaks it for the two most
#: ordinary configuration mistakes. Verified against httpx 0.28, and held by
#: ``tests/style/test_transport_doors_catch_every_httpx_root.py``.
_SEND_FAILURES = (httpx.HTTPError, httpx.InvalidURL, ValueError)

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
# Polygon's natives are BNB/POL: deliberately absent.
NATIVE_ETH_CHAINS: frozenset[int] = frozenset({1, 10, 8453, 42161})


def coin_key(caip19: str) -> str | None:
    """DefiLlama coin key for a CAIP-19 id, or ``None`` if unmapped.

    Pinned mapping (pure, no I/O, no registry):
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

    Empty input yields ``[]``. Pure: the request layout, unit-testable
    without HTTP.
    """
    ordered = sorted(set(keys))
    return [
        ordered[start : start + CHUNK_SIZE]
        for start in range(0, len(ordered), CHUNK_SIZE)
    ]


class DefiLlamaOracle:
    """Current and past USD prices from the keyless ``coins.llama.fi``.

    ``client`` is REQUIRED and injected: the oracle never constructs a
    transport of its own, never retries, never rate-limits. Structurally a
    ``prices.inquirer.PriceOracle`` and a ``prices.inquirer
    .HistoricalPriceOracle``; imports neither.
    """

    def __init__(
        self, client: httpx.Client, base_url: str = "https://coins.llama.fi"
    ) -> None:
        """Bind the injected client and base URL. Performs no I/O.

        ``base_url`` is refused here because ``.rstrip`` is the first
        thing to touch it: see :class:`~auradefi.sources.bitcoin.esplora
        .Esplora`, which has the same door.
        """
        self._client = client
        self._base_url = require_str(base_url, "base_url", SourceError).rstrip("/")

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
        # `coin_key` reaches straight for `caip19.partition("/")`, so a
        # non-sequence or a non-str element leaves as AttributeError or
        # TypeError, outside the SourceError this method documents.
        for caip19 in require_sequence(caip19s, "caip19s", SourceError):
            require_str(caip19, "caip19", SourceError)
        return self._priced(caip19s, "current")

    def usd_prices_at(
        self, caip19s: Sequence[str], at_ms: int
    ) -> dict[str, Money]:
        """USD price for each priceable input CAIP-19 at ``at_ms``.

        The id mapping, the chunking, the reverse mapping of response keys
        onto the caller's ids and the ``Money(Decimal(str(price)), 'USD')``
        conversion are :meth:`usd_prices`'s, unchanged. Only the URL
        differs: ``{base_url}/prices/historical/{at_ms // 1000}/{coins}``.

        ``at_ms`` goes on the wire verbatim, floored to whole seconds and
        never bucketed: the historian is the only bucketer.

        Unmapped ids contribute no request key and are absent from the
        result; if NO input maps, returns ``{}`` with zero HTTP. A key
        absent from the response's ``coins`` object is unpriced and absent
        from the result, which is "not listed" and not an unreachable
        instant.

        ``caip19s`` must be a list or tuple of strings and ``at_ms`` a
        non-negative integer, both refused with ``SourceError`` before any
        HTTP. A non-2xx response or a malformed body raises ``SourceError``
        too, so every failure this method has is one channel.
        """
        for caip19 in require_sequence(caip19s, "caip19s", SourceError):
            require_str(caip19, "caip19", SourceError)
        if require_int(at_ms, "at_ms", SourceError) < 0:
            raise SourceError(f"at_ms must not precede the epoch, got {at_ms}")
        # Floor, never round: 1620000000999 and 1620000000000 are the same
        # second on the wire, so a remainder cannot walk the instant on.
        return self._priced(caip19s, f"historical/{at_ms // 1000}")

    def _priced(self, caip19s: Sequence[str], endpoint: str) -> dict[str, Money]:
        """Map, chunk, fetch and reverse-map, which is everything both
        public methods do once their entry doors have run.

        ``endpoint`` is the segment between ``/prices/`` and the joined
        coin keys, either ``'current'`` or ``'historical/{unix_seconds}'``,
        and it is the only difference between a mark now and a mark then.
        Ids sharing one key each receive that key's price; a key the
        response body omits leaves its ids unpriced and absent.
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
            coins = self._coins(
                f"{self._base_url}/prices/{endpoint}/{','.join(chunk)}"
            )
            for key in chunk:
                quote = coins.get(key)
                if quote is None:
                    continue
                price = _quoted_price(key, quote)
                for caip19 in ids_by_key[key]:
                    result[caip19] = price
        return result

    def _coins(self, url: str) -> dict:
        """GET one fully built URL and return the response's ``coins``
        object; ``SourceError`` on non-2xx or a body that is not JSON with
        a ``coins`` mapping.

        The single transport door in this module. Both endpoints hand a
        finished URL here, so ``_SEND_FAILURES`` is caught in one place
        and the refusal ladder below is written once. A second door
        catching ``httpx.HTTPError`` alone would leak urllib's bare
        ``ValueError`` for a scheme-less base URL, and
        ``tests/style/test_transport_doors_catch_every_httpx_root.py``
        reads this file's AST to say so.
        """
        try:
            response = self._client.get(url)
        except _SEND_FAILURES as exc:
            raise SourceError(f"DefiLlama request failed: {exc!r}") from exc
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
