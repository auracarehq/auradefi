"""Contract tests for auradefi.positions.protocol (SPEC §5.4, §5.2, §5.1).

``ContractReader`` is the ONLY chain-read abstraction in positions/ —
positions is not an IO domain, so a dict-backed fake must satisfy the
protocol and the whole domain stays offline. ``ContractSet`` is the
pre-filter vehicle: ``restrict_to`` implements SPEC §5.2 (only contracts
the user actually touched run) and arrives at ``resolve()`` partially
populated or empty (SPEC §5.4).
"""

from __future__ import annotations

import dataclasses
import inspect
from dataclasses import FrozenInstanceError

import pytest

from auradefi.positions.protocol import (
    ContractDescriptor,
    ContractReader,
    ContractSet,
    DiscoveryContext,
    PositionAdapter,
    ResolveContext,
)

AAVE_POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
AAVE_POOL_137 = "0x794a61358d6845594f94dc1db02a252b5b4814ad"
UNIV3_POOL = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"
UNIV3_MANAGER = "0xc36442b4a4522e871399cd717abdd847ab11fe88"
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "eip155:1/erc20:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
SOLANA_ADDRESS = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"


class DictReader:
    """Dict-backed fake — SPEC §5.4: satisfies ContractReader with no I/O."""

    def __init__(self, responses: dict[tuple[str, str, tuple], object]) -> None:
        self._responses = dict(responses)

    def call(self, address: str, fn: str, args: tuple[object, ...] = ()) -> object:
        return self._responses[(address, fn, args)]


def descriptor(**overrides) -> ContractDescriptor:
    fields = {
        "adapter_id": "aave-v3",
        "chain_id": "eip155:1",
        "address": AAVE_POOL,
        "category": "pool",
    }
    fields.update(overrides)
    return ContractDescriptor(**fields)


class TestContractReader:
    def test_dict_backed_fake_satisfies_the_protocol(self):
        reader = DictReader({(AAVE_POOL, "getReserveData", (USDC,)): (1, 2, 3)})
        assert isinstance(reader, ContractReader)
        assert reader.call(AAVE_POOL, "getReserveData", (USDC,)) == (1, 2, 3)

    def test_object_without_call_does_not_satisfy(self):
        class NoCall:
            pass

        assert not isinstance(NoCall(), ContractReader)

    def test_call_signature_args_default_is_empty_tuple(self):
        parameters = inspect.signature(ContractReader.call).parameters
        assert list(parameters) == ["self", "address", "fn", "args"]
        assert parameters["args"].default == ()


class TestDiscoveryContext:
    def test_fields(self):
        reader = DictReader({})
        ctx = DiscoveryContext(chain_id="eip155:1", reader=reader)
        assert ctx.chain_id == "eip155:1"
        assert ctx.reader is reader

    def test_frozen(self):
        ctx = DiscoveryContext(chain_id="eip155:1", reader=DictReader({}))
        with pytest.raises(FrozenInstanceError):
            ctx.chain_id = "eip155:137"

    def test_has_no_address_field(self):
        # SPEC §5.1: discover() does NOT know the address.
        field_names = {f.name for f in dataclasses.fields(DiscoveryContext)}
        assert "address" not in field_names


class TestResolveContext:
    def test_lowercases_0x_address_in_post_init(self):
        ctx = ResolveContext(
            chain_id="eip155:1",
            address="0xD8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            reader=DictReader({}),
        )
        assert ctx.address == "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

    def test_non_0x_address_keeps_case(self):
        # Solana base58 is case-significant (same rule as canonical CAIP-19).
        ctx = ResolveContext(
            chain_id="solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
            address=SOLANA_ADDRESS,
            reader=DictReader({}),
        )
        assert ctx.address == SOLANA_ADDRESS

    def test_block_number_defaults_to_none(self):
        ctx = ResolveContext(
            chain_id="eip155:1", address=AAVE_POOL, reader=DictReader({})
        )
        assert ctx.block_number is None

    def test_explicit_block_number_kept(self):
        ctx = ResolveContext(
            chain_id="eip155:1",
            address=AAVE_POOL,
            reader=DictReader({}),
            block_number=19_000_000,
        )
        assert ctx.block_number == 19_000_000

    def test_frozen(self):
        ctx = ResolveContext(
            chain_id="eip155:1", address=AAVE_POOL, reader=DictReader({})
        )
        with pytest.raises(FrozenInstanceError):
            ctx.address = UNIV3_POOL


