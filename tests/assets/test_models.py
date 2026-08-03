"""Asset/Implementation models and the PINNED asset id (rule #3).

The ast_… literals below were derived independently via
``python3 -c`` from the algorithm pinned in docs/DECISIONS.md:

    "ast_" + sha256("\\n".join(sorted(canonical_caip19s)).encode()).hexdigest()[:16]

They are hardcoded on purpose: the id is a permanently stable wire
contract, and a stability contract is a literal, not a call to the
function under test.
"""

from __future__ import annotations

import dataclasses

import pytest

from auradefi.assets.models import (
    Asset,
    AssetClass,
    AssetFlags,
    Implementation,
    asset_id,
    make_asset,
)
from auradefi.errors import CaipParseError, ValidationError

SOL_CHAIN = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
USDC_ETH_MIXED = "eip155:1/erc20:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_ETH_UPPER = "eip155:1/erc20:0xA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
USDC_ETH = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_POL = "eip155:137/erc20:0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
USDC_SOL = f"{SOL_CHAIN}/token:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ETH_NATIVE = "eip155:1/slip44:60"

# Golden vectors — derived with python3 -c hashlib, hardcoded (rule #3).
AST_USDC_ETH = "ast_a798531c7b37abe1"  # sha256(USDC_ETH)[:16]
AST_USDC_TRI = "ast_99f26454cb92a351"  # sha256(USDC_ETH \n USDC_POL \n USDC_SOL)
AST_ETH = "ast_ed0bcc482c2859ce"  # sha256("eip155:1/slip44:60")
AST_USDC_SOL = "ast_a7809b605100f560"  # sha256(USDC_SOL) — base58 case kept


# --- AssetClass: exactly the SPEC §4.2 list ----------------------------------


def test_asset_class_members_are_exactly_the_spec_list_in_order():
    assert [(m.name, m.value) for m in AssetClass] == [
        ("NATIVE", "native"),
        ("TOKEN", "token"),
        ("STABLECOIN", "stablecoin"),
        ("LP_TOKEN", "lp_token"),
        ("RECEIPT_TOKEN", "receipt_token"),
        ("DEBT_TOKEN", "debt_token"),
        ("VAULT_SHARE", "vault_share"),
        ("NFT", "nft"),
        ("WRAPPED", "wrapped"),
        ("DERIVATIVE", "derivative"),
        ("PERP", "perp"),
    ]


def test_asset_class_is_a_str_enum():
    assert isinstance(AssetClass.NATIVE, str)
    assert AssetClass.STABLECOIN == "stablecoin"
    assert f"{AssetClass.LP_TOKEN}" == "lp_token"


# --- AssetFlags ---------------------------------------------------------------


def test_asset_flags_default_to_unmarked():
    flags = AssetFlags()
    assert flags.spam_suspected is False
    assert flags.verified is False


