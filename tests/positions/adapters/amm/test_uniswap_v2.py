"""Contract tests for the Uniswap V2 adapter (SPEC §5.4; DECISIONS.md).

The pinned pro-rata is burn semantics with integer floor::

    underlying_raw_i = lp_raw * reserve_i_raw // total_supply_raw

Golden literals here were derived independently with python3 (exact
integer arithmetic over the pinned algorithm), never by running the
adapter. Block-20450000 end-to-end goldens live in
tests/golden/test_positions_uniswap.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

from auradefi.money.quantity import Quantity
from auradefi.positions.adapters.amm.uniswap_v2 import UniswapV2Adapter
from auradefi.positions.models import MetaType
from auradefi.positions.protocol import (
    ContractDescriptor,
    ContractSet,
    DiscoveryContext,
    PositionAdapter,
    ResolveContext,
)

CHAIN = "eip155:1"
FACTORY = "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f"
ADDRESS = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
PAIR_USDC_WETH = "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"
PAIR_WETH_USDT = "0x0d4a11d5eeaac28ec3f61d100daf4d40471f1852"

USDC_CAIP19 = f"{CHAIN}/erc20:{USDC}"
WETH_CAIP19 = f"{CHAIN}/erc20:{WETH}"
USDT_CAIP19 = f"{CHAIN}/erc20:{USDT}"

RESERVE0 = 52_000_000_000_000
RESERVE1 = 14_500_000_000_000_000_000_000
TOTAL_SUPPLY = 850_000_000_000_000_000

SRC = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "auradefi"
    / "positions"
    / "adapters"
    / "amm"
    / "uniswap_v2.py"
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


def pair_reader(lp_raw: int) -> DictReader:
    """USDC/WETH pair fixture with a caller-chosen LP balance."""
    return DictReader(
        {
            (PAIR_USDC_WETH, "balanceOf", (ADDRESS,)): lp_raw,
            (PAIR_USDC_WETH, "totalSupply", ()): TOTAL_SUPPLY,
            (PAIR_USDC_WETH, "getReserves", ()): (
                RESERVE0,
                RESERVE1,
                1_722_470_000,
            ),
            (PAIR_USDC_WETH, "token0", ()): USDC,
            (PAIR_USDC_WETH, "token1", ()): WETH,
            (USDC, "decimals", ()): 6,
            (WETH, "decimals", ()): 18,
        }
    )


def pair_descriptor() -> ContractDescriptor:
    return ContractDescriptor(
        adapter_id="uniswap-v2",
        chain_id=CHAIN,
        address=PAIR_USDC_WETH,
        category="amm-pair",
        underlyings=(USDC_CAIP19, WETH_CAIP19),
    )


def resolve_pair(lp_raw: int):
    ctx = ResolveContext(
        chain_id=CHAIN, address=ADDRESS, reader=pair_reader(lp_raw)
    )
    return UniswapV2Adapter().resolve(ctx, ContractSet.of(pair_descriptor()))


class TestAdapterIdentity:
    def test_pinned_class_attributes(self):
        assert UniswapV2Adapter.id == "uniswap-v2"
        assert UniswapV2Adapter.chains == frozenset({"eip155:1"})
        assert (
            UniswapV2Adapter.factory_address
            == "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f"
        )

    def test_satisfies_the_position_adapter_protocol(self):
        assert isinstance(UniswapV2Adapter(), PositionAdapter)


class TestForkSubclass:
    """SPEC §5.4: Zapper's UniV2 fork was a 3-attribute subclass."""

    def _fork(self) -> type[UniswapV2Adapter]:
        class SushiSwapAdapter(UniswapV2Adapter):
            id = "sushiswap"
            chains = frozenset({"eip155:1"})
            factory_address = "0xc0aee478e3658e2610c5f7a4a2e1777ce9e4f2ac"

        return SushiSwapAdapter

    def test_fork_overrides_exactly_the_three_attributes(self):
        fork = self._fork()
        overridden = {
            name
            for name in vars(fork)
            if not name.startswith("__") and name != "_abc_impl"
        }
        assert overridden == {"id", "chains", "factory_address"}

    def test_fork_inherits_discover_and_resolve_bodies(self):
        fork = self._fork()
        assert fork.discover is UniswapV2Adapter.discover
        assert fork.resolve is UniswapV2Adapter.resolve

    def test_fork_discovers_from_its_own_factory_with_its_own_id(self):
        fork = self._fork()
        sushi_factory = "0xc0aee478e3658e2610c5f7a4a2e1777ce9e4f2ac"
        sushi_pair = "0x397ff1542f962076d0bfe58ea045ffa2d347aca0"
        reader = DictReader(
            {
                (sushi_factory, "allPairsLength", ()): 1,
                (sushi_factory, "allPairs", (0,)): sushi_pair,
                (sushi_pair, "token0", ()): USDC,
                (sushi_pair, "token1", ()): WETH,
            }
        )
        contracts = fork().discover(
            DiscoveryContext(chain_id=CHAIN, reader=reader)
        )
        [descriptor] = list(contracts)
        assert descriptor.adapter_id == "sushiswap"
        assert descriptor.address == sushi_pair


