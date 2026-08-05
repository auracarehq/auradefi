"""ChainRegistry: CAIP-2 is the ONLY key; five pinned seed chains (SPEC §4.2).

Every seed row is asserted field-by-field against hardcoded literals.
These are wire-format contracts, not derived values.
"""

from __future__ import annotations

import dataclasses

import pytest

from auradefi.chains.families import ChainFamily
from auradefi.chains.registry import Chain, ChainRegistry
from auradefi.errors import ConflictError, UnknownChainError

BTC_CAIP2 = "bip122:000000000019d6689c085ae165831e93"
SOL_CAIP2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

# (caip2, family, name, native_caip19, native_symbol, native_decimals)
SEEDS = [
    ("eip155:1", ChainFamily.EVM, "Ethereum", "eip155:1/slip44:60", "ETH", 18),
    ("eip155:137", ChainFamily.EVM, "Polygon", "eip155:137/slip44:966", "POL", 18),
    ("eip155:8453", ChainFamily.EVM, "Base", "eip155:8453/slip44:60", "ETH", 18),
    (BTC_CAIP2, ChainFamily.BITCOIN, "Bitcoin", f"{BTC_CAIP2}/slip44:0", "BTC", 8),
    (SOL_CAIP2, ChainFamily.SOLANA, "Solana", f"{SOL_CAIP2}/slip44:501", "SOL", 9),
]


def _ethereum() -> Chain:
    return Chain(
        caip2="eip155:1",
        family=ChainFamily.EVM,
        name="Ethereum",
        native_caip19="eip155:1/slip44:60",
        native_symbol="ETH",
        native_decimals=18,
    )


# --- Chain the dataclass --------------------------------------------------


def test_chain_is_frozen():
    chain = _ethereum()
    with pytest.raises(dataclasses.FrozenInstanceError):
        chain.name = "Renamed"  # type: ignore[misc]


def test_chain_has_slots_no_instance_dict():
    chain = _ethereum()
    assert not hasattr(chain, "__dict__")
    assert set(Chain.__slots__) == {
        "caip2", "family", "name", "native_caip19", "native_symbol", "native_decimals",
    }
    # The exact exception for an unknown attribute varies by CPython version
    # on frozen+slots dataclasses (FrozenInstanceError / AttributeError /
    # TypeError); the contract is only that assignment MUST fail.
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError, TypeError)):
        chain.new_attribute = 1  # type: ignore[attr-defined]


def test_chain_value_equality_by_fields():
    assert _ethereum() == _ethereum()
    assert _ethereum() != dataclasses.replace(_ethereum(), native_decimals=8)


# --- seeding ---------------------------------------------------------------


def test_registry_is_seeded_with_exactly_the_five_pinned_chains():
    registry = ChainRegistry()
    assert [c.caip2 for c in registry.chains()] == [
        BTC_CAIP2,  # 'b' < 'e' < 's': sorted by caip2 string
        "eip155:1",
        "eip155:137",
        "eip155:8453",
        SOL_CAIP2,
    ]


@pytest.mark.parametrize(
    ("caip2", "family", "name", "native_caip19", "native_symbol", "native_decimals"),
    SEEDS,
)
def test_seed_fields_are_pinned(
    caip2, family, name, native_caip19, native_symbol, native_decimals
):
    chain = ChainRegistry().get(caip2)
    assert chain.caip2 == caip2
    assert chain.family is family
    assert chain.name == name
    assert chain.native_caip19 == native_caip19
    assert chain.native_symbol == native_symbol
    assert chain.native_decimals == native_decimals


def test_acceptance_ethereum_native_asset():
    ethereum = ChainRegistry().get("eip155:1")
    assert ethereum.native_caip19 == "eip155:1/slip44:60"
    assert ethereum.native_decimals == 18


def test_chains_returns_a_tuple_sorted_by_caip2():
    listed = ChainRegistry().chains()
    assert isinstance(listed, tuple)
    assert [c.caip2 for c in listed] == sorted(c.caip2 for c in listed)


# --- get: unknown ids and the dead name zoo ---------------------------------


def test_get_unknown_but_wellformed_caip2_raises():
    with pytest.raises(UnknownChainError):
        ChainRegistry().get("eip155:999999")


@pytest.mark.parametrize(
    "zoo_key",
    [
        "ethereum",  # Allium
        "eth-mainnet",  # GoldRush
        "1",  # Dune SIM
        "ETH",
        "polygon",
        "matic",
        "Base",
        "bitcoin",
        "btc",
        "solana",
        "sol-mainnet",
        "",
    ],
)
def test_get_by_vendor_name_raises_unknown_chain(zoo_key):
    # SPEC §4.2: CAIP-2 is the only key. No translation table, ever.
    with pytest.raises(UnknownChainError):
        ChainRegistry().get(zoo_key)


# --- register ----------------------------------------------------------------


def test_reregistering_an_identical_chain_is_a_noop():
    registry = ChainRegistry()
    registry.register(_ethereum())  # equal to the seed: must not raise
    assert len(registry.chains()) == 5
    assert registry.get("eip155:1") == _ethereum()


@pytest.mark.parametrize(
    "conflicting",
    [
        dataclasses.replace(_ethereum(), name="Ethereum Classic"),
        dataclasses.replace(_ethereum(), native_decimals=8),
        dataclasses.replace(_ethereum(), native_symbol="WETH"),
        dataclasses.replace(_ethereum(), native_caip19="eip155:1/slip44:61"),
        dataclasses.replace(_ethereum(), family=ChainFamily.BITCOIN),
    ],
)
def test_registering_a_conflicting_chain_raises(conflicting):
    registry = ChainRegistry()
    with pytest.raises(ConflictError):
        registry.register(conflicting)
    # detection is never destructive: the seed row survives the rejection
    assert registry.get("eip155:1") == _ethereum()


def test_registering_a_new_chain_makes_it_gettable_and_keeps_order():
    registry = ChainRegistry()
    optimism = Chain(
        caip2="eip155:10",
        family=ChainFamily.EVM,
        name="OP Mainnet",
        native_caip19="eip155:10/slip44:60",
        native_symbol="ETH",
        native_decimals=18,
    )
    registry.register(optimism)
    assert registry.get("eip155:10") == optimism
    listed = registry.chains()
    assert len(listed) == 6
    assert [c.caip2 for c in listed] == sorted(c.caip2 for c in listed)


def test_registry_instances_are_independent():
    first = ChainRegistry()
    second = ChainRegistry()
    first.register(
        Chain(
            caip2="eip155:42161",
            family=ChainFamily.EVM,
            name="Arbitrum One",
            native_caip19="eip155:42161/slip44:60",
            native_symbol="ETH",
            native_decimals=18,
        )
    )
    assert len(first.chains()) == 6
    assert len(second.chains()) == 5
    with pytest.raises(UnknownChainError):
        second.get("eip155:42161")
