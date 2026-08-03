"""Asset groups with single fallback (SPEC §4.2) — golden-vector tests.

Every ast_… and grp_… literal below was derived independently via
``python3 -c`` from the algorithms pinned in docs/DECISIONS.md::

    "ast_" + sha256("\\n".join(sorted(canonical_caip19s)).encode()).hexdigest()[:16]
    "grp_" + sha256("\\n".join(sorted(asset_ids)).encode()).hexdigest()[:16]

They are hardcoded on purpose: the group id is a permanently stable wire
contract, and a stability contract is a literal, not a call to the
function under test. (Cross-check: AST_USDC_ETH and AST_ETH match the
vectors already pinned in tests/assets/test_models.py.)
"""

from __future__ import annotations

import dataclasses

import pytest

from auradefi.assets.groups import AssetGroup, GroupKind, group_assets, group_id
from auradefi.assets.models import AssetClass, Implementation, make_asset
from auradefi.errors import (
    DecimalsMismatchError,
    UnknownAssetError,
    ValidationError,
)

USDC_ETH = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_POL = "eip155:137/erc20:0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
ETH_NATIVE = "eip155:1/slip44:60"
WBTC_ETH = "eip155:1/erc20:0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
BRIDGED_ETH = "eip155:1/erc20:0x0000000000000000000000000000000000000042"
BRIDGED_POL = "eip155:137/erc20:0x0000000000000000000000000000000000000042"

# Golden vectors — derived with python3 -c hashlib, hardcoded (rule #3).
AST_USDC_ETH = "ast_a798531c7b37abe1"  # sha256(USDC_ETH) — matches test_models.py
AST_USDC_POL = "ast_1497f0dd93b859f6"  # sha256(USDC_POL)
AST_ETH = "ast_ed0bcc482c2859ce"  # sha256(ETH_NATIVE) — matches test_models.py
AST_WBTC = "ast_6a0a857e5d8dd092"  # sha256(WBTC_ETH)
AST_MIXED = "ast_9de52c8da87fa1b2"  # sha256(BRIDGED_ETH \n BRIDGED_POL)

GRP_A = "grp_ca978112ca1bbdca"  # sha256("a")
GRP_AB = "grp_7e18f737311b2dc3"  # sha256("a\nb")
GRP_USDC_PAIR = "grp_9b8a4a63c988f8ae"  # sha256(AST_USDC_POL \n AST_USDC_ETH sorted)
GRP_ETH_SOLO = "grp_27328812af10fb83"  # sha256(AST_ETH)
GRP_WBTC_SOLO = "grp_a286fdb84bf61fb6"  # sha256(AST_WBTC)
GRP_MIXED_SOLO = "grp_2960bb6a1accb909"  # sha256(AST_MIXED)
GRP_USDC_ETH_SOLO = "grp_576a52847352b85a"  # sha256(AST_USDC_ETH)


def _asset(symbol, name, impls, asset_class=AssetClass.TOKEN):
    return make_asset(
        symbol=symbol, name=name, implementations=impls, asset_class=asset_class
    )


def usdc_eth_asset():
    return _asset(
        "USDC",
        "USD Coin (Ethereum)",
        (Implementation(caip19=USDC_ETH, chain_id="eip155:1", decimals=6),),
        AssetClass.STABLECOIN,
    )


def usdc_pol_asset():
    return _asset(
        "USDC",
        "USD Coin (Polygon)",
        (Implementation(caip19=USDC_POL, chain_id="eip155:137", decimals=6),),
        AssetClass.STABLECOIN,
    )


def eth_asset():
    return _asset(
        "ETH",
        "Ether",
        (Implementation(caip19=ETH_NATIVE, chain_id="eip155:1", decimals=18),),
        AssetClass.NATIVE,
    )


def wbtc_asset():
    return _asset(
        "WBTC",
        "Wrapped Bitcoin",
        (Implementation(caip19=WBTC_ETH, chain_id="eip155:1", decimals=8),),
        AssetClass.WRAPPED,
    )


def mixed_decimals_asset():
    """One asset whose own implementations disagree on decimals (a real
    bridged-asset situation — SPEC §4.2 says decimals live per-impl)."""
    return _asset(
        "BRG",
        "Bridged Mixed",
        (
            Implementation(caip19=BRIDGED_ETH, chain_id="eip155:1", decimals=18),
            Implementation(caip19=BRIDGED_POL, chain_id="eip155:137", decimals=6),
        ),
    )


# --- fixture sanity: the asset ids the grp_ vectors were derived from ---------


