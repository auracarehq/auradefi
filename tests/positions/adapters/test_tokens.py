"""Contract tests for the ERC-20 fork helpers + ReceiptTokenAdapter base
(SPEC §5.4: "What makes that true is not the interface, it is the fork
helpers"; SPEC §4.3: vault shares valued by redemption).

Pinned redemption (DECISIONS.md "Receipt-token redemption", breaking to
change): ``underlying_raw = share_raw * rate_raw // 10**18``: integer
floor, 18-decimal fixed-point rate, identity ``10**18`` when ``rate_fn``
is ``None`` (rebasing 1:1 receipts like stETH). Golden id vectors below
are hardcoded from the pinned algorithms, never computed by the code
under test:

    pos_f9b93b10e5b933ec == "pos_" + sha256(
        "stake-fork|eip155:1|0x00000000000000000000000000000000000000aa|"
    ).hexdigest()[:16]
    grp_e290c5c26d00e935 == "grp_" + sha256(
        "stake-fork|eip155:1|0x00000000000000000000000000000000000000aa"
    ).hexdigest()[:16]
    pos_b0353dec5ca061fb / grp_866e08100f6259a7: same over ...bb.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from auradefi.money.quantity import Quantity
from auradefi.positions.adapters.tokens import (
    ReceiptToken,
    ReceiptTokenAdapter,
    caip19_for_erc20,
    erc20_balance,
    erc20_decimals,
    erc20_total_supply,
)
from auradefi.positions.models import (
    MetaType,
    PositionKind,
    PositionType,
    ProtocolModule,
)
from auradefi.positions.protocol import (
    ContractDescriptor,
    ContractSet,
    DiscoveryContext,
    PositionAdapter,
    ResolveContext,
)

CHAIN = "eip155:1"
HOLDER = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
RECEIPT = "0x00000000000000000000000000000000000000aa"
RECEIPT_2 = "0x00000000000000000000000000000000000000bb"
ETH = "eip155:1/slip44:60"

RATE = 1_120_000_000_000_000_000  # 1.12, as an 18-decimal fixed point
ONE = 10**18

# Hardcoded golden ids (derivation in the module docstring).
POS_RECEIPT = "pos_f9b93b10e5b933ec"
GRP_RECEIPT = "grp_e290c5c26d00e935"
POS_RECEIPT_2 = "pos_b0353dec5ca061fb"
GRP_RECEIPT_2 = "grp_866e08100f6259a7"


class RecordingReader:
    """Dict-backed ContractReader that logs every call it serves."""

    def __init__(self, responses: dict[tuple[str, str, tuple], object]) -> None:
        self._responses = dict(responses)
        self.calls: list[tuple[str, str, tuple]] = []

    def call(self, address: str, fn: str, args: tuple[object, ...] = ()) -> object:
        self.calls.append((address, fn, args))
        return self._responses[(address, fn, args)]

    def fns_called(self) -> set[str]:
        return {fn for _, fn, _ in self.calls}


class StakeForkAdapter(ReceiptTokenAdapter):
    """Synthetic fork: one rate-bearing receipt, one rebasing receipt."""

    id = "stake-fork"
    chains = frozenset({CHAIN})
    receipts = {
        CHAIN: (
            ReceiptToken(RECEIPT, ETH, 18, "getRate"),
            ReceiptToken(RECEIPT_2, ETH, 18, None),
        ),
    }


def _discover(adapter, reader) -> ContractSet:
    return adapter.discover(DiscoveryContext(chain_id=CHAIN, reader=reader))


def _resolve(adapter, reader, contracts):
    ctx = ResolveContext(chain_id=CHAIN, address=HOLDER, reader=reader)
    return adapter.resolve(ctx, contracts)


class TestErc20Helpers:
    def test_erc20_balance_reads_balanceOf(self):
        reader = RecordingReader({(USDC, "balanceOf", (HOLDER,)): 1_250_000})
        assert erc20_balance(reader, USDC, HOLDER) == 1_250_000
        assert reader.calls == [(USDC, "balanceOf", (HOLDER,))]

    def test_erc20_balance_10_to_77_scale_passes_exactly(self):
        huge = 10**77 + 1
        reader = RecordingReader({(USDC, "balanceOf", (HOLDER,)): huge})
        result = erc20_balance(reader, USDC, HOLDER)
        assert result == huge
        assert type(result) is int

    def test_erc20_decimals_reads_decimals(self):
        reader = RecordingReader({(USDC, "decimals", ()): 6})
        assert erc20_decimals(reader, USDC) == 6
        assert reader.calls == [(USDC, "decimals", ())]

    def test_erc20_total_supply_reads_totalSupply(self):
        reader = RecordingReader({(USDC, "totalSupply", ()): 10**27})
        assert erc20_total_supply(reader, USDC) == 10**27
        assert reader.calls == [(USDC, "totalSupply", ())]


class TestCaip19ForErc20:
    def test_lowercases_a_checksummed_address(self):
        assert (
            caip19_for_erc20(CHAIN, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
            == f"eip155:1/erc20:{USDC}"
        )

    def test_exact_format_on_another_chain(self):
        assert (
            caip19_for_erc20("eip155:8453", RECEIPT)
            == f"eip155:8453/erc20:{RECEIPT}"
        )

    def test_lowercase_input_is_passthrough(self):
        assert caip19_for_erc20(CHAIN, USDC) == f"eip155:1/erc20:{USDC}"


class TestReceiptToken:
    def test_positional_fields(self):
        receipt = ReceiptToken(RECEIPT, ETH, 18, "getExchangeRate")
        assert receipt.address == RECEIPT
        assert receipt.underlying_caip19 == ETH
        assert receipt.underlying_decimals == 18
        assert receipt.rate_fn == "getExchangeRate"

    def test_rate_fn_none_means_rebasing_identity(self):
        assert ReceiptToken(RECEIPT, ETH, 18, None).rate_fn is None

    def test_frozen(self):
        receipt = ReceiptToken(RECEIPT, ETH, 18, None)
        with pytest.raises(FrozenInstanceError):
            receipt.rate_fn = "getRate"


class TestReceiptTokenAdapterClassContract:
    def test_default_axes_are_staked_by_staked(self):
        assert ReceiptTokenAdapter.position_type is PositionType.STAKED
        assert ReceiptTokenAdapter.protocol_module is ProtocolModule.STAKED

    def test_subclass_satisfies_position_adapter_protocol(self):
        adapter = StakeForkAdapter()
        assert isinstance(adapter, PositionAdapter)
        assert adapter.id == "stake-fork"
        assert adapter.chains == frozenset({CHAIN})


class TestDiscover:
    def test_one_descriptor_per_receipt_on_the_chain(self):
        contracts = _discover(StakeForkAdapter(), RecordingReader({}))
        assert isinstance(contracts, ContractSet)
        assert len(contracts) == 2
        by_address = {d.address: d for d in contracts}
        assert set(by_address) == {RECEIPT, RECEIPT_2}
        for descriptor in contracts:
            assert descriptor.adapter_id == "stake-fork"
            assert descriptor.chain_id == CHAIN
            assert descriptor.category == "receipt-token"
            assert descriptor.underlyings == (ETH,)

    def test_discover_is_static_and_never_touches_the_reader(self):
        # SPEC §5.1: discovery output is static configuration.
        reader = RecordingReader({})
        _discover(StakeForkAdapter(), reader)
        assert reader.calls == []

    def test_chain_without_receipts_yields_the_empty_set(self):
        ctx = DiscoveryContext(chain_id="eip155:10", reader=RecordingReader({}))
        assert StakeForkAdapter().discover(ctx) == ContractSet.empty()


class TestResolveRedemption:
    def _one_receipt_set(self, address: str) -> ContractSet:
        contracts = _discover(StakeForkAdapter(), RecordingReader({}))
        return contracts.restrict_to(frozenset({address}))

    def test_pinned_redemption_vector(self):
        # 2.5 shares at rate 1.12 -> exactly 2.8 underlying.
        reader = RecordingReader(
            {
                (RECEIPT, "balanceOf", (HOLDER,)): 2_500_000_000_000_000_000,
                (RECEIPT, "getRate", ()): RATE,
            }
        )
        positions = _resolve(
            StakeForkAdapter(), reader, self._one_receipt_set(RECEIPT)
        )
        assert len(positions) == 1
        underlying = positions[0].underlyings[0]
        assert underlying.quantity == Quantity(2_800_000_000_000_000_000, 18)

    def test_floor_vector_truncates_never_rounds(self):
        # 1_500_000_000_000_000_001 * 1.12 = 1_680_000_000_000_000_001.12
        # -> floor 1_680_000_000_000_000_001.
        reader = RecordingReader(
            {
                (RECEIPT, "balanceOf", (HOLDER,)): 1_500_000_000_000_000_001,
                (RECEIPT, "getRate", ()): RATE,
            }
        )
        positions = _resolve(
            StakeForkAdapter(), reader, self._one_receipt_set(RECEIPT)
        )
        assert positions[0].underlyings[0].quantity == Quantity(
            1_680_000_000_000_000_001, 18
        )

    def test_rate_fn_none_uses_the_identity_rate(self):
        reader = RecordingReader(
            {(RECEIPT_2, "balanceOf", (HOLDER,)): 5_000_000_000_000_000_000}
        )
        positions = _resolve(
            StakeForkAdapter(), reader, self._one_receipt_set(RECEIPT_2)
        )
        assert positions[0].underlyings[0].quantity == Quantity(
            5_000_000_000_000_000_000, 18
        )
        # Identity is arithmetic, not a chain read.
        assert reader.fns_called() == {"balanceOf"}

    def test_10_to_77_scale_share_is_exact(self):
        share = 10**77 + 1
        reader = RecordingReader(
            {
                (RECEIPT, "balanceOf", (HOLDER,)): share,
                (RECEIPT, "getRate", ()): RATE,
            }
        )
        positions = _resolve(
            StakeForkAdapter(), reader, self._one_receipt_set(RECEIPT)
        )
        # (10**77 + 1) * 1.12 floors to 112 * 10**75 + 1, exactly.
        assert positions[0].underlyings[0].quantity == Quantity(
            112 * 10**75 + 1, 18
        )


class TestResolvePositionShape:
    def _positions(self):
        reader = RecordingReader(
            {
                (RECEIPT, "balanceOf", (HOLDER,)): 2_500_000_000_000_000_000,
                (RECEIPT, "getRate", ()): RATE,
                (RECEIPT_2, "balanceOf", (HOLDER,)): ONE,
            }
        )
        adapter = StakeForkAdapter()
        return _resolve(adapter, reader, _discover(adapter, RecordingReader({})))

    def test_one_position_per_held_receipt_in_descriptor_order(self):
        positions = self._positions()
        assert isinstance(positions, list)
        # ContractSet iterates sorted by address: ...aa before ...bb.
        assert [p.id for p in positions] == [POS_RECEIPT, POS_RECEIPT_2]

    def test_pinned_ids_and_group_ids(self):
        first, second = self._positions()
        assert first.id == POS_RECEIPT
        assert first.group_id == GRP_RECEIPT
        assert second.id == POS_RECEIPT_2
        assert second.group_id == GRP_RECEIPT_2

    def test_app_token_with_class_attr_axes(self):
        for position in self._positions():
            assert position.kind is PositionKind.APP_TOKEN
            assert position.position_type is PositionType.STAKED
            assert position.protocol_module is ProtocolModule.STAKED
            assert position.adapter_id == "stake-fork"
            assert position.chain_id == CHAIN

    def test_contract_address_is_the_receipt(self):
        first, second = self._positions()
        assert first.contract_address == RECEIPT
        assert second.contract_address == RECEIPT_2

    def test_single_supplied_underlying_raw(self):
        for position in self._positions():
            assert len(position.underlyings) == 1
            underlying = position.underlyings[0]
            assert underlying.asset_id == ETH
            assert underlying.meta_type is MetaType.SUPPLIED
            assert underlying.price is None
            assert underlying.value is None
        # Raw everywhere -> Position.value is None until drill.
        assert all(p.value is None for p in self._positions())

    def test_overridden_class_attr_axes_flow_through(self):
        class YieldFork(ReceiptTokenAdapter):
            id = "yield-fork"
            chains = frozenset({CHAIN})
            receipts = {CHAIN: (ReceiptToken(RECEIPT, ETH, 18, None),)}
            position_type = PositionType.DEPOSIT
            protocol_module = ProtocolModule.YIELD

        adapter = YieldFork()
        reader = RecordingReader({(RECEIPT, "balanceOf", (HOLDER,)): ONE})
        positions = _resolve(
            adapter, reader, _discover(adapter, RecordingReader({}))
        )
        assert positions[0].position_type is PositionType.DEPOSIT
        assert positions[0].protocol_module is ProtocolModule.YIELD


class TestResolveSkipsAndPrefilter:
    def test_zero_share_no_position_and_no_rate_call(self):
        reader = RecordingReader(
            {
                (RECEIPT, "balanceOf", (HOLDER,)): 0,
                (RECEIPT_2, "balanceOf", (HOLDER,)): ONE,
            }
        )
        adapter = StakeForkAdapter()
        positions = _resolve(
            adapter, reader, _discover(adapter, RecordingReader({}))
        )
        assert [p.id for p in positions] == [POS_RECEIPT_2]
        assert "getRate" not in reader.fns_called()

    def test_all_zero_shares_yield_the_empty_list(self):
        reader = RecordingReader(
            {
                (RECEIPT, "balanceOf", (HOLDER,)): 0,
                (RECEIPT_2, "balanceOf", (HOLDER,)): 0,
            }
        )
        adapter = StakeForkAdapter()
        positions = _resolve(
            adapter, reader, _discover(adapter, RecordingReader({}))
        )
        assert positions == []
        assert reader.fns_called() == {"balanceOf"}

    def test_empty_contract_set_yields_empty_without_touching_reader(self):
        reader = RecordingReader({})
        positions = _resolve(StakeForkAdapter(), reader, ContractSet.empty())
        assert positions == []
        assert reader.calls == []

    def test_only_surviving_descriptors_are_read(self):
        # SPEC §5.2 pre-filter: the untouched receipt is never queried, 
        # the reader has no responses for it, so a stray read would raise.
        reader = RecordingReader({(RECEIPT_2, "balanceOf", (HOLDER,)): ONE})
        adapter = StakeForkAdapter()
        contracts = _discover(adapter, RecordingReader({})).restrict_to(
            frozenset({RECEIPT_2})
        )
        positions = _resolve(adapter, reader, contracts)
        assert [p.id for p in positions] == [POS_RECEIPT_2]
        assert {address for address, _, _ in reader.calls} == {RECEIPT_2}


class TestStaleDescriptorCostsOnePosition:
    """RELEASE_0.1.1 §5 #31. One unknown descriptor is not a wipe-out."""

    def test_a_descriptor_with_no_receipt_is_skipped_not_raised(self):
        # pins: descriptor sets are "persisted between discovery runs"
        #       (ContractDescriptor's own docstring), so a descriptor can
        #       outlive the receipt table that produced it: a delisted
        #       receipt, a renamed adapter, a set written by an older
        #       release. The unguarded index raised KeyError on the FIRST
        #       such descriptor, and because resolve() builds its whole list
        #       before returning, that removed EVERY Lido/Rocket Pool
        #       position from net_worth rather than the one stale row. The
        #       Aave adapter already does .get(...) + continue; this matches.
        stale = ContractDescriptor(
            adapter_id="stake-fork",
            chain_id=CHAIN,
            address="0x00000000000000000000000000000000000000ff",
            category="receipt",
        )
        reader = RecordingReader(
            {
                (RECEIPT, "balanceOf", (HOLDER,)): ONE,
                (RECEIPT, "getRate", ()): RATE,
                (RECEIPT_2, "balanceOf", (HOLDER,)): ONE,
            }
        )
        adapter = StakeForkAdapter()
        live = _discover(adapter, RecordingReader({}))
        contracts = ContractSet.of(*live, stale)

        positions = _resolve(adapter, reader, contracts)

        assert [p.id for p in positions] == [POS_RECEIPT, POS_RECEIPT_2], (
            "one stale descriptor cost the entire staking slice, not one row"
        )

    def test_a_stale_descriptor_never_reaches_the_reader(self):
        # pins: the skip happens BEFORE any contract call, so an unknown
        #       descriptor costs no RPC either.
        stale = ContractDescriptor(
            adapter_id="stake-fork",
            chain_id=CHAIN,
            address="0x00000000000000000000000000000000000000ff",
            category="receipt",
        )
        reader = RecordingReader({})
        positions = _resolve(StakeForkAdapter(), reader, ContractSet.of(stale))

        assert positions == []
        assert reader.calls == []
