"""AssetRegistry: both-ways addressing, idempotent registration,
conflict on rebinding (SPEC §4.2).

Expected ids are the same hardcoded golden literals as test_models.py —
never derived by calling the code under test.
"""

from __future__ import annotations

import dataclasses

import pytest

from auradefi.assets.models import Asset, AssetClass, Implementation, make_asset
from auradefi.assets.registry import AssetRegistry
from auradefi.errors import AssetConflictError, CaipParseError, UnknownAssetError

SOL_CHAIN = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
USDC_ETH = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_ETH_UPPER = "eip155:1/erc20:0xA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
USDC_ETH_MIXED = "eip155:1/erc20:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_POL = "eip155:137/erc20:0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
USDC_SOL_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_SOL = f"{SOL_CHAIN}/token:{USDC_SOL_MINT}"
WETH_ADDR = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
WETH = f"eip155:1/erc20:{WETH_ADDR}"
ETH_NATIVE = "eip155:1/slip44:60"

# Golden literals (derivation: docs/internal/DECISIONS.md algorithm, python3 -c hashlib).
AST_USDC_TRI = "ast_99f26454cb92a351"
AST_ETH = "ast_ed0bcc482c2859ce"
AST_WETH = "ast_5cd4341d4474350c"
AST_SLIP44_144 = "ast_61f696125f4f3993"  # sha256("eip155:1/slip44:144")
AST_TOKEN_144 = "ast_175ff7235e3acaee"  # sha256("eip155:1/token:144")


def _usdc() -> Asset:
    return make_asset(
        symbol="USDC",
        name="USD Coin",
        implementations=(
            Implementation(caip19=USDC_ETH, chain_id="eip155:1", decimals=6),
            Implementation(caip19=USDC_POL, chain_id="eip155:137", decimals=6),
            Implementation(caip19=USDC_SOL, chain_id=SOL_CHAIN, decimals=6),
        ),
        asset_class=AssetClass.STABLECOIN,
    )


def _eth() -> Asset:
    return make_asset(
        symbol="ETH",
        name="Ether",
        implementations=(
            Implementation(caip19=ETH_NATIVE, chain_id="eip155:1", decimals=18),
        ),
        asset_class=AssetClass.NATIVE,
    )


def _weth() -> Asset:
    return make_asset(
        symbol="WETH",
        name="Wrapped Ether",
        implementations=(
            Implementation(caip19=WETH, chain_id="eip155:1", decimals=18),
        ),
        asset_class=AssetClass.WRAPPED,
    )


# --- empty registry -----------------------------------------------------------


def test_new_registry_is_empty():
    assert AssetRegistry().assets() == ()


def test_get_by_id_on_empty_registry_raises():
    with pytest.raises(UnknownAssetError):
        AssetRegistry().get_by_id(AST_USDC_TRI)


# --- register + get_by_id -------------------------------------------------------


def test_register_then_get_by_id():
    registry = AssetRegistry()
    usdc = _usdc()
    registry.register(usdc)
    assert registry.get_by_id(AST_USDC_TRI) == usdc


def test_registering_the_same_asset_twice_is_a_noop():
    registry = AssetRegistry()
    registry.register(_usdc())
    registry.register(_usdc())  # identical: must not raise
    assert len(registry.assets()) == 1


def test_rebinding_a_caip19_to_a_different_asset_id_raises():
    registry = AssetRegistry()
    registry.register(_eth())
    # Same slip44 caip19 inside a DIFFERENT implementation set → different id.
    impostor = make_asset(
        symbol="ETH2",
        name="Ether impostor",
        implementations=(
            Implementation(caip19=ETH_NATIVE, chain_id="eip155:1", decimals=18),
            Implementation(caip19=WETH, chain_id="eip155:1", decimals=18),
        ),
        asset_class=AssetClass.NATIVE,
    )
    with pytest.raises(AssetConflictError):
        registry.register(impostor)


def test_rejected_conflict_never_mutates_the_registry():
    registry = AssetRegistry()
    registry.register(_eth())
    impostor = make_asset(
        symbol="X",
        name="X",
        implementations=(
            Implementation(caip19=ETH_NATIVE, chain_id="eip155:1", decimals=18),
            Implementation(caip19=WETH, chain_id="eip155:1", decimals=18),
        ),
        asset_class=AssetClass.TOKEN,
    )
    with pytest.raises(AssetConflictError):
        registry.register(impostor)
    assert registry.get_by_id(AST_ETH) == _eth()
    assert [a.id for a in registry.assets()] == [AST_ETH]
    with pytest.raises(UnknownAssetError):
        registry.get_by_caip19(WETH)  # the impostor's other leg never landed


def test_same_id_with_differing_metadata_raises():
    registry = AssetRegistry()
    usdc = _usdc()
    registry.register(usdc)
    with pytest.raises(AssetConflictError):
        registry.register(dataclasses.replace(usdc, symbol="NOTUSDC"))


# --- get_by_caip19: canonicalizes first ------------------------------------------


def test_get_by_caip19_exact_form():
    registry = AssetRegistry()
    registry.register(_usdc())
    assert registry.get_by_caip19(USDC_ETH).id == AST_USDC_TRI


@pytest.mark.parametrize("variant", [USDC_ETH_UPPER, USDC_ETH_MIXED])
def test_get_by_caip19_evm_case_variants_find_the_asset(variant):
    registry = AssetRegistry()
    registry.register(_usdc())
    assert registry.get_by_caip19(variant).id == AST_USDC_TRI


def test_get_by_caip19_finds_every_implementation_leg():
    registry = AssetRegistry()
    registry.register(_usdc())
    assert registry.get_by_caip19(USDC_POL).id == AST_USDC_TRI
    assert registry.get_by_caip19(USDC_SOL).id == AST_USDC_TRI


