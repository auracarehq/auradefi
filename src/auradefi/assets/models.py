"""Asset and Implementation models; the pinned deterministic asset id
(SPEC §4.2, rule #3; docs/internal/DECISIONS.md).

A chain-agnostic Asset carries one Implementation per chain. ``decimals``
lives ON the implementation, never on the asset. USDC is 6 everywhere
today, but bridged assets genuinely differ, and you cannot format an
amount without knowing which chain it is on.

The asset id is a PERMANENTLY STABLE wire contract::

    "ast_" + sha256("\\n".join(sorted(canonical_caip19s)).encode()).hexdigest()[:16]

Changing it is a breaking change to persisted data; the golden vectors in
tests/assets/test_models.py are hardcoded literals for exactly that reason.
Stdlib only; may import money/ and chains/ only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from auradefi.assets.caip import canonical_caip19, format_caip19, parse_caip19
from auradefi.errors import ValidationError


class AssetClass(StrEnum):
    """One enum spanning families (SPEC §4.2): exactly this list."""

    NATIVE = "native"
    TOKEN = "token"
    STABLECOIN = "stablecoin"
    LP_TOKEN = "lp_token"
    RECEIPT_TOKEN = "receipt_token"
    DEBT_TOKEN = "debt_token"
    VAULT_SHARE = "vault_share"
    NFT = "nft"
    WRAPPED = "wrapped"
    DERIVATIVE = "derivative"
    PERP = "perp"


@dataclass(frozen=True, slots=True)
class AssetFlags:
    """Additive quality marks. Detection is additive, never destructive
    (SPEC §4.2, rotki's scar), so these are plain booleans that default
    to the unmarked state."""

    spam_suspected: bool = False
    verified: bool = False


@dataclass(frozen=True, slots=True)
class Implementation:
    """One asset on one chain. ``decimals`` lives here (SPEC §4.2).

    Raises:
        ValidationError: if ``decimals`` is negative.
    """

    caip19: str
    chain_id: str
    decimals: int

    def __post_init__(self) -> None:
        if self.decimals < 0:
            raise ValidationError(f"decimals must be >= 0, got {self.decimals}")


@dataclass(frozen=True, slots=True)
class Asset:
    """Chain-agnostic asset: "USDC", not "USDC on Ethereum".

    ``make_asset`` is the validating constructor that computes ``id``;
    the dataclass itself performs no validation. ``external_ids`` is a
    tuple of ``(provider, external_id)`` pairs (hashable, frozen).
    """

    id: str
    symbol: str
    name: str
    icon: str | None
    implementations: tuple[Implementation, ...]
    external_ids: tuple[tuple[str, str], ...]
    asset_class: AssetClass
    flags: AssetFlags


def asset_id(caip19s: Iterable[str]) -> str:
    """The pinned deterministic asset id (docs/internal/DECISIONS.md, rule #3):
    hash over the sorted, deduplicated canonical CAIP-19 set; empty
    input rejected.

    Each CAIP-19 is canonicalized first (EVM addresses lowercased, Solana
    base58 case preserved), then the set is DEDUPLICATED and sorted,
    joined with ``"\\n"``, sha256-hashed, and the first 16 hex digits
    prefixed with ``"ast_"``. Input order therefore never matters, case
    variants of the same EVM address yield the SAME id, and a repeated
    entry never shifts the hash. ``make_asset`` already rejects
    duplicates and empty input, so no id built through it can change.

    Raises:
        ValidationError: if ``caip19s`` is empty. Hashing nothing would
            mint one well-formed id for "no asset at all".
        CaipParseError: if any entry is not a parseable CAIP-19.
    """
    canonical = sorted({canonical_caip19(entry) for entry in caip19s})
    if not canonical:
        raise ValidationError("asset_id needs at least one CAIP-19")
    digest = hashlib.sha256("\n".join(canonical).encode()).hexdigest()
    return "ast_" + digest[:16]


def make_asset(
    *,
    symbol: str,
    name: str,
    implementations: Sequence[Implementation],
    asset_class: AssetClass,
    icon: str | None = None,
    external_ids: Sequence[tuple[str, str]] = (),
    flags: AssetFlags | None = None,
) -> Asset:
    """Build an :class:`Asset`, computing ``id`` via :func:`asset_id`
    over the implementations' CAIP-19s.

    ``implementations`` and ``external_ids`` are stored as tuples in the
    given order; ``flags`` defaults to ``AssetFlags()``.

    Raises:
        ValidationError: if ``implementations`` is empty, two
            implementations share a CAIP-19 (compared canonically, two
            case variants of one EVM address are duplicates), or an
            implementation's ``chain_id`` contradicts the chain embedded
            in its own ``caip19``, the registry indexes by the CAIP-19
            chain, so a mismatch would silently disagree with lookups.
        CaipParseError: if an implementation CAIP-19 cannot be parsed.
    """
    impls = tuple(implementations)
    if not impls:
        raise ValidationError("an asset needs at least one implementation")
    parsed = [parse_caip19(impl.caip19) for impl in impls]
    for impl, leg in zip(impls, parsed, strict=True):
        if impl.chain_id != leg.chain_id:
            raise ValidationError(
                f"implementation chain_id {impl.chain_id!r} contradicts the "
                f"chain {leg.chain_id!r} in its CAIP-19 {impl.caip19!r}"
            )
    canonical = [format_caip19(leg) for leg in parsed]
    if len(set(canonical)) != len(canonical):
        duplicates = sorted({c for c in canonical if canonical.count(c) > 1})
        raise ValidationError(f"duplicate implementation CAIP-19s: {duplicates}")
    return Asset(
        id=asset_id(canonical),
        symbol=symbol,
        name=name,
        icon=icon,
        implementations=impls,
        external_ids=tuple(external_ids),
        asset_class=asset_class,
        flags=flags if flags is not None else AssetFlags(),
    )
