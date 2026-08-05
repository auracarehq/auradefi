"""Asset registry: addressable both ways, on every lookup (SPEC §4.2):
by deterministic asset id, by CAIP-19, or by (chain, address).

Lookup keys are canonical CAIP-19 strings, so EVM addresses are matched
case-insensitively while Solana token references stay case-sensitive.
Only address-shaped namespaces (``erc20``, ``token``) enter the
(chain, address) index. A ``slip44`` reference is a coin type, not an
on-chain address, so natives are reachable by id and CAIP-19 only.
Registration is additive and never destructive: a rejected conflict,
on either index, leaves the registry exactly as it was. stdlib only;
may import money/ and chains/ only.
"""

from __future__ import annotations

import re

from auradefi.assets.caip import canonical_caip19, format_caip19, parse_caip19
from auradefi.assets.models import Asset
from auradefi.errors import AssetConflictError, UnknownAssetError

# An address canonicalizes exactly like an erc20 reference: EVM addresses
# lowercase, everything else (base58 can never start with "0x") untouched.
_EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")

# Namespaces whose reference IS an on-chain address. slip44 is excluded:
# its reference is a registry coin type, and indexing it as an address
# would let e.g. slip44:144 collide with token:144 ("144" is valid base58).
_ADDRESS_NAMESPACES = frozenset({"erc20", "token"})


def _canonical_address(address: str) -> str:
    if _EVM_ADDRESS.fullmatch(address) is not None:
        return address.lower()
    return address


class AssetRegistry:
    """In-memory registry of Assets. Starts empty; instances are
    independent: registering into one never affects another."""

    def __init__(self) -> None:
        self._by_id: dict[str, Asset] = {}
        self._by_caip19: dict[str, str] = {}  # canonical CAIP-19 -> asset id
        self._by_chain_address: dict[tuple[str, str], str] = {}

    def register(self, asset: Asset) -> None:
        """Register ``asset`` under its id and each implementation CAIP-19.

        Re-registering an asset identical to an existing entry is a
        no-op. Rejection never mutates: after a conflict the registry is
        unchanged.

        Raises:
            AssetConflictError: if any implementation CAIP-19, or its
                (chain, address) key, for address-shaped namespaces, is
                already bound to a different asset id, or if ``asset.id``
                is already registered with any differing field.
        """
        existing = self._by_id.get(asset.id)
        if existing is not None:
            if existing == asset:
                return
            raise AssetConflictError(
                f"asset id {asset.id!r} already registered with differing fields"
            )
        legs = [parse_caip19(impl.caip19) for impl in asset.implementations]
        for parsed in legs:
            bound = self._by_caip19.get(format_caip19(parsed))
            if bound is not None and bound != asset.id:
                raise AssetConflictError(
                    f"{format_caip19(parsed)!r} is already bound to asset {bound!r}"
                )
            if parsed.namespace in _ADDRESS_NAMESPACES:
                address_key = (parsed.chain_id, parsed.reference)
                bound = self._by_chain_address.get(address_key)
                if bound is not None and bound != asset.id:
                    raise AssetConflictError(
                        f"address {parsed.reference!r} on {parsed.chain_id!r}"
                        f" is already bound to asset {bound!r}"
                    )
        # All legs validated: the writes below can no longer fail partway.
        self._by_id[asset.id] = asset
        for parsed in legs:
            self._by_caip19[format_caip19(parsed)] = asset.id
            if parsed.namespace in _ADDRESS_NAMESPACES:
                self._by_chain_address[(parsed.chain_id, parsed.reference)] = asset.id

    def get_by_id(self, asset_id: str) -> Asset:
        """Return the asset registered under ``asset_id``.

        Raises:
            UnknownAssetError: if ``asset_id`` is not registered.
        """
        try:
            return self._by_id[asset_id]
        except KeyError:
            raise UnknownAssetError(f"unknown asset id {asset_id!r}") from None

    def get_by_caip19(self, caip19: str) -> Asset:
        """Return the asset holding ``caip19`` as an implementation.

        Canonicalizes ``caip19`` first, so any case variant of an EVM
        address finds the asset; a wrong-case Solana reference does not.

        Raises:
            CaipParseError: if ``caip19`` cannot be parsed at all.
            UnknownAssetError: if no registered asset holds it.
        """
        canonical = canonical_caip19(caip19)
        try:
            return self._by_id[self._by_caip19[canonical]]
        except KeyError:
            raise UnknownAssetError(f"no asset holds {canonical!r}") from None

    def get_by_chain_address(self, chain_id: str, address: str) -> Asset:
        """Return the asset with an on-chain ``address`` on ``chain_id``.

        The other half of both-ways addressing (SPEC §4.2): EVM
        addresses match case-insensitively; Solana token addresses match
        case-sensitively. Only address-shaped namespaces (``erc20``,
        ``token``) are reachable here. Slip44 coin types are not
        addresses and never enter this index.

        Raises:
            UnknownAssetError: if nothing matches.
        """
        key = (chain_id, _canonical_address(address))
        try:
            return self._by_id[self._by_chain_address[key]]
        except KeyError:
            raise UnknownAssetError(
                f"no asset at address {address!r} on chain {chain_id!r}"
            ) from None

    def assets(self) -> tuple[Asset, ...]:
        """All registered assets as a tuple sorted by ``id``.
        Deterministic across runs and registration order."""
        return tuple(sorted(self._by_id.values(), key=lambda asset: asset.id))
