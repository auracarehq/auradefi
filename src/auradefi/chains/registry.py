"""Chain registry — CAIP-2 is the only key (SPEC §4.2).

No vendor name lookup anywhere: ``get('ethereum')`` is UnknownChainError by
design, killing the ``eth-mainnet`` / ``ethereum`` / ``1`` translation-table
zoo at the door. stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass

from auradefi.chains import bitcoin, solana
from auradefi.chains.families import ChainFamily
from auradefi.errors import ConflictError, UnknownChainError


@dataclass(frozen=True, slots=True)
class Chain:
    """Immutable descriptor of one chain, keyed by its CAIP-2 identifier.

    ``native_caip19`` is the CAIP-19 of the chain's native asset
    (``eip155:1/slip44:60`` style); ``native_decimals`` travels here because
    you cannot format an amount without knowing the chain (SPEC §4.2).
    """

    caip2: str
    family: ChainFamily
    name: str
    native_caip19: str
    native_symbol: str
    native_decimals: int


# The five Phase 0 seed chains (SPEC §4.2) — wire-format contracts.
_SEEDS: tuple[Chain, ...] = (
    Chain(
        caip2="eip155:1",
        family=ChainFamily.EVM,
        name="Ethereum",
        native_caip19="eip155:1/slip44:60",
        native_symbol="ETH",
        native_decimals=18,
    ),
    Chain(
        caip2="eip155:137",
        family=ChainFamily.EVM,
        name="Polygon",
        native_caip19="eip155:137/slip44:966",
        native_symbol="POL",
        native_decimals=18,
    ),
    Chain(
        caip2="eip155:8453",
        family=ChainFamily.EVM,
        name="Base",
        native_caip19="eip155:8453/slip44:60",
        native_symbol="ETH",
        native_decimals=18,
    ),
    Chain(
        caip2=bitcoin.MAINNET,
        family=ChainFamily.BITCOIN,
        name="Bitcoin",
        native_caip19=f"{bitcoin.MAINNET}/slip44:{bitcoin.SLIP44}",
        native_symbol="BTC",
        native_decimals=8,
    ),
    Chain(
        caip2=solana.MAINNET,
        family=ChainFamily.SOLANA,
        name="Solana",
        native_caip19=f"{solana.MAINNET}/slip44:{solana.SLIP44}",
        native_symbol="SOL",
        native_decimals=9,
    ),
)


class ChainRegistry:
    """Mutable registry of known chains, keyed strictly by CAIP-2.

    Every instance is pre-seeded with exactly five chains: Ethereum
    (eip155:1), Polygon (eip155:137), Base (eip155:8453), Bitcoin mainnet
    (bip122:000000000019d6689c085ae165831e93) and Solana mainnet
    (solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp). Instances are independent —
    registering into one never affects another.
    """

    def __init__(self) -> None:
        self._by_caip2: dict[str, Chain] = {chain.caip2: chain for chain in _SEEDS}

    def get(self, caip2: str) -> Chain:
        """Return the Chain registered under ``caip2``.

        Raises:
            UnknownChainError: if ``caip2`` is not registered — including
                any vendor-name key like ``'ethereum'``.
        """
        try:
            return self._by_caip2[caip2]
        except KeyError:
            raise UnknownChainError(
                f"unknown chain {caip2!r} — CAIP-2 is the only key"
            ) from None

    def register(self, chain: Chain) -> None:
        """Register ``chain`` under its ``caip2``.

        Re-registering a Chain identical to the existing entry is a no-op.

        Raises:
            ConflictError: if ``chain.caip2`` is already registered with
                any differing field.
        """
        existing = self._by_caip2.get(chain.caip2)
        if existing is None:
            self._by_caip2[chain.caip2] = chain
        elif existing != chain:
            raise ConflictError(
                f"{chain.caip2} is already registered with different fields",
                existing_id=existing.caip2,
            )

    def chains(self) -> tuple[Chain, ...]:
        """All registered chains as a tuple sorted by ``caip2``."""
        return tuple(sorted(self._by_caip2.values(), key=lambda chain: chain.caip2))
