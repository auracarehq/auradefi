"""Contract tests for the Uniswap V3 adapter (SPEC §5.4, §4.3).

TickMath and amount vectors are pinned in DECISIONS.md and were derived
here INDEPENDENTLY: a from-scratch python3 implementation of the
canonical Uniswap V3 TickMath integer algorithm (per-bit 128.128
magic-constant products, ratio inverted for tick > 0, >>32 rounded up)
reproduced every hardcoded literal below. The adapter was never run to
produce an expected value. Block-20450000 end-to-end goldens live in
tests/golden/test_positions_uniswap.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from auradefi.errors import ValidationError
from auradefi.money.quantity import Quantity
from auradefi.positions.adapters.amm.uniswap_v3 import (
    MAX_TICK,
    MIN_TICK,
    UniswapV3Adapter,
    amounts_for_liquidity,
    get_sqrt_ratio_at_tick,
)
from auradefi.positions.models import MetaType, Range
from auradefi.positions.protocol import (
    ContractSet,
    DiscoveryContext,
    PositionAdapter,
    ResolveContext,
)

CHAIN = "eip155:1"
MANAGER = "0xc36442b4a4522e871399cd717abdd847ab11fe88"
FACTORY = "0x1f98431c8ad98523631ae4a59f267346ea31f984"
ADDRESS = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
POOL = "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8"  # USDC/WETH 0.3%
USDC_CAIP19 = f"{CHAIN}/erc20:{USDC}"
WETH_CAIP19 = f"{CHAIN}/erc20:{WETH}"

TOKEN_ID = 912345
TICK_LOWER = 193320
TICK_UPPER = 195480
LIQUIDITY = 2_000_000_000_000_000
SQRT_PRICE_IN_RANGE = 1322911675800610514020464994530246
TICK_IN_RANGE = 194470

# Independently derived (canonical TickMath reimplementation):
SQRT_AT_0 = 79228162514264337593543950336
SQRT_AT_MIN = 4295128739
SQRT_AT_MAX = 1461446703485210103287273052203988822378723970342
SQRT_AT_193000 = 1229169572564735153712353661504389
SQRT_AT_193320 = 1248993462782813945679703639744217
SQRT_AT_195480 = 1391430837969698905428982050554204

# Independently derived from the pinned amount formulas:
AMOUNT0_IN_RANGE = 5_898_331_123
AMOUNT1_IN_RANGE = 1_865_958_029_873_234_551
AMOUNT0_BELOW_RANGE = 12_987_087_057
AMOUNT1_ABOVE_RANGE = 3_595_624_855_271_390_557

SRC = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "auradefi"
    / "positions"
    / "adapters"
    / "amm"
    / "uniswap_v3.py"
)


class DictReader:
    """Dict-backed fake keyed (address_lower, fn, args) — no I/O."""

    def __init__(
        self, responses: dict[tuple[str, str, tuple], object]
    ) -> None:
        self._responses = dict(responses)

    def call(
        self, address: str, fn: str, args: tuple[object, ...] = ()
    ) -> object:
        return self._responses[(address.lower(), fn, args)]


def nft_reader(
    *,
    liquidity: int = LIQUIDITY,
    sqrt_price_x96: int = SQRT_PRICE_IN_RANGE,
    tick: int = TICK_IN_RANGE,
    tokens_owed0: int = 0,
    tokens_owed1: int = 0,
) -> DictReader:
    """One USDC/WETH 0.3% NFT under the manager, parameterised."""
    return DictReader(
        {
            (MANAGER, "balanceOf", (ADDRESS,)): 1,
            (MANAGER, "tokenOfOwnerByIndex", (ADDRESS, 0)): TOKEN_ID,
            (MANAGER, "positions", (TOKEN_ID,)): (
                0,
                "0x0000000000000000000000000000000000000000",
                USDC,
                WETH,
                3000,
                TICK_LOWER,
                TICK_UPPER,
                liquidity,
                0,
                0,
                tokens_owed0,
                tokens_owed1,
            ),
            (FACTORY, "getPool", (USDC, WETH, 3000)): POOL,
            (POOL, "slot0", ()): (sqrt_price_x96, tick, 0, 1, 1, 0, True),
            (USDC, "decimals", ()): 6,
            (WETH, "decimals", ()): 18,
        }
    )


def manager_descriptor():
    from auradefi.positions.protocol import ContractDescriptor

    return ContractDescriptor(
        adapter_id="uniswap-v3",
        chain_id=CHAIN,
        address=MANAGER,
        category="amm-nft-manager",
    )


def resolve_with(reader: DictReader):
    ctx = ResolveContext(chain_id=CHAIN, address=ADDRESS, reader=reader)
    return UniswapV3Adapter().resolve(
        ctx, ContractSet.of(manager_descriptor())
    )


class TestAdapterIdentity:
    def test_pinned_class_attributes(self):
        assert UniswapV3Adapter.id == "uniswap-v3"
        assert UniswapV3Adapter.chains == frozenset({"eip155:1"})
        assert (
            UniswapV3Adapter.position_manager
            == "0xc36442b4a4522e871399cd717abdd847ab11fe88"
        )
        assert (
            UniswapV3Adapter.factory_address
            == "0x1f98431c8ad98523631ae4a59f267346ea31f984"
        )

    def test_satisfies_the_position_adapter_protocol(self):
        assert isinstance(UniswapV3Adapter(), PositionAdapter)

    def test_tick_bound_constants(self):
        assert MIN_TICK == -887272
        assert MAX_TICK == 887272


class TestTickMath:
    def test_pinned_vector_tick_zero(self):
        assert get_sqrt_ratio_at_tick(0) == SQRT_AT_0

    def test_pinned_vector_min_tick(self):
        assert get_sqrt_ratio_at_tick(-887272) == SQRT_AT_MIN

    def test_pinned_vector_max_tick(self):
        assert get_sqrt_ratio_at_tick(887272) == SQRT_AT_MAX

    def test_pinned_vector_tick_193320(self):
        assert get_sqrt_ratio_at_tick(193320) == SQRT_AT_193320

    def test_pinned_vector_tick_195480(self):
        assert get_sqrt_ratio_at_tick(195480) == SQRT_AT_195480

    def test_pinned_vector_tick_193000(self):
        assert get_sqrt_ratio_at_tick(193000) == SQRT_AT_193000

    def test_tick_above_max_raises_validation_error(self):
        with pytest.raises(ValidationError):
            get_sqrt_ratio_at_tick(887273)

    def test_tick_below_min_raises_validation_error(self):
        with pytest.raises(ValidationError):
            get_sqrt_ratio_at_tick(-887273)


class TestAmountsForLiquidity:
    def test_in_range_amounts(self):
        assert amounts_for_liquidity(
            LIQUIDITY,
            SQRT_PRICE_IN_RANGE,
            TICK_IN_RANGE,
            TICK_LOWER,
            TICK_UPPER,
        ) == (AMOUNT0_IN_RANGE, AMOUNT1_IN_RANGE)

    def test_below_range_is_all_token0(self):
        assert amounts_for_liquidity(
            LIQUIDITY, SQRT_AT_193000, 193000, TICK_LOWER, TICK_UPPER
        ) == (AMOUNT0_BELOW_RANGE, 0)

    def test_above_range_is_all_token1(self):
        assert amounts_for_liquidity(
            LIQUIDITY,
            SQRT_AT_195480,
            TICK_UPPER + 100,
            TICK_LOWER,
            TICK_UPPER,
        ) == (0, AMOUNT1_ABOVE_RANGE)

    def test_tick_equal_to_upper_uses_the_above_range_branch(self):
        # Strict upper bound: tick == tick_upper is OUT of range.
        assert amounts_for_liquidity(
            LIQUIDITY, SQRT_AT_195480, TICK_UPPER, TICK_LOWER, TICK_UPPER
        ) == (0, AMOUNT1_ABOVE_RANGE)

    def test_tick_equal_to_lower_uses_the_in_range_branch(self):
        # Inclusive lower bound: tick == tick_lower is IN range.
        amount0, amount1 = amounts_for_liquidity(
            LIQUIDITY, SQRT_AT_193320, TICK_LOWER, TICK_LOWER, TICK_UPPER
        )
        expected0 = (
            (LIQUIDITY << 96)
            * (SQRT_AT_195480 - SQRT_AT_193320)
            // SQRT_AT_195480
        ) // SQRT_AT_193320
        assert (amount0, amount1) == (expected0, 0)

    def test_zero_liquidity_yields_zero_amounts(self):
        assert amounts_for_liquidity(
            0, SQRT_PRICE_IN_RANGE, TICK_IN_RANGE, TICK_LOWER, TICK_UPPER
        ) == (0, 0)


class TestDiscover:
    def test_emits_the_single_manager_descriptor(self):
        ctx = DiscoveryContext(chain_id=CHAIN, reader=DictReader({}))
        contracts = UniswapV3Adapter().discover(ctx)
        [descriptor] = list(contracts)
        assert descriptor.adapter_id == "uniswap-v3"
        assert descriptor.chain_id == CHAIN
        assert descriptor.address == MANAGER
        assert descriptor.category == "amm-nft-manager"


class TestResolveSkipRule:
    def test_empty_nft_skipped_iff_no_liquidity_and_no_fees(self):
        reader = nft_reader(liquidity=0, tokens_owed0=0, tokens_owed1=0)
        assert resolve_with(reader) == []

    def test_fees_only_nft_emits_claimable_only_position(self):
        reader = nft_reader(liquidity=0, tokens_owed0=125_000_000)
        [position] = resolve_with(reader)
        assert tuple(
            (u.meta_type, u.asset_id, u.quantity)
            for u in position.underlyings
        ) == ((MetaType.CLAIMABLE, USDC_CAIP19, Quantity(125_000_000, 6)),)


class TestResolveOutOfRange:
    def test_below_range_supplies_token0_only(self):
        # slot0 tick 193000 < tickLower: amount1 side absent,
        # amount0 == 12_987_087_057 (pinned below-range formula,
        # derived independently), in_range False.
        reader = nft_reader(sqrt_price_x96=SQRT_AT_193000, tick=193000)
        [position] = resolve_with(reader)
        assert position.range == Range(TICK_LOWER, TICK_UPPER, False)
        assert tuple(
            (u.meta_type, u.asset_id, u.quantity)
            for u in position.underlyings
        ) == (
            (
                MetaType.SUPPLIED,
                USDC_CAIP19,
                Quantity(AMOUNT0_BELOW_RANGE, 6),
            ),
        )

    def test_tick_at_upper_bound_is_out_of_range_token1_only(self):
        reader = nft_reader(sqrt_price_x96=SQRT_AT_195480, tick=TICK_UPPER)
        [position] = resolve_with(reader)
        assert position.range == Range(TICK_LOWER, TICK_UPPER, False)
        assert tuple(
            (u.meta_type, u.asset_id, u.quantity)
            for u in position.underlyings
        ) == (
            (
                MetaType.SUPPLIED,
                WETH_CAIP19,
                Quantity(AMOUNT1_ABOVE_RANGE, 18),
            ),
        )


class TestModuleImports:
    def test_never_imports_httpx_portfolio_decode_or_ledger(self):
        tree = ast.parse(SRC.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        banned = {"httpx", "portfolio", "decode", "ledger"}
        offenders = {
            name for name in imported if set(name.split(".")) & banned
        }
        assert not offenders, f"forbidden imports: {sorted(offenders)}"