def test_get_by_caip19_registered_with_mixed_case_found_lowercase():
    registry = AssetRegistry()
    registry.register(
        make_asset(
            symbol="USDC",
            name="USD Coin",
            implementations=(
                Implementation(caip19=USDC_ETH_MIXED, chain_id="eip155:1", decimals=6),
            ),
            asset_class=AssetClass.STABLECOIN,
        )
    )
    assert registry.get_by_caip19(USDC_ETH).symbol == "USDC"


def test_get_by_caip19_solana_case_is_significant():
    registry = AssetRegistry()
    registry.register(_usdc())
    with pytest.raises(UnknownAssetError):
        registry.get_by_caip19(f"{SOL_CHAIN}/token:{USDC_SOL_MINT.lower()}")


def test_get_by_caip19_unknown_raises():
    registry = AssetRegistry()
    registry.register(_usdc())
    with pytest.raises(UnknownAssetError):
        registry.get_by_caip19(WETH)


def test_get_by_caip19_malformed_raises_parse_error():
    with pytest.raises(CaipParseError):
        AssetRegistry().get_by_caip19("garbage")


# --- get_by_chain_address: the other half of both-ways addressing -----------------


def test_get_by_chain_address_uppercase_evm_address_finds_the_asset():
    # Acceptance: uppercase USDC address on eip155:1 (case-insensitive EVM).
    registry = AssetRegistry()
    registry.register(_usdc())
    found = registry.get_by_chain_address(
        "eip155:1", "0xA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
    )
    assert found.id == AST_USDC_TRI


def test_get_by_chain_address_lowercase_evm_address_finds_the_asset():
    registry = AssetRegistry()
    registry.register(_usdc())
    found = registry.get_by_chain_address(
        "eip155:1", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    )
    assert found.id == AST_USDC_TRI


def test_get_by_chain_address_is_chain_scoped():
    registry = AssetRegistry()
    registry.register(_usdc())
    with pytest.raises(UnknownAssetError):
        # Right address, wrong chain: Base never registered.
        registry.get_by_chain_address("eip155:8453", USDC_ETH.split(":")[-1])


def test_get_by_chain_address_solana_exact_case():
    registry = AssetRegistry()
    registry.register(_usdc())
    assert registry.get_by_chain_address(SOL_CHAIN, USDC_SOL_MINT).id == AST_USDC_TRI


def test_get_by_chain_address_solana_wrong_case_raises():
    registry = AssetRegistry()
    registry.register(_usdc())
    with pytest.raises(UnknownAssetError):
        registry.get_by_chain_address(SOL_CHAIN, USDC_SOL_MINT.lower())


def test_get_by_chain_address_unknown_raises():
    registry = AssetRegistry()
    registry.register(_usdc())
    with pytest.raises(UnknownAssetError):
        registry.get_by_chain_address("eip155:1", "0x" + "00" * 20)


# --- slip44 vs token: coin types are NOT addresses --------------------------------
# "144" is a valid slip44 coin type AND a valid base58 token reference.
# slip44 references never enter the (chain, address) index, so the two
# coexist and only the token is address-reachable.


def _slip44_144() -> Asset:
    return make_asset(
        symbol="XRP",
        name="Ripple (coin type)",
        implementations=(
            Implementation(caip19="eip155:1/slip44:144", chain_id="eip155:1", decimals=6),
        ),
        asset_class=AssetClass.NATIVE,
    )


def _token_144() -> Asset:
    return make_asset(
        symbol="T144",
        name="Token at base58 144",
        implementations=(
            Implementation(caip19="eip155:1/token:144", chain_id="eip155:1", decimals=6),
        ),
        asset_class=AssetClass.TOKEN,
    )


def test_slip44_and_token_with_colliding_reference_register_side_by_side():
    registry = AssetRegistry()
    registry.register(_slip44_144())
    registry.register(_token_144())  # no conflict: slip44 is not an address
    assert [a.id for a in registry.assets()] == sorted(
        [AST_SLIP44_144, AST_TOKEN_144]
    )
    assert registry.get_by_caip19("eip155:1/slip44:144").id == AST_SLIP44_144
    assert registry.get_by_caip19("eip155:1/token:144").id == AST_TOKEN_144


def test_get_by_chain_address_returns_the_token_never_the_coin_type():
    registry = AssetRegistry()
    registry.register(_slip44_144())
    registry.register(_token_144())
    assert registry.get_by_chain_address("eip155:1", "144").id == AST_TOKEN_144


def test_slip44_reference_is_not_reachable_as_an_address():
    registry = AssetRegistry()
    registry.register(_slip44_144())  # ONLY the coin type
    with pytest.raises(UnknownAssetError):
        registry.get_by_chain_address("eip155:1", "144")


# --- assets(): deterministic order -------------------------------------------------


def test_assets_returns_tuple_sorted_by_id():
    registry = AssetRegistry()
    registry.register(_eth())
    registry.register(_usdc())
    registry.register(_weth())
    listed = registry.assets()
    assert isinstance(listed, tuple)
    # '5cd' < '99f' < 'ed0' — pinned, independent of registration order.
    assert [a.id for a in listed] == [AST_WETH, AST_USDC_TRI, AST_ETH]


def test_assets_order_is_independent_of_registration_order():
    first, second = AssetRegistry(), AssetRegistry()
    first.register(_eth())
    first.register(_weth())
    second.register(_weth())
    second.register(_eth())
    assert [a.id for a in first.assets()] == [a.id for a in second.assets()]


def test_registry_instances_are_independent():
    first, second = AssetRegistry(), AssetRegistry()
    first.register(_eth())
    assert second.assets() == ()
    with pytest.raises(UnknownAssetError):
        second.get_by_id(AST_ETH)