class TestContractDescriptor:
    def test_lowercases_0x_address(self):
        d = descriptor(address="0x87870BCA3F3FD6335C3F4CE8392D69350B4FA4E2")
        assert d.address == AAVE_POOL

    def test_non_0x_address_keeps_case(self):
        d = descriptor(
            adapter_id="marinade",
            chain_id="solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
            address=SOLANA_ADDRESS,
        )
        assert d.address == SOLANA_ADDRESS

    def test_defaults_underlyings_and_meta_empty(self):
        d = descriptor()
        assert d.underlyings == ()
        assert d.meta == ()

    def test_hashable_and_equal_descriptors_dedupe_in_a_set(self):
        first = descriptor(underlyings=(USDC, WETH), meta=(("fee", "500"),))
        second = descriptor(underlyings=(USDC, WETH), meta=(("fee", "500"),))
        assert first == second
        assert hash(first) == hash(second)
        assert len({first, second}) == 1

    def test_checksummed_and_lowercase_inputs_are_equal(self):
        assert descriptor(
            address="0x87870BCA3F3FD6335C3F4CE8392D69350B4FA4E2"
        ) == descriptor(address=AAVE_POOL)

    def test_frozen(self):
        d = descriptor()
        with pytest.raises(FrozenInstanceError):
            d.category = "factory"


class TestContractSet:
    def _four(self):
        d_aave_1 = descriptor()
        d_aave_137 = descriptor(chain_id="eip155:137", address=AAVE_POOL_137)
        d_pool = descriptor(
            adapter_id="uniswap-v3",
            address=UNIV3_POOL,
            underlyings=(USDC, WETH),
        )
        d_manager = descriptor(
            adapter_id="uniswap-v3", address=UNIV3_MANAGER, category="nft_manager"
        )
        return d_aave_1, d_aave_137, d_pool, d_manager

    def test_empty_is_falsy_zero_length_yields_nothing(self):
        empty = ContractSet.empty()
        assert len(empty) == 0
        assert not empty
        assert list(empty) == []

    def test_of_nothing_equals_empty(self):
        assert ContractSet.of() == ContractSet.empty()

    def test_of_dedupes_and_iterates_sorted(self):
        d_aave_1, d_aave_137, d_pool, d_manager = self._four()
        duplicate = descriptor(
            adapter_id="uniswap-v3",
            address=UNIV3_POOL,
            underlyings=(USDC, WETH),
        )
        contract_set = ContractSet.of(
            d_manager, d_pool, d_aave_137, duplicate, d_aave_1
        )
        # Sorted by (adapter_id, chain_id, address, category); hand-ordered:
        # 'eip155:1' < 'eip155:137', '0x88e6...' < '0xc364...'.
        assert list(contract_set) == [d_aave_1, d_aave_137, d_pool, d_manager]
        assert len(contract_set) == 4
        assert bool(contract_set)

    def test_restrict_to_with_mixed_case_touched_addresses(self):
        d_aave_1, d_aave_137, d_pool, d_manager = self._four()
        contract_set = ContractSet.of(d_aave_1, d_aave_137, d_pool, d_manager)
        touched = frozenset(
            {
                "0x88E6A0C2dDD26FEEb64F039a2c41296FcB3f5640",  # univ3 pool
                "0x794A61358D6845594F94dc1DB02A252b5b4814aD",  # aave pool @137
            }
        )
        restricted = contract_set.restrict_to(touched)
        assert isinstance(restricted, ContractSet)
        assert list(restricted) == [d_aave_137, d_pool]

    def test_restrict_to_empty_touched_is_empty(self):
        d_aave_1, d_aave_137, d_pool, d_manager = self._four()
        contract_set = ContractSet.of(d_aave_1, d_aave_137, d_pool, d_manager)
        assert contract_set.restrict_to(frozenset()) == ContractSet.empty()

    def test_restrict_to_unknown_addresses_is_empty(self):
        contract_set = ContractSet.of(descriptor())
        restricted = contract_set.restrict_to(
            frozenset({"0x0000000000000000000000000000000000000001"})
        )
        assert restricted == ContractSet.empty()
        assert not restricted

    def test_frozen(self):
        contract_set = ContractSet.of(descriptor())
        with pytest.raises(FrozenInstanceError):
            contract_set.descriptors = ()


class TestPositionAdapterProtocol:
    def test_structural_adapter_satisfies_the_protocol(self):
        class UniswapV2Stub:
            id = "uniswap-v2"
            chains = frozenset({"eip155:1"})

            def discover(self, ctx):
                raise NotImplementedError

            def resolve(self, ctx, contracts):
                raise NotImplementedError

        adapter = UniswapV2Stub()
        assert isinstance(adapter, PositionAdapter)
        assert adapter.id == "uniswap-v2"  # DefiLlama slug — the join key
        assert adapter.chains == frozenset({"eip155:1"})

    def test_missing_resolve_does_not_satisfy(self):
        class DiscoverOnly:
            id = "uniswap-v2"
            chains = frozenset({"eip155:1"})

            def discover(self, ctx):
                raise NotImplementedError

        assert not isinstance(DiscoverOnly(), PositionAdapter)

    def test_missing_id_does_not_satisfy(self):
        class Anonymous:
            chains = frozenset({"eip155:1"})

            def discover(self, ctx):
                raise NotImplementedError

            def resolve(self, ctx, contracts):
                raise NotImplementedError

        assert not isinstance(Anonymous(), PositionAdapter)
