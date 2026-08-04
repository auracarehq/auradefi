"""Uniswap V2 adapter (SPEC §5.4; DECISIONS.md pinned pro-rata).

The bar is Zapper's production UniV2 integration: 15 lines, zero
methods — a fork is a subclass overriding exactly ``id``, ``chains``
and ``factory_address``. All chain reads go through ``ctx.reader``
(the only chain-read seam in ``positions/`` — no HTTP client here,
ever).

``discover`` (SPEC §5.1, address-blind)::

    n = call(factory, 'allPairsLength')
    for i in range(n):
        pair   = call(factory, 'allPairs', (i,))
        token0 = call(pair, 'token0'); token1 = call(pair, 'token1')

emitting one ``ContractDescriptor`` per pair — ``category='amm-pair'``,
``underlyings`` the two canonical CAIP-19 strings
``f'{chain_id}/erc20:{token.lower()}'`` in (token0, token1) order.

``resolve`` (SPEC §5.4) per descriptor::

    lp_raw = call(pair, 'balanceOf', (ctx.address,))   # skip pair if 0
    total_supply = call(pair, 'totalSupply')
    (r0, r1, _) = call(pair, 'getReserves')
    d_i = call(token_i, 'decimals')

PINNED pro-rata (DECISIONS.md, burn semantics — integer floor, never
rounding)::

    underlying_raw_i = lp_raw * r_i // total_supply

emitting ONE RAW position per held pair: ``kind=APP_TOKEN`` (an LP
token is fungible and priceable — SPEC §4.3), ``position_type=DEPOSIT``,
``protocol_module=LIQUIDITY_POOL``, ``id=position_id(id, chain, pair)``,
``group_id=group_id_for(id, chain, pair)``, underlyings two ``SUPPLIED``
``Quantity(raw_i, d_i)`` in (token0, token1) order with NO prices
(SPEC §5.3: raw persists, drill re-prices without an RPC).
"""

from __future__ import annotations

from auradefi.money.quantity import Quantity
from auradefi.positions.models import (
    MetaType,
    Position,
    PositionKind,
    PositionType,
    ProtocolModule,
    Underlying,
    group_id_for,
    position_id,
)
from auradefi.positions.protocol import (
    ContractDescriptor,
    ContractSet,
    DiscoveryContext,
    ResolveContext,
)


class UniswapV2Adapter:
    """Uniswap V2 on Ethereum mainnet. Forks override the three attrs."""

    id: str = "uniswap-v2"
    chains: frozenset[str] = frozenset({"eip155:1"})
    factory_address: str = "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f"

    def discover(self, ctx: DiscoveryContext) -> ContractSet:
        """Enumerate every pair on ``factory_address`` via ``ctx.reader``.

        Address-blind (SPEC §5.1). One ``category='amm-pair'``
        descriptor per pair, ``underlyings`` the two canonical
        lowercase CAIP-19 erc20 ids in (token0, token1) order.
        """
        pair_count = ctx.reader.call(self.factory_address, "allPairsLength")
        descriptors = []
        for index in range(pair_count):
            pair = ctx.reader.call(self.factory_address, "allPairs", (index,))
            token0 = ctx.reader.call(pair, "token0")
            token1 = ctx.reader.call(pair, "token1")
            descriptors.append(
                ContractDescriptor(
                    adapter_id=self.id,
                    chain_id=ctx.chain_id,
                    address=pair,
                    category="amm-pair",
                    underlyings=(
                        f"{ctx.chain_id}/erc20:{token0.lower()}",
                        f"{ctx.chain_id}/erc20:{token1.lower()}",
                    ),
                )
            )
        return ContractSet.of(*descriptors)

    def resolve(
        self, ctx: ResolveContext, contracts: ContractSet
    ) -> list[Position]:
        """Attach pro-rata RAW balances for ``ctx.address``.

        Per descriptor: skip if ``balanceOf`` is 0; else emit one
        APP_TOKEN/DEPOSIT/LIQUIDITY_POOL position whose two SUPPLIED
        underlyings are ``lp_raw * r_i // total_supply`` (pinned
        integer floor), unpriced.
        """
        positions: list[Position] = []
        for descriptor in contracts:
            pair = descriptor.address
            lp_raw = ctx.reader.call(pair, "balanceOf", (ctx.address,))
            if lp_raw == 0:
                continue
            total_supply = ctx.reader.call(pair, "totalSupply")
            reserve0, reserve1, _ = ctx.reader.call(pair, "getReserves")
            underlyings = []
            for asset_id, reserve in zip(
                descriptor.underlyings, (reserve0, reserve1), strict=True
            ):
                token = asset_id.rsplit(":", 1)[1]
                decimals = ctx.reader.call(token, "decimals")
                underlyings.append(
                    Underlying(
                        asset_id=asset_id,
                        quantity=Quantity(
                            lp_raw * reserve // total_supply, decimals
                        ),
                        meta_type=MetaType.SUPPLIED,
                    )
                )
            positions.append(
                Position(
                    id=position_id(self.id, ctx.chain_id, pair),
                    adapter_id=self.id,
                    chain_id=ctx.chain_id,
                    contract_address=pair,
                    kind=PositionKind.APP_TOKEN,
                    position_type=PositionType.DEPOSIT,
                    protocol_module=ProtocolModule.LIQUIDITY_POOL,
                    group_id=group_id_for(self.id, ctx.chain_id, pair),
                    underlyings=tuple(underlyings),
                )
            )
        return positions
