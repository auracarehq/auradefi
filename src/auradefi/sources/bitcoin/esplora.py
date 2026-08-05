"""Blockstream Esplora client + gap-limit scanner (SPEC §3.2, §10 Bitcoin
row, §4.2 bip122).

Keyless: Esplora has no API key. The ``httpx.Client`` is REQUIRED and
injected (cassettes plug in); construction and import perform no I/O.
NO retry, NO rate limiting. The client never validates address syntax.
Whatever string it is handed goes straight into the URL path.

``GET {base}/address/{address}`` → :class:`AddressStats` parsed from
``chain_stats`` ONLY (``mempool_stats`` is IGNORED, Esplora ships sats
as JSON ints, safe below 2**53; SPEC rule #2 governs OUR output, not
vendor parsing). ``GET {base}/address/{address}/utxo`` → ordered
``tuple[Utxo, ...]``; UTXOs are money, so a malformed row raises
``SourceError``: strict, unlike etherscan's additive spam-skip.
Transport failure, non-2xx, non-JSON, and missing fields all raise
``SourceError`` (auradefi.errors).

:func:`scan` is the gap-limit scanner, derivation-agnostic: it receives
a ``derive(chain, start, count)`` callable (the residual signature of
``functools.partial(xpub.derive_addresses, xpub_str, kind)``) and never
sees an extended key. Keys never leave the box (SPEC §10). Semantics
are PINNED in DECISIONS "Gap-limit scan".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import httpx

from auradefi.errors import SourceError, ValidationError
from auradefi.sources.bitcoin.utxo import (
    AddressBalance,
    AddressStats,
    BitcoinScanResult,
    Utxo,
)

_CHAINS = (0, 1)  # BIP44 external then change, in that order (DECISIONS)


def _stats_field(chain_stats: dict, key: str) -> int:
    """One ``chain_stats`` counter as an int, or ``SourceError``.

    A missing key and a non-int (bool included: Esplora ships counters as
    JSON numbers, never booleans) are both malformed vendor data.
    """
    value = chain_stats.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceError(f"esplora chain_stats.{key} is malformed: {value!r}")
    return value


def _parse_utxo(row: object) -> Utxo:
    """One ``{txid, vout, value, status: {confirmed}}`` row, STRICT.

    UTXOs are money: a row that is not an object, lacks a field, or fails
    :class:`Utxo` validation raises ``SourceError``, never skipped.
    """
    if not isinstance(row, dict):
        raise SourceError(f"esplora utxo row is not an object: {row!r}")
    status = row.get("status")
    try:
        return Utxo(
            txid=row["txid"],
            vout=row["vout"],
            value_sats=row["value"],
            confirmed=status["confirmed"] if isinstance(status, dict) else None,
        )
    except (KeyError, TypeError, ValidationError) as exc:
        raise SourceError(f"malformed esplora utxo row: {row!r}") from exc


class Esplora:
    """Esplora HTTP source over an injected ``httpx.Client``. Keyless."""

    def __init__(
        self,
        client: httpx.Client,
        base_url: str = "https://blockstream.info/api",
    ) -> None:
        """Bind the injected client and base URL. No I/O, no key."""
        self._client = client
        self._base_url = base_url.rstrip("/")

    def address_stats(self, address: str) -> AddressStats:
        """``GET {base}/address/{address}`` → chain_stats as AddressStats.

        ``mempool_stats`` is ignored entirely. Transport failure,
        non-2xx, non-JSON, and missing fields raise ``SourceError``.
        Address syntax is never validated here.
        """
        body = self._get(f"/address/{address}")
        chain_stats = body.get("chain_stats") if isinstance(body, dict) else None
        if not isinstance(chain_stats, dict):
            raise SourceError(f"esplora address body has no chain_stats: {body!r}")
        try:
            return AddressStats(
                funded_txo_sum=_stats_field(chain_stats, "funded_txo_sum"),
                spent_txo_sum=_stats_field(chain_stats, "spent_txo_sum"),
                tx_count=_stats_field(chain_stats, "tx_count"),
            )
        except ValidationError as exc:
            raise SourceError(
                f"inconsistent esplora chain_stats: {chain_stats!r}"
            ) from exc

    def utxos(self, address: str) -> tuple[Utxo, ...]:
        """``GET {base}/address/{address}/utxo`` → Utxos, response order.

        Rows are ``{txid, vout, value, status: {confirmed}}``. A
        malformed row raises ``SourceError``. UTXOs are money, strict.
        """
        body = self._get(f"/address/{address}/utxo")
        if not isinstance(body, list):
            raise SourceError(f"esplora utxo body is not a list: {body!r}")
        return tuple(_parse_utxo(row) for row in body)

    def _get(self, path: str) -> object:
        """One GET on ``base_url + path`` → the parsed JSON body.

        Transport failure, non-2xx status, and a non-JSON body all raise
        ``SourceError``. No retry, no rate limiting, no query params.
        """
        try:
            response = self._client.get(f"{self._base_url}{path}")
        except httpx.HTTPError as exc:
            raise SourceError(f"esplora request failed: {exc!r}") from exc
        if not 200 <= response.status_code < 300:
            raise SourceError(f"esplora HTTP {response.status_code} for {path}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(f"esplora returned a non-JSON body for {path}") from exc


def scan(
    esplora: Esplora,
    derive: Callable[[int, int, int], Sequence[str]],
    gap: int = 20,
) -> BitcoinScanResult:
    """BIP44 gap-limit scan over ``derive`` (DECISIONS "Gap-limit scan").

    ``derive(chain, start, count)`` returns the addresses for indices
    ``start .. start + count - 1``, exactly ``count`` of them: a batch
    of any other length is a broken counterparty and raises
    ``ValidationError``. ``ValidationError`` if ``gap < 1``,
    BEFORE any HTTP. For chain 0 then 1: derive batches of exactly
    ``gap`` addresses; query ascending via ``address_stats``; an address
    is used iff ``chain_stats.tx_count > 0``; the consecutive-unused run
    counter resets on every used address; the chain STOPS immediately
    when the run reaches ``gap`` (mid-batch: later batch addresses are
    never queried). Every used address, including balance 0, swept,
    yields an ``AddressBalance(address, chain, index, confirmed_sats,
    tx_count)``. Output is ordered chain 0 then 1, index ascending.
    Never calls ``/utxo``; never sees an xpub.
    """
    if isinstance(gap, bool) or not isinstance(gap, int):
        raise ValidationError(f"gap must be an int, got {type(gap).__name__}")
    if gap < 1:
        raise ValidationError(f"gap must be >= 1, got {gap}")
    found: list[AddressBalance] = []
    for chain in _CHAINS:
        found.extend(_scan_chain(esplora, derive, gap, chain))
    return BitcoinScanResult(addresses=tuple(found))


def _scan_chain(
    esplora: Esplora,
    derive: Callable[[int, int, int], Sequence[str]],
    gap: int,
    chain: int,
) -> list[AddressBalance]:
    """One chain's used addresses, index ascending, stopping at ``gap``.

    The stop is mid-batch: the moment the consecutive-unused run reaches
    ``gap`` the remaining derived addresses are never queried.

    Indices are assigned positionally (``start + offset``), so a batch
    that is not exactly ``gap`` long would silently skip, duplicate, or
    truncate indices: ``derive`` breaking its own contract raises
    ``ValidationError`` rather than producing a wrong balance.
    """
    used: list[AddressBalance] = []
    unused_run = 0
    start = 0
    while unused_run < gap:
        addresses = derive(chain, start, gap)
        if len(addresses) != gap:
            raise ValidationError(
                f"derive(chain={chain}, start={start}, count={gap}) returned "
                f"{len(addresses)} addresses, expected {gap}"
            )
        for offset, address in enumerate(addresses):
            stats = esplora.address_stats(address)
            if stats.tx_count == 0:
                unused_run += 1
                if unused_run >= gap:
                    return used
                continue
            unused_run = 0  # a used address resets the run (DECISIONS)
            used.append(
                AddressBalance(
                    address=address,
                    chain=chain,
                    index=start + offset,
                    balance_sats=stats.confirmed_sats,
                    tx_count=stats.tx_count,
                )
            )
        start += gap
    return used