def test_fixture_asset_ids_match_the_pinned_vectors():
    assert usdc_eth_asset().id == AST_USDC_ETH
    assert usdc_pol_asset().id == AST_USDC_POL
    assert eth_asset().id == AST_ETH
    assert wbtc_asset().id == AST_WBTC
    assert mixed_decimals_asset().id == AST_MIXED


# --- GroupKind: exactly two members --------------------------------------------


def test_group_kind_members_are_exactly_group_and_single_in_order():
    assert [(m.name, m.value) for m in GroupKind] == [
        ("GROUP", "group"),
        ("SINGLE", "single"),
    ]


def test_group_kind_is_a_str_enum():
    assert isinstance(GroupKind.SINGLE, str)
    assert GroupKind.GROUP == "group"
    assert f"{GroupKind.SINGLE}" == "single"


# --- AssetGroup shape -----------------------------------------------------------


def test_asset_group_fields_and_frozen():
    grp = AssetGroup(
        id=GRP_USDC_PAIR,
        symbol="USDC",
        kind=GroupKind.GROUP,
        asset_ids=(AST_USDC_POL, AST_USDC_ETH),
    )
    assert grp.id == GRP_USDC_PAIR
    assert grp.symbol == "USDC"
    assert grp.kind is GroupKind.GROUP
    assert grp.asset_ids == (AST_USDC_POL, AST_USDC_ETH)
    with pytest.raises(dataclasses.FrozenInstanceError):
        grp.symbol = "usdc"  # type: ignore[misc]


# --- group_id: the pinned wire contract ------------------------------------------


def test_group_id_golden_vectors():
    assert group_id(["a"]) == GRP_A
    assert group_id(["a", "b"]) == GRP_AB


def test_group_id_is_order_independent():
    assert group_id(["b", "a"]) == GRP_AB
    assert group_id((AST_USDC_POL, AST_USDC_ETH)) == GRP_USDC_PAIR
    assert group_id((AST_USDC_ETH, AST_USDC_POL)) == GRP_USDC_PAIR


# --- explicit groups: decimals law -----------------------------------------------


def test_explicit_group_same_decimals_across_chains_succeeds():
    usdc_e, usdc_p = usdc_eth_asset(), usdc_pol_asset()
    groups = group_assets(
        [usdc_e, usdc_p], explicit={"USDC": [usdc_e.id, usdc_p.id]}
    )
    assert groups == (
        AssetGroup(
            id=GRP_USDC_PAIR,
            symbol="USDC",
            kind=GroupKind.GROUP,
            asset_ids=(AST_USDC_POL, AST_USDC_ETH),  # sorted ascending
        ),
    )


def test_explicit_group_of_eth18_and_usdc6_raises_decimals_mismatch():
    eth, usdc = eth_asset(), usdc_eth_asset()
    with pytest.raises(DecimalsMismatchError):
        group_assets([eth, usdc], explicit={"WAT": [eth.id, usdc.id]})


def test_explicit_group_with_one_internally_mixed_member_raises():
    mixed = mixed_decimals_asset()
    with pytest.raises(DecimalsMismatchError):
        group_assets([mixed], explicit={"BRG": [mixed.id]})


def test_internally_mixed_asset_is_fine_as_a_single_fallback():
    mixed = mixed_decimals_asset()
    groups = group_assets([mixed])
    assert groups == (
        AssetGroup(
            id=GRP_MIXED_SOLO,
            symbol="BRG",
            kind=GroupKind.SINGLE,
            asset_ids=(AST_MIXED,),
        ),
    )


def test_unknown_asset_id_in_explicit_raises():
    usdc = usdc_eth_asset()
    with pytest.raises(UnknownAssetError):
        group_assets(
            [usdc], explicit={"USDC": [usdc.id, "ast_0000000000000000"]}
        )


def test_single_member_explicit_group_is_kind_group():
    wbtc = wbtc_asset()
    groups = group_assets([wbtc], explicit={"WBTC": [wbtc.id]})
    assert groups == (
        AssetGroup(
            id=GRP_WBTC_SOLO,
            symbol="WBTC",
            kind=GroupKind.GROUP,
            asset_ids=(AST_WBTC,),
        ),
    )


# --- membership is set-shaped: exactly-one-group, no double counting -------------


def test_asset_claimed_by_two_explicit_groups_raises():
    # Both members share decimals=6 so the ONLY violation is the double
    # claim — this must not be masked by the decimals law.
    usdc_e, usdc_p = usdc_eth_asset(), usdc_pol_asset()
    with pytest.raises(ValidationError):
        group_assets(
            [usdc_e, usdc_p],
            explicit={"ONE": [usdc_e.id], "TWO": [usdc_e.id, usdc_p.id]},
        )


