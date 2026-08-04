"""Contract tests for the Aave v3 lending adapter (SPEC §4.3, §5.4).

SPEC §4.3: supply is ``lending`` + ``deposit`` on an ``APP_TOKEN``
(aTokens are fungible and priceable); borrow is ``lending`` + ``loan``
on a ``CONTRACT_POSITION`` (debt cannot be added to MetaMask). BORROWED
alone carries the sign — resolve never emits a negative quantity. The
Pool is the risk unit: one shared group_id, GroupInfo on the FIRST
emitted position only, and NO getUserAccountData call when nothing was
emitted (asserted through the fake reader's call log).

Pinned id literals derived independently (python3 -c over the
DECISIONS.md algorithms), never from the code under test:

  pos_baff12a5eafb77f6 = "pos_"+sha256("aave-v3|eip155:1|<aweth>|")[:16]
  pos_1bbfb302ddabf62b = "pos_"+sha256("aave-v3|eip155:1|<vdebtusdc>|")[:16]
  grp_0f89caffe413b09f = "grp_"+sha256("aave-v3|eip155:1|<pool>")[:16]
  pos_1c76fc3175a40964 = "pos_"+sha256("spark|eip155:1|<spweth>|")[:16]
  grp_c02d06abbdf6ef8e = "grp_"+sha256("spark|eip155:1|<spark pool>")[:16]
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from auradefi.money.quantity import Quantity
from auradefi.positions.adapters.lending.aave import AaveV3Adapter, Market
from auradefi.positions.models import (
    MetaType,
    PositionKind,
    PositionType,
    ProtocolModule,
)
from auradefi.positions.protocol import (
    ContractSet,
    DiscoveryContext,
    PositionAdapter,
    ResolveContext,
)

POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
AWETH = "0x4d5f47fa6a74757f35c14fd3a6ef8e3c9bc514e8"
DEBT_WETH = "0xea51d7853eefb32b6ee06b1c12e6dcca88be0ffe"
AUSDC = "0x98c23e9d8f34fefb1b7bd6a91b7ff122f4e16f5c"
DEBT_USDC = "0x72e95b8931767c79ba4eee721354d6e99a61d004"
ETH = "eip155:1/slip44:60"
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USER_INPUT = "0x00000000000000000000000000000000000A11CE"
USER = "0x00000000000000000000000000000000000a11ce"

WETH_MARKET = Market(AWETH, DEBT_WETH, ETH, 18)
USDC_MARKET = Market(AUSDC, DEBT_USDC, USDC, 6)

# (tc, td, ab, clt, ltv_bp, hf_raw) — first four unused by the contract.
ACCOUNT_DATA = (3_584_250_000_000, 500_000_000_000, 2_367_400_000_000,
                8250, 8000, 5_812_500_000_000_000_000)

SPWETH = "0x59cd1c87501baa753d0b5b5ab5d8416a45cd71db"
SP_DEBT_WETH = "0x2e7576042566f8d6990e07a1b61ad1efd86ae70d"
SPARK_POOL = "0xC13e21B648A5Ee794902342038FF3aDAB66BE987"  # checksummed
SPARK_POOL_LOWER = "0xc13e21b648a5ee794902342038ff3adab66be987"


class TwoMarketAave(AaveV3Adapter):
    """Mainnet base adapter over the WETH + USDC reserves."""

    markets = (WETH_MARKET, USDC_MARKET)


class SparkFork(AaveV3Adapter):
    """A fork overrides id/chains/pool/markets ONLY (SPEC §5.4)."""

    id = "spark"
    chains = frozenset({"eip155:1"})
    pool = SPARK_POOL
    markets = (Market(SPWETH, SP_DEBT_WETH, ETH, 18),)


class LoggingReader:
    """Dict-backed ContractReader that logs every call it serves."""

    def __init__(self, responses: dict[tuple[str, str, tuple], object]) -> None:
        self._responses = dict(responses)
        self.calls: list[tuple[str, str, tuple]] = []

    def call(self, address: str, fn: str, args: tuple[object, ...] = ()) -> object:
        self.calls.append((address, fn, args))
        return self._responses[(address, fn, args)]


def two_market_reader(
    aweth: int = 0,
    debt_weth: int = 0,
    ausdc: int = 0,
    debt_usdc: int = 0,
    account_data: tuple = ACCOUNT_DATA,
) -> LoggingReader:
    return LoggingReader({
        (AWETH, "balanceOf", (USER,)): aweth,
        (DEBT_WETH, "balanceOf", (USER,)): debt_weth,
        (AUSDC, "balanceOf", (USER,)): ausdc,
        (DEBT_USDC, "balanceOf", (USER,)): debt_usdc,
        (POOL, "getUserAccountData", (USER,)): account_data,
    })


def resolve_ctx(reader: LoggingReader) -> ResolveContext:
    # Checksummed input — ResolveContext lowercases; the fake is keyed
    # by the lowercase form, so any un-lowered read would KeyError.
    return ResolveContext(chain_id="eip155:1", address=USER_INPUT, reader=reader)


def discover_ctx(reader: LoggingReader) -> DiscoveryContext:
    return DiscoveryContext(chain_id="eip155:1", reader=reader)


def full_resolve(reader: LoggingReader, adapter: AaveV3Adapter | None = None):
    adapter = adapter if adapter is not None else TwoMarketAave()
    contracts = adapter.discover(discover_ctx(reader))
    return adapter.resolve(resolve_ctx(reader), contracts)


class TestMarket:
    def test_holds_the_four_fields_verbatim(self):
        market = Market(AWETH, DEBT_WETH, ETH, 18)
        assert market.a_token == AWETH
        assert market.variable_debt_token == DEBT_WETH
        assert market.underlying_caip19 == ETH
        assert market.decimals == 18

    def test_frozen(self):
        with pytest.raises(FrozenInstanceError):
            WETH_MARKET.decimals = 6


class TestAdapterContract:
    def test_pinned_class_attribute_literals(self):
        assert AaveV3Adapter.id == "aave-v3"  # DefiLlama slug — the join key
        assert AaveV3Adapter.chains == frozenset({"eip155:1"})
        assert AaveV3Adapter.pool == POOL

    def test_satisfies_the_position_adapter_protocol(self):
        assert isinstance(TwoMarketAave(), PositionAdapter)
        assert isinstance(SparkFork(), PositionAdapter)


class TestDiscover:
    def test_two_descriptors_per_market(self):
        contracts = TwoMarketAave().discover(discover_ctx(LoggingReader({})))
        assert isinstance(contracts, ContractSet)
        assert len(contracts) == 4

    def test_supply_descriptor_shape(self):
        contracts = TwoMarketAave().discover(discover_ctx(LoggingReader({})))
        [supply] = [d for d in contracts if d.address == AWETH]
        assert supply.adapter_id == "aave-v3"
        assert supply.chain_id == "eip155:1"
        assert supply.category == "lending-supply"
        assert supply.underlyings == (ETH,)
        assert supply.meta == (("pool", POOL),)

    def test_borrow_descriptor_shape(self):
        contracts = TwoMarketAave().discover(discover_ctx(LoggingReader({})))
        [borrow] = [d for d in contracts if d.address == DEBT_USDC]
        assert borrow.adapter_id == "aave-v3"
        assert borrow.category == "lending-borrow"
        assert borrow.underlyings == (USDC,)
        assert borrow.meta == (("pool", POOL),)

    def test_checksummed_market_addresses_emit_lowercase(self):
        class Checksummed(AaveV3Adapter):
            markets = (Market(
                "0x4d5F47FA6A74757f35C14fD3a6Ef8E3C9BC514E8",
                "0xeA51d7853EEFb32b6ee06b1C12E6dcCA88Be0fFE",
                ETH,
                18,
            ),)

        contracts = Checksummed().discover(discover_ctx(LoggingReader({})))
        assert {d.address for d in contracts} == {AWETH, DEBT_WETH}

    def test_discover_is_static_no_reader_calls(self):
        # SPEC §5.1: discovery output is static contract descriptors.
        reader = LoggingReader({})
        TwoMarketAave().discover(discover_ctx(reader))
        assert reader.calls == []

    def test_fork_descriptors_derive_from_subclass_attributes(self):
        contracts = SparkFork().discover(discover_ctx(LoggingReader({})))
        assert len(contracts) == 2
        assert {d.adapter_id for d in contracts} == {"spark"}
        assert {d.address for d in contracts} == {SPWETH, SP_DEBT_WETH}
        assert {d.meta for d in contracts} == {(("pool", SPARK_POOL_LOWER),)}


class TestResolveSupply:
    def test_supply_position_full_shape(self):
        reader = two_market_reader(aweth=3_500_000_000_000_000_000)
        positions = full_resolve(reader)
        assert isinstance(positions, list)
        [position] = positions
        assert position.id == "pos_baff12a5eafb77f6"
        assert position.adapter_id == "aave-v3"
        assert position.chain_id == "eip155:1"
        assert position.contract_address == AWETH
        assert position.kind is PositionKind.APP_TOKEN  # fungible, priceable
        assert position.position_type is PositionType.DEPOSIT
        assert position.protocol_module is ProtocolModule.LENDING
        assert position.group_id == "grp_0f89caffe413b09f"

    def test_supply_underlying_is_raw_supplied_at_rebase_parity(self):
        # aTokens rebase 1:1 — balanceOf IS the underlying amount.
        reader = two_market_reader(aweth=3_500_000_000_000_000_000)
        [position] = full_resolve(reader)
        [underlying] = position.underlyings
        assert underlying.asset_id == ETH
        assert not underlying.asset_id.startswith("ast_")
        assert underlying.quantity == Quantity(3_500_000_000_000_000_000, 18)
        assert underlying.meta_type is MetaType.SUPPLIED
        assert underlying.price is None
        assert underlying.value is None


class TestResolveBorrow:
    def test_borrow_position_full_shape(self):
        reader = two_market_reader(
            debt_usdc=1_234_567,
            account_data=(0, 0, 0, 7900, 7700, 1_050_000_000_000_000_000),
        )
        [position] = full_resolve(reader)
        assert position.id == "pos_1bbfb302ddabf62b"
        assert position.contract_address == DEBT_USDC
        assert position.kind is PositionKind.CONTRACT_POSITION  # not in MetaMask
        assert position.position_type is PositionType.LOAN
        assert position.protocol_module is ProtocolModule.LENDING
        assert position.group_id == "grp_0f89caffe413b09f"
        [underlying] = position.underlyings
        assert underlying.asset_id == USDC
        assert underlying.meta_type is MetaType.BORROWED
        # BORROWED alone carries the sign — the quantity stays positive.
        assert underlying.quantity == Quantity(1_234_567, 6)
        assert underlying.quantity.raw > 0
        assert underlying.price is None and underlying.value is None

    def test_borrow_only_carries_the_group_info(self):
        # First emitted position gets the GroupInfo; here it is the loan.
        reader = two_market_reader(
            debt_usdc=1_234_567,
            account_data=(0, 0, 0, 7900, 7700, 1_050_000_000_000_000_000),
        )
        [position] = full_resolve(reader)
        info = position.group_info
        assert info is not None
        assert info.health_factor == Decimal("1.05")
        assert info.ltv == Decimal("0.77")
        assert info.liquidation_price is None


class TestResolveGrouping:
    def test_group_info_on_first_position_only(self):
        reader = two_market_reader(
            aweth=3_500_000_000_000_000_000, debt_usdc=1_234_567
        )
        positions = full_resolve(reader)
        assert [p.id for p in positions] == [
            "pos_baff12a5eafb77f6", "pos_1bbfb302ddabf62b"
        ]
        assert positions[0].group_info is not None
        assert positions[1].group_info is None
        account_reads = [c for c in reader.calls if c[1] == "getUserAccountData"]
        assert account_reads == [(POOL, "getUserAccountData", (USER,))]

    def test_health_factor_and_ltv_are_exact_decimals(self):
        reader = two_market_reader(aweth=3_500_000_000_000_000_000)
        [position] = full_resolve(reader)
        info = position.group_info
        assert isinstance(info.health_factor, Decimal)
        assert info.health_factor == Decimal("5.8125")  # Quantity(hf_raw, 18)
        assert isinstance(info.ltv, Decimal)
        assert info.ltv == Decimal("0.8")               # Quantity(8000, 4)
        assert info.liquidation_price is None

    def test_both_positions_share_the_pool_group_id(self):
        reader = two_market_reader(
            aweth=3_500_000_000_000_000_000, debt_usdc=1_234_567
        )
        assert {p.group_id for p in full_resolve(reader)} == {
            "grp_0f89caffe413b09f"
        }


class TestResolvePreFilter:
    def test_zero_balances_no_positions_and_no_account_data_read(self):
        reader = two_market_reader()  # every balance zero
        assert full_resolve(reader) == []
        assert all(c[1] != "getUserAccountData" for c in reader.calls)

    def test_restricted_to_supply_descriptor_emits_only_the_supply(self):
        # SPEC §5.2: only surviving descriptors run — no debt-token read.
        reader = LoggingReader({
            (AWETH, "balanceOf", (USER,)): 3_500_000_000_000_000_000,
            (POOL, "getUserAccountData", (USER,)): ACCOUNT_DATA,
        })
        adapter = TwoMarketAave()
        contracts = adapter.discover(discover_ctx(LoggingReader({})))
        restricted = contracts.restrict_to(frozenset({AWETH}))
        positions = adapter.resolve(resolve_ctx(reader), restricted)
        assert [p.id for p in positions] == ["pos_baff12a5eafb77f6"]
        assert {c[0] for c in reader.calls} <= {AWETH, POOL}

    def test_empty_contract_set_resolves_to_nothing(self):
        reader = two_market_reader(aweth=3_500_000_000_000_000_000)
        adapter = TwoMarketAave()
        assert adapter.resolve(resolve_ctx(reader), ContractSet.empty()) == []
        assert reader.calls == []

    def test_zero_and_negative_raw_balances_are_skipped(self):
        # Emit iff raw > 0 — resolve never emits negative quantities.
        reader = two_market_reader(aweth=0, debt_usdc=-5)
        assert full_resolve(reader) == []


class TestResolveBoundaries:
    def test_huge_balance_survives_exact(self):
        huge = 10**77 + 7
        reader = two_market_reader(aweth=huge)
        [position] = full_resolve(reader)
        assert position.underlyings[0].quantity == Quantity(huge, 18)

    def test_fork_resolve_derives_ids_from_subclass_attributes(self):
        reader = LoggingReader({
            (SPWETH, "balanceOf", (USER,)): 2_000_000_000_000_000_000,
            (SP_DEBT_WETH, "balanceOf", (USER,)): 0,
            (SPARK_POOL_LOWER, "getUserAccountData", (USER,)): ACCOUNT_DATA,
        })
        [position] = full_resolve(reader, adapter=SparkFork())
        assert position.adapter_id == "spark"
        assert position.id == "pos_1c76fc3175a40964"
        assert position.group_id == "grp_c02d06abbdf6ef8e"