def test_asset_flags_frozen_and_value_equal():
    flags = AssetFlags(spam_suspected=True)
    assert flags == AssetFlags(spam_suspected=True, verified=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        flags.verified = True  # type: ignore[misc]


# --- Implementation -------------------------------------------------------------


def _usdc_eth_impl() -> Implementation:
    return Implementation(caip19=USDC_ETH, chain_id="eip155:1", decimals=6)


def test_implementation_fields():
    impl = _usdc_eth_impl()
    assert impl.caip19 == USDC_ETH
    assert impl.chain_id == "eip155:1"
    assert impl.decimals == 6


def test_implementation_is_frozen():
    impl = _usdc_eth_impl()
    with pytest.raises(dataclasses.FrozenInstanceError):
        impl.decimals = 18  # type: ignore[misc]


def test_implementation_zero_decimals_is_valid():
    impl = Implementation(caip19=ETH_NATIVE, chain_id="eip155:1", decimals=0)
    assert impl.decimals == 0


@pytest.mark.parametrize("decimals", [-1, -18, -(10**77)])
def test_implementation_negative_decimals_raises(decimals):
    with pytest.raises(ValidationError):
        Implementation(caip19=USDC_ETH, chain_id="eip155:1", decimals=decimals)


# --- Asset (dataclass shape only; make_asset is the validating path) -----------


def _bare_asset() -> Asset:
    return Asset(
        id="ast_0000000000000000",
        symbol="TST",
        name="Test",
        icon=None,
        implementations=(),
        external_ids=(),
        asset_class=AssetClass.TOKEN,
        flags=AssetFlags(),
    )


def test_asset_is_frozen():
    asset = _bare_asset()
    with pytest.raises(dataclasses.FrozenInstanceError):
        asset.symbol = "XXX"  # type: ignore[misc]


def test_asset_holds_tuples_not_lists():
    asset = _bare_asset()
    assert isinstance(asset.implementations, tuple)
    assert isinstance(asset.external_ids, tuple)


# --- asset_id: the pinned golden vectors ----------------------------------------


def test_asset_id_single_implementation_golden_vector():
    # Acceptance vector: the exact literal, derived offline via hashlib.
    assert asset_id(["eip155:1/erc20:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"]) == AST_USDC_ETH


@pytest.mark.parametrize("variant", [USDC_ETH, USDC_ETH_MIXED, USDC_ETH_UPPER])
def test_asset_id_case_variants_of_same_evm_address_agree(variant):
    assert asset_id([variant]) == AST_USDC_ETH


def test_asset_id_multi_implementation_golden_vector():
    assert asset_id([USDC_ETH_MIXED, USDC_POL, USDC_SOL]) == AST_USDC_TRI


def test_asset_id_is_order_independent():
    assert asset_id([USDC_SOL, USDC_POL, USDC_ETH]) == AST_USDC_TRI
    assert asset_id([USDC_POL, USDC_ETH, USDC_SOL]) == AST_USDC_TRI


def test_asset_id_native_golden_vector():
    assert asset_id([ETH_NATIVE]) == AST_ETH


def test_asset_id_solana_base58_case_is_significant():
    assert asset_id([USDC_SOL]) == AST_USDC_SOL
    assert asset_id([USDC_SOL.lower()]) != AST_USDC_SOL  # lowercasing would corrupt ids


def test_asset_id_shape():
    value = asset_id([ETH_NATIVE])
    assert value.startswith("ast_")
    assert len(value) == 20  # "ast_" + 16 hex chars
    assert set(value[4:]) <= set("0123456789abcdef")


def test_asset_id_accepts_any_iterable():
    assert asset_id(iter([USDC_ETH])) == AST_USDC_ETH


def test_asset_id_deduplicates_case_variants_of_one_address():
    # Two case variants canonicalize to ONE caip19: the id equals the
    # singleton's pinned literal, not a hash of the entry twice.
    assert asset_id([USDC_ETH, USDC_ETH_UPPER]) == AST_USDC_ETH


def test_asset_id_empty_input_raises():
    # Hashing nothing would mint one well-formed id for "no asset at all".
    with pytest.raises(ValidationError):
        asset_id([])


def test_asset_id_malformed_caip19_raises():
    with pytest.raises(CaipParseError):
        asset_id(["garbage"])


# --- make_asset -------------------------------------------------------------------


def _tri_impls() -> tuple[Implementation, ...]:
    return (
        Implementation(caip19=USDC_ETH_MIXED, chain_id="eip155:1", decimals=6),
        Implementation(caip19=USDC_POL, chain_id="eip155:137", decimals=6),
        Implementation(caip19=USDC_SOL, chain_id=SOL_CHAIN, decimals=6),
    )


def test_make_asset_computes_the_pinned_id():
    asset = make_asset(
        symbol="USDC",
        name="USD Coin",
        implementations=_tri_impls(),
        asset_class=AssetClass.STABLECOIN,
    )
    assert asset.id == AST_USDC_TRI  # mixed-case input, canonical hash


def test_make_asset_populates_fields_and_defaults():
    asset = make_asset(
        symbol="USDC",
        name="USD Coin",
        implementations=_tri_impls(),
        asset_class=AssetClass.STABLECOIN,
        external_ids=[("coingecko", "usd-coin"), ("cmc", "3408")],
    )
    assert asset.symbol == "USDC"
    assert asset.name == "USD Coin"
    assert asset.icon is None
    assert asset.implementations == _tri_impls()
    assert asset.external_ids == (("coingecko", "usd-coin"), ("cmc", "3408"))
    assert asset.asset_class is AssetClass.STABLECOIN
    assert asset.flags == AssetFlags()


def test_make_asset_empty_implementations_raises():
    with pytest.raises(ValidationError):
        make_asset(
            symbol="X", name="X", implementations=(), asset_class=AssetClass.TOKEN
        )


def test_make_asset_duplicate_caip19_raises():
    impl = Implementation(caip19=USDC_ETH, chain_id="eip155:1", decimals=6)
    with pytest.raises(ValidationError):
        make_asset(
            symbol="USDC",
            name="USD Coin",
            implementations=(impl, impl),
            asset_class=AssetClass.STABLECOIN,
        )


def test_make_asset_chain_id_contradicting_caip19_chain_raises():
    # The registry indexes by the CAIP-19 chain; a contradicting
    # chain_id would silently disagree with lookups.
    with pytest.raises(ValidationError):
        make_asset(
            symbol="USDC",
            name="USD Coin",
            implementations=(
                Implementation(caip19=USDC_ETH, chain_id="eip155:137", decimals=6),
            ),
            asset_class=AssetClass.STABLECOIN,
        )


def test_make_asset_case_variant_duplicate_caip19_raises():
    # Two case variants of one EVM address are the SAME canonical caip19.
    with pytest.raises(ValidationError):
        make_asset(
            symbol="USDC",
            name="USD Coin",
            implementations=(
                Implementation(caip19=USDC_ETH, chain_id="eip155:1", decimals=6),
                Implementation(caip19=USDC_ETH_UPPER, chain_id="eip155:1", decimals=6),
            ),
            asset_class=AssetClass.STABLECOIN,
        )
