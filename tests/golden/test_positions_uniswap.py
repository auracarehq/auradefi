"""Block-20450000 golden vectors for the Uniswap adapters (SPEC rule #5).

RAW integers only. USD goldens live in drill (SPEC §5.3: raw persists,
re-drills against fresh prices without an RPC). Every literal below was
derived INDEPENDENTLY with python3: sha256 preimages for the pinned id
algorithms, exact integer arithmetic for the pinned pro-rata, and a
from-scratch reimplementation of canonical TickMath for the V3 amounts,
never by running the adapters:

    pos_e463a531f5d6a400 = "pos_" + sha256(
        "uniswap-v2|eip155:1|0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc|"
    )[:16]
    grp_b351d79d77bc24eb = "grp_" + sha256(
        "uniswap-v2|eip155:1|0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"
    )[:16]
    pos_447985e390bf1d89 = "pos_" + sha256(
        "uniswap-v3|eip155:1|0xc36442b4a4522e871399cd717abdd847ab11fe88"
        "|912345"
    )[:16]
    grp_9b813f4a0ae43e5b = "grp_" + sha256(
        "uniswap-v3|eip155:1|0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8"
    )[:16]

    V2: 850_000_000_000_000 * 52_000_000_000_000
            // 850_000_000_000_000_000 == 52_000_000_000
        850_000_000_000_000 * 14_500_000_000_000_000_000_000
            // 850_000_000_000_000_000 == 14_500_000_000_000_000_000

    V3: sqrtA = TickMath(193320) = 1248993462782813945679703639744217
        sqrtB = TickMath(195480) = 1391430837969698905428982050554204
        sqrtP = 1322911675800610514020464994530246 (tick 194470)
        amount0 = ((L<<96)*(sqrtB-sqrtP)//sqrtB)//sqrtP == 5_898_331_123
        amount1 = L*(sqrtP-sqrtA)//2**96 == 1_865_958_029_873_234_551
"""

from __future__ import annotations

from auradefi.money.quantity import Quantity
from auradefi.positions.adapters.amm.uniswap_v2 import UniswapV2Adapter
from auradefi.positions.adapters.amm.uniswap_v3 import UniswapV3Adapter
from auradefi.positions.models import (
    MetaType,
    PositionKind,
    PositionType,
    ProtocolModule,
    Range,
)
from auradefi.positions.protocol import (
    ContractDescriptor,
    ContractSet,
    ResolveContext,
)

BLOCK = 20_450_000
CHAIN = "eip155:1"
ADDRESS = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC_CAIP19 = f"{CHAIN}/erc20:{USDC}"
WETH_CAIP19 = f"{CHAIN}/erc20:{WETH}"

V2_PAIR = "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"
V3_MANAGER = "0xc36442b4a4522e871399cd717abdd847ab11fe88"
V3_FACTORY = "0x1f98431c8ad98523631ae4a59f267346ea31f984"
V3_POOL = "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8"


class DictReader:
    """Dict-backed fake keyed (address_lower, fn, args), no I/O."""

    def __init__(
        self, responses: dict[tuple[str, str, tuple], object]
    ) -> None:
        self._responses = dict(responses)

    def call(
        self, address: str, fn: str, args: tuple[object, ...] = ()
    ) -> object:
        return self._responses[(address.lower(), fn, args)]


class TestUniswapV2Block20450000:
    """USDC/WETH pair state at block 20_450_000. RAW integers only."""

    def _position(self):
        reader = DictReader(
            {
                (V2_PAIR, "balanceOf", (ADDRESS,)): 850_000_000_000_000,
                (V2_PAIR, "totalSupply", ()): 850_000_000_000_000_000,
                (V2_PAIR, "getReserves", ()): (
                    52_000_000_000_000,
                    14_500_000_000_000_000_000_000,
                    1_722_470_000,
                ),
                (V2_PAIR, "token0", ()): USDC,
                (V2_PAIR, "token1", ()): WETH,
                (USDC, "decimals", ()): 6,
                (WETH, "decimals", ()): 18,
            }
        )
        descriptor = ContractDescriptor(
            adapter_id="uniswap-v2",
            chain_id=CHAIN,
            address=V2_PAIR,
            category="amm-pair",
            underlyings=(USDC_CAIP19, WETH_CAIP19),
        )
        ctx = ResolveContext(
            chain_id=CHAIN,
            address=ADDRESS,
            reader=reader,
            block_number=BLOCK,
        )
        positions = UniswapV2Adapter().resolve(
            ctx, ContractSet.of(descriptor)
        )
        assert len(positions) == 1
        return positions[0]

    def test_pinned_position_and_group_ids(self):
        position = self._position()
        assert position.id == "pos_e463a531f5d6a400"
        assert position.group_id == "grp_b351d79d77bc24eb"

    def test_classification(self):
        position = self._position()
        assert position.adapter_id == "uniswap-v2"
        assert position.chain_id == CHAIN
        assert position.contract_address == V2_PAIR
        assert position.kind is PositionKind.APP_TOKEN
        assert position.position_type is PositionType.DEPOSIT
        assert position.protocol_module is ProtocolModule.LIQUIDITY_POOL

    def test_golden_pro_rata_underlyings(self):
        position = self._position()
        assert tuple(
            (u.meta_type, u.asset_id, u.quantity)
            for u in position.underlyings
        ) == (
            (MetaType.SUPPLIED, USDC_CAIP19, Quantity(52_000_000_000, 6)),
            (
                MetaType.SUPPLIED,
                WETH_CAIP19,
                Quantity(14_500_000_000_000_000_000, 18),
            ),
        )

    def test_output_is_raw_no_prices_no_values(self):
        position = self._position()
        for underlying in position.underlyings:
            assert underlying.price is None
            assert underlying.value is None
        assert position.value is None


