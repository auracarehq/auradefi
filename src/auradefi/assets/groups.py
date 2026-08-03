"""Asset groups with a single fallback (SPEC §4.2 — OneBalance's ob:usdc).

One "USDC" row across N chains, with an explicit ``single`` fallback
bucket so NOTHING falls out of the model: every asset the caller passes
lands in exactly one group. Aggregation is only sound when every
implementation shares one ``decimals`` value, so an explicit group whose
members mix decimals is rejected server-side (Zerion punts this to the
client; we do not).

The group id is a PERMANENTLY STABLE wire contract, same recipe as the
pinned asset id (docs/DECISIONS.md)::

    "grp_" + sha256("\\n".join(sorted(asset_ids)).encode()).hexdigest()[:16]

Input order never matters. The golden vectors in
tests/assets/test_groups.py are hardcoded literals for exactly that
reason. stdlib only; may import money/ and chains/ only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from auradefi.assets.models import Asset
from auradefi.errors import (
    DecimalsMismatchError,
    UnknownAssetError,
    ValidationError,
)


class GroupKind(StrEnum):
    """How a group came to exist — exactly these two (SPEC §4.2)."""

    GROUP = "group"
    SINGLE = "single"


@dataclass(frozen=True, slots=True)
class AssetGroup:
    """One aggregation bucket. ``asset_ids`` is stored sorted ascending
    (the same order the id hashes), so equal groups are equal values.

    ``group_assets`` is the validating constructor; the dataclass itself
    performs no validation.
    """

    id: str
    symbol: str
    kind: GroupKind
    asset_ids: tuple[str, ...]


def group_id(asset_ids: Iterable[str]) -> str:
    """The pinned deterministic group id (see module docstring).

    Asset ids are sorted, joined with ``"\\n"``, sha256-hashed, and the
    first 16 hex digits prefixed with ``"grp_"``. Input order therefore
    never matters.
    """
    digest = hashlib.sha256("\n".join(sorted(asset_ids)).encode()).hexdigest()
    return "grp_" + digest[:16]


def group_assets(
    assets: Sequence[Asset],
    explicit: Mapping[str, Sequence[str]] | None = None,
) -> tuple[AssetGroup, ...]:
    """Partition ``assets`` into explicit groups plus SINGLE fallbacks.

    ``explicit`` maps a group symbol to the member asset ids; every such
    group has ``kind=GroupKind.GROUP`` (even with one member). Every
    asset NOT claimed by an explicit group lands in its own
    ``GroupKind.SINGLE`` group carrying that asset's symbol — nothing
    falls out of the model. ``explicit=None`` and ``explicit={}`` are
    equivalent.

    Membership is set-shaped: repeating a member id inside one explicit
    group is idempotent (the id and ``asset_ids`` are functions of the
    member SET), and repeating an identical ``Asset`` value in
    ``assets`` yields one group, not two — so summing over the output
    never double-counts.

    Deterministic: the output tuple is sorted by group id, and each
    group's ``asset_ids`` are sorted ascending, regardless of input
    order.

    Raises:
        UnknownAssetError: if an explicit member id is not among
            ``assets``.
        ValidationError: if an explicit group has ZERO member ids (a
            group that aggregates nothing is meaningless, and two such
            groups would collide on one id), if one asset id is claimed
            by MORE THAN ONE explicit group (it must land in exactly
            one), or if ``assets`` contains two UNEQUAL ``Asset`` values
            sharing one id (which asset wins would depend on input
            order).
        DecimalsMismatchError: if the implementations across ALL members
            of one explicit group do not share a single ``decimals``
            value (aggregation would be meaningless). SINGLE fallback
            groups are exempt — they aggregate nothing.
    """
    by_id = _index_by_id(assets)
    groups: list[AssetGroup] = []
    claimed: set[str] = set()
    for symbol, member_ids in (explicit or {}).items():
        if not member_ids:
            raise ValidationError(
                f"explicit group {symbol!r} has no member ids; a group "
                "that aggregates nothing is meaningless"
            )
        members = tuple(
            _resolve(member_id, by_id) for member_id in dict.fromkeys(member_ids)
        )
        _require_one_decimals(symbol, members)
        for asset in members:
            if asset.id in claimed:
                raise ValidationError(
                    f"asset {asset.id!r} is claimed by more than one explicit "
                    f"group (second claim: {symbol!r}); every asset must land "
                    "in exactly one group"
                )
            claimed.add(asset.id)
        sorted_ids = tuple(sorted(asset.id for asset in members))
        groups.append(
            AssetGroup(
                id=group_id(sorted_ids),
                symbol=symbol,
                kind=GroupKind.GROUP,
                asset_ids=sorted_ids,
            )
        )
    for asset in by_id.values():
        if asset.id in claimed:
            continue
        groups.append(
            AssetGroup(
                id=group_id((asset.id,)),
                symbol=asset.symbol,
                kind=GroupKind.SINGLE,
                asset_ids=(asset.id,),
            )
        )
    return tuple(sorted(groups, key=lambda grp: grp.id))


def _index_by_id(assets: Sequence[Asset]) -> dict[str, Asset]:
    """Index assets by id, collapsing identical repeats.

    Raises:
        ValidationError: if two UNEQUAL assets share one id — resolving
            that silently would make the output input-order dependent.
    """
    by_id: dict[str, Asset] = {}
    for asset in assets:
        existing = by_id.setdefault(asset.id, asset)
        if existing != asset:
            raise ValidationError(
                f"two different assets share id {asset.id!r}; "
                "cannot group an ambiguous input"
            )
    return by_id


def _resolve(member_id: str, by_id: Mapping[str, Asset]) -> Asset:
    """Look up one explicit member id.

    Raises:
        UnknownAssetError: if the id is not among the passed assets.
    """
    asset = by_id.get(member_id)
    if asset is None:
        raise UnknownAssetError(
            f"explicit group member {member_id!r} is not among the assets"
        )
    return asset


def _require_one_decimals(symbol: str, members: Sequence[Asset]) -> None:
    """Enforce the aggregation law: one ``decimals`` value across ALL
    implementations of ALL members of an explicit group.

    Raises:
        DecimalsMismatchError: if the members mix decimals.
    """
    decimals = {impl.decimals for asset in members for impl in asset.implementations}
    if len(decimals) > 1:
        raise DecimalsMismatchError(
            f"explicit group {symbol!r} mixes decimals {sorted(decimals)}; "
            "aggregation requires a single value"
        )