class TestDiscover:
    def _two_pair_reader(self) -> DictReader:
        # Checksum-cased token replies prove canonical lowercasing.
        return DictReader(
            {
                (FACTORY, "allPairsLength", ()): 2,
                (FACTORY, "allPairs", (0,)): PAIR_USDC_WETH,
                (FACTORY, "allPairs", (1,)): PAIR_WETH_USDT,
                (PAIR_USDC_WETH, "token0", ()): (
                    "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
                ),
                (PAIR_USDC_WETH, "token1", ()): (
                    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
                ),
                (PAIR_WETH_USDT, "token0", ()): (
                    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
                ),
                (PAIR_WETH_USDT, "token1", ()): (
                    "0xdAC17F958D2ee523a2206206994597C13D831ec7"
                ),
            }
        )

    def _discover(self) -> ContractSet:
        ctx = DiscoveryContext(chain_id=CHAIN, reader=self._two_pair_reader())
        return UniswapV2Adapter().discover(ctx)

    def test_two_pairs_yield_two_descriptors(self):
        assert len(self._discover()) == 2

    def test_descriptors_are_amm_pairs_with_lowercase_addresses(self):
        for descriptor in self._discover():
            assert descriptor.adapter_id == "uniswap-v2"
            assert descriptor.chain_id == CHAIN
            assert descriptor.category == "amm-pair"
            assert descriptor.address == descriptor.address.lower()
        assert {d.address for d in self._discover()} == {
            PAIR_USDC_WETH,
            PAIR_WETH_USDT,
        }

    def test_underlyings_are_canonical_lowercase_erc20_caip19(self):
        by_address = {d.address: d for d in self._discover()}
        assert by_address[PAIR_USDC_WETH].underlyings == (
            USDC_CAIP19,
            WETH_CAIP19,
        )
        assert by_address[PAIR_WETH_USDT].underlyings == (
            WETH_CAIP19,
            USDT_CAIP19,
        )


class TestResolve:
    def test_zero_lp_balance_emits_no_position(self):
        assert resolve_pair(0) == []

    def test_pro_rata_is_integer_floor_never_rounding(self):
        # lp=1: token0 exact quotient is 0.0000611764705…  -> floor 0
        #       token1 exact quotient is 17058.8235294117… -> floor 17058
        # (rounding would give 17059 — the discriminator).
        # Derived independently:
        #   1 * 52_000_000_000_000 // 850_000_000_000_000_000 == 0
        #   1 * 14_500_000_000_000_000_000_000
        #       // 850_000_000_000_000_000 == 17058
        [position] = resolve_pair(1)
        assert position.underlyings[0].quantity == Quantity(0, 6)
        assert position.underlyings[1].quantity == Quantity(17058, 18)

    def test_underlyings_are_supplied_and_raw(self):
        [position] = resolve_pair(1)
        for underlying in position.underlyings:
            assert underlying.meta_type is MetaType.SUPPLIED
            assert underlying.price is None
            assert underlying.value is None
        assert position.value is None

    def test_underlying_asset_ids_in_token0_token1_order(self):
        [position] = resolve_pair(1)
        assert tuple(u.asset_id for u in position.underlyings) == (
            USDC_CAIP19,
            WETH_CAIP19,
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
            name
            for name in imported
            if set(name.split(".")) & banned
        }
        assert not offenders, f"forbidden imports: {sorted(offenders)}"