def test_duplicate_member_ids_within_one_explicit_group_dedupe():
    # ['X', 'X'] is the same member SET as ['X']: the id is the pinned
    # singleton literal, and summing over asset_ids never double-counts.
    usdc = usdc_eth_asset()
    groups = group_assets([usdc], explicit={"USDC": [usdc.id, usdc.id]})
    assert groups == (
        AssetGroup(
            id=GRP_USDC_ETH_SOLO,
            symbol="USDC",
            kind=GroupKind.GROUP,
            asset_ids=(AST_USDC_ETH,),
        ),
    )


def test_identical_asset_repeated_in_assets_collapses_to_one_single_group():
    usdc = usdc_eth_asset()
    groups = group_assets([usdc, usdc])
    assert groups == (
        AssetGroup(
            id=GRP_USDC_ETH_SOLO,
            symbol="USDC",
            kind=GroupKind.SINGLE,
            asset_ids=(AST_USDC_ETH,),
        ),
    )


def test_two_unequal_assets_sharing_an_id_raise():
    usdc = usdc_eth_asset()
    impostor = dataclasses.replace(usdc, name="Not USD Coin")
    with pytest.raises(ValidationError):
        group_assets([usdc, impostor])


def test_explicit_group_with_no_member_ids_raises():
    with pytest.raises(ValidationError):
        group_assets([], explicit={"A": []})
    with pytest.raises(ValidationError):
        group_assets([], explicit={"A": [], "B": []})


# --- the single fallback: nothing falls out of the model -------------------------


def test_ungrouped_assets_each_land_in_their_own_single_group():
    groups = group_assets([eth_asset(), wbtc_asset()])
    assert groups == (
        AssetGroup(
            id=GRP_ETH_SOLO,
            symbol="ETH",
            kind=GroupKind.SINGLE,
            asset_ids=(AST_ETH,),
        ),
        AssetGroup(
            id=GRP_WBTC_SOLO,
            symbol="WBTC",
            kind=GroupKind.SINGLE,
            asset_ids=(AST_WBTC,),
        ),
    )  # already in group-id sorted order: grp_27… < grp_a2…


def test_mixed_explicit_and_fallback_covers_every_asset_exactly_once():
    usdc_e, usdc_p, eth, wbtc = (
        usdc_eth_asset(),
        usdc_pol_asset(),
        eth_asset(),
        wbtc_asset(),
    )
    groups = group_assets(
        [usdc_e, usdc_p, eth, wbtc], explicit={"USDC": [usdc_e.id, usdc_p.id]}
    )
    assert len(groups) == 3
    covered = [aid for grp in groups for aid in grp.asset_ids]
    assert sorted(covered) == sorted([AST_USDC_ETH, AST_USDC_POL, AST_ETH, AST_WBTC])
    assert len(covered) == len(set(covered))
    kinds = {grp.id: grp.kind for grp in groups}
    assert kinds == {
        GRP_USDC_PAIR: GroupKind.GROUP,
        GRP_ETH_SOLO: GroupKind.SINGLE,
        GRP_WBTC_SOLO: GroupKind.SINGLE,
    }


def test_no_assets_means_no_groups():
    assert group_assets([]) == ()
    assert group_assets([], explicit={}) == ()


def test_explicit_none_and_empty_mapping_are_equivalent():
    assets = [eth_asset(), wbtc_asset()]
    assert group_assets(assets) == group_assets(assets, explicit=None)
    assert group_assets(assets) == group_assets(assets, explicit={})


# --- determinism ------------------------------------------------------------------


def test_output_is_sorted_by_group_id():
    groups = group_assets(
        [wbtc_asset(), eth_asset(), usdc_pol_asset(), usdc_eth_asset()],
        explicit={"USDC": [AST_USDC_ETH, AST_USDC_POL]},
    )
    ids = [grp.id for grp in groups]
    assert ids == sorted(ids)
    assert ids == [GRP_ETH_SOLO, GRP_USDC_PAIR, GRP_WBTC_SOLO]


def test_input_order_never_changes_the_output():
    forward = group_assets(
        [usdc_eth_asset(), usdc_pol_asset(), eth_asset()],
        explicit={"USDC": [AST_USDC_ETH, AST_USDC_POL]},
    )
    backward = group_assets(
        [eth_asset(), usdc_pol_asset(), usdc_eth_asset()],
        explicit={"USDC": [AST_USDC_POL, AST_USDC_ETH]},
    )
    assert forward == backward