class TestUniswapV3Block20450000:
    """NFT 912345 (USDC/WETH 0.3%) at block 20_450_000. RAW only."""

    def _position(self):
        reader = DictReader(
            {
                (V3_MANAGER, "balanceOf", (ADDRESS,)): 1,
                (V3_MANAGER, "tokenOfOwnerByIndex", (ADDRESS, 0)): 912345,
                (V3_MANAGER, "positions", (912345,)): (
                    0,
                    "0x0000000000000000000000000000000000000000",
                    USDC,
                    WETH,
                    3000,
                    193320,
                    195480,
                    2_000_000_000_000_000,
                    0,
                    0,
                    125_000_000,
                    40_000_000_000_000_000,
                ),
                (V3_FACTORY, "getPool", (USDC, WETH, 3000)): V3_POOL,
                (V3_POOL, "slot0", ()): (
                    1322911675800610514020464994530246,
                    194470,
                    0,
                    1,
                    1,
                    0,
                    True,
                ),
                (USDC, "decimals", ()): 6,
                (WETH, "decimals", ()): 18,
            }
        )
        descriptor = ContractDescriptor(
            adapter_id="uniswap-v3",
            chain_id=CHAIN,
            address=V3_MANAGER,
            category="amm-nft-manager",
        )
        ctx = ResolveContext(
            chain_id=CHAIN,
            address=ADDRESS,
            reader=reader,
            block_number=BLOCK,
        )
        positions = UniswapV3Adapter().resolve(
            ctx, ContractSet.of(descriptor)
        )
        assert len(positions) == 1
        return positions[0]

    def test_pinned_position_and_group_ids(self):
        position = self._position()
        assert position.id == "pos_447985e390bf1d89"
        assert position.group_id == "grp_9b813f4a0ae43e5b"

    def test_classification(self):
        position = self._position()
        assert position.adapter_id == "uniswap-v3"
        assert position.chain_id == CHAIN
        assert position.contract_address == V3_MANAGER
        assert position.kind is PositionKind.CONTRACT_POSITION
        assert position.position_type is PositionType.DEPOSIT
        assert position.protocol_module is ProtocolModule.LIQUIDITY_POOL

    def test_range_is_in_range(self):
        assert self._position().range == Range(193320, 195480, True)

    def test_golden_supplied_and_claimable_underlyings(self):
        position = self._position()
        assert tuple(
            (u.meta_type, u.asset_id, u.quantity)
            for u in position.underlyings
        ) == (
            (MetaType.SUPPLIED, USDC_CAIP19, Quantity(5_898_331_123, 6)),
            (
                MetaType.SUPPLIED,
                WETH_CAIP19,
                Quantity(1_865_958_029_873_234_551, 18),
            ),
            (MetaType.CLAIMABLE, USDC_CAIP19, Quantity(125_000_000, 6)),
            (
                MetaType.CLAIMABLE,
                WETH_CAIP19,
                Quantity(40_000_000_000_000_000, 18),
            ),
        )

    def test_unclaimed_fees_are_the_claimable_underlyings(self):
        fees = self._position().unclaimed_fees
        assert tuple((u.asset_id, u.quantity) for u in fees) == (
            (USDC_CAIP19, Quantity(125_000_000, 6)),
            (WETH_CAIP19, Quantity(40_000_000_000_000_000, 18)),
        )

    def test_output_is_raw_no_prices_no_values(self):
        position = self._position()
        for underlying in position.underlyings:
            assert underlying.price is None
            assert underlying.value is None
        assert position.value is None
