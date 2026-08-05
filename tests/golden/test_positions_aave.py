"""Aave v3 golden fixture pinned to block 20_450_000 (SPEC rule #5).

Per-adapter golden fixtures pinned to a block height are non-negotiable:
LlamaFolio shipped zero tests in 3,422 files, Zapper Studio three in
1,010 fetchers: both died of silently wrong numbers. A number changes
here → this file goes red.

State at Ethereum mainnet block 20_450_000 (SPEC §6.3's worked example:
supply 10 ETH, borrow 5,000 USDC):

    aWETH.balanceOf(user)          = 10_000_000_000_000_000_000  (10 ETH)
    variableDebtUSDC.balanceOf     = 5_000_000_000               (5,000 USDC)
    Pool.getUserAccountData(user)  = (.., .., .., .., 8000, 5_812_500_000_000_000_000)
                                      → ltv 0.8000, health factor 5.8125

Golden literals derived independently via python3 -c over the pinned
DECISIONS.md algorithms (sha256 preimage "{adapter}|{chain}|{contract}|"
for positions, "{adapter}|{chain}|{pool}" for the group: 0x lowercased),
never from the code under test:

    pos_baff12a5eafb77f6  aave-v3|eip155:1|0x4d5f47fa6a74757f35c14fd3a6ef8e3c9bc514e8|
    pos_1bbfb302ddabf62b  aave-v3|eip155:1|0x72e95b8931767c79ba4eee721354d6e99a61d004|
    grp_0f89caffe413b09f  aave-v3|eip155:1|0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2
"""

from __future__ import annotations

from decimal import Decimal

from auradefi.money.quantity import Quantity
from auradefi.positions.adapters.lending.aave import AaveV3Adapter, Market
from auradefi.positions.models import (
    MetaType,
    PositionKind,
    PositionType,
    ProtocolModule,
)
from auradefi.positions.protocol import DiscoveryContext, ResolveContext

BLOCK = 20_450_000
CHAIN = "eip155:1"
POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
AWETH = "0x4d5f47fa6a74757f35c14fd3a6ef8e3c9bc514e8"
DEBT_WETH = "0xea51d7853eefb32b6ee06b1c12e6dcca88be0ffe"
AUSDC = "0x98c23e9d8f34fefb1b7bd6a91b7ff122f4e16f5c"
DEBT_USDC = "0x72e95b8931767c79ba4eee721354d6e99a61d004"
ETH = "eip155:1/slip44:60"
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USER = "0x00000000000000000000000000000000000a11ce"

SUPPLY_ID = "pos_baff12a5eafb77f6"
BORROW_ID = "pos_1bbfb302ddabf62b"
GROUP_ID = "grp_0f89caffe413b09f"


class MainnetAaveV3(AaveV3Adapter):
    """Aave v3 Ethereum mainnet over the WETH and USDC reserves."""

    markets = (
        Market(AWETH, DEBT_WETH, ETH, 18),
        Market(AUSDC, DEBT_USDC, USDC, 6),
    )


class BlockReader:
    """Chain state frozen at block 20_450_000, with a call log."""

    RESPONSES = {
        (AWETH, "balanceOf", (USER,)): 10_000_000_000_000_000_000,
        (DEBT_WETH, "balanceOf", (USER,)): 0,
        (AUSDC, "balanceOf", (USER,)): 0,
        (DEBT_USDC, "balanceOf", (USER,)): 5_000_000_000,
        # (tc, td, ab, clt, ltv_bp, hf_raw); first four unused.
        (POOL, "getUserAccountData", (USER,)): (
            3_584_250_000_000, 500_000_000_000, 2_367_400_000_000,
            8250, 8000, 5_812_500_000_000_000_000,
        ),
    }

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple]] = []

    def call(self, address: str, fn: str, args: tuple[object, ...] = ()) -> object:
        self.calls.append((address, fn, args))
        return self.RESPONSES[(address, fn, args)]


def _resolved() -> tuple[list, BlockReader]:
    adapter = MainnetAaveV3()
    reader = BlockReader()
    contracts = adapter.discover(DiscoveryContext(chain_id=CHAIN, reader=reader))
    ctx = ResolveContext(
        chain_id=CHAIN, address=USER, reader=reader, block_number=BLOCK
    )
    return adapter.resolve(ctx, contracts), reader


class TestAaveV3Block20450000:
    """The pinned Aave v3 adapter output at mainnet block 20_450_000."""

    def test_exactly_two_raw_positions_supply_then_borrow(self):
        positions, _ = _resolved()
        assert [p.id for p in positions] == [SUPPLY_ID, BORROW_ID]

    def test_supply_position_golden(self):
        positions, _ = _resolved()
        supply = positions[0]
        assert supply.id == SUPPLY_ID
        assert supply.adapter_id == "aave-v3"
        assert supply.chain_id == CHAIN
        assert supply.contract_address == AWETH
        assert supply.kind is PositionKind.APP_TOKEN
        assert supply.position_type is PositionType.DEPOSIT
        assert supply.protocol_module is ProtocolModule.LENDING
        assert supply.group_id == GROUP_ID
        [underlying] = supply.underlyings
        assert underlying.asset_id == ETH
        assert underlying.quantity == Quantity(10 * 10**18, 18)
        assert underlying.meta_type is MetaType.SUPPLIED

    def test_borrow_position_golden(self):
        positions, _ = _resolved()
        borrow = positions[1]
        assert borrow.id == BORROW_ID
        assert borrow.adapter_id == "aave-v3"
        assert borrow.chain_id == CHAIN
        assert borrow.contract_address == DEBT_USDC
        assert borrow.kind is PositionKind.CONTRACT_POSITION
        assert borrow.position_type is PositionType.LOAN
        assert borrow.protocol_module is ProtocolModule.LENDING
        assert borrow.group_id == GROUP_ID
        [underlying] = borrow.underlyings
        assert underlying.asset_id == USDC
        assert underlying.quantity == Quantity(5_000_000_000, 6)
        assert underlying.meta_type is MetaType.BORROWED
        # BORROWED alone carries the sign; the raw quantity is positive.
        assert underlying.quantity.raw > 0

    def test_group_info_on_exactly_one_position(self):
        positions, _ = _resolved()
        infos = [p.group_info for p in positions if p.group_info is not None]
        assert len(infos) == 1
        assert positions[0].group_info is not None  # the FIRST emitted
        [info] = infos
        assert isinstance(info.health_factor, Decimal)
        assert info.health_factor == Decimal("5.8125")  # Quantity(hf_raw, 18)
        assert isinstance(info.ltv, Decimal)
        assert info.ltv == Decimal("0.8")               # Quantity(8000, 4)
        assert info.liquidation_price is None

    def test_account_data_read_exactly_once_from_the_pool(self):
        _, reader = _resolved()
        reads = [c for c in reader.calls if c[1] == "getUserAccountData"]
        assert reads == [(POOL, "getUserAccountData", (USER,))]

    def test_all_underlyings_raw_and_canonically_identified(self):
        positions, _ = _resolved()
        for position in positions:
            for underlying in position.underlyings:
                assert underlying.price is None      # raw: re-drill later
                assert underlying.value is None
                assert not underlying.asset_id.startswith("ast_")
        assert {u.asset_id for p in positions for u in p.underlyings} == {
            ETH, USDC
        }
