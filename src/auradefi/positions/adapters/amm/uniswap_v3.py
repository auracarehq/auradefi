"""Uniswap V3 adapter (SPEC §5.4, §4.3; DECISIONS.md pinned TickMath).

Concentrated liquidity: each NFT held via the position manager is ONE
``CONTRACT_POSITION`` (SPEC §4.3 — you cannot add an NFT position to
MetaMask). All chain reads go through ``ctx.reader``.

``discover`` (SPEC §5.1) emits the single position-manager descriptor
(``category='amm-nft-manager'``) — enumeration is per-user, so it lives
in ``resolve``::

    n = call(manager, 'balanceOf', (address,))
    token_id = call(manager, 'tokenOfOwnerByIndex', (address, i))
    (nonce, operator, token0, token1, fee, tickLower, tickUpper,
     liquidity, fg0, fg1, tokensOwed0, tokensOwed1)
        = call(manager, 'positions', (token_id,))
    pool = call(factory, 'getPool', (token0, token1, fee))
    (sqrtPriceX96, tick, *_) = call(pool, 'slot0')

A token_id is skipped iff ``liquidity == 0`` and both tokensOwed are 0.
Emitted position: ``position_type=DEPOSIT``,
``protocol_module=LIQUIDITY_POOL``,
``id=position_id(id, chain, manager, str(token_id))``,
``group_id=group_id_for(id, chain, pool_lower)``,
``range=Range(tickLower, tickUpper, in_range)`` with
``in_range = tickLower <= tick < tickUpper`` (strict upper bound).
Underlyings: SUPPLIED amount0/amount1 (a zero side is omitted) then
CLAIMABLE tokensOwed0/1 when nonzero — all RAW (no prices, SPEC §5.3).

The math is pinned in DECISIONS.md and exposed here as module-level
pure functions; golden vectors in the test tree hardcode their outputs.
"""

from __future__ import annotations

from auradefi.errors import ValidationError
from auradefi.money.quantity import Quantity
from auradefi.positions.models import (
    MetaType,
    Position,
    PositionKind,
    PositionType,
    ProtocolModule,
    Range,
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

MIN_TICK: int = -887272
MAX_TICK: int = 887272

# Canonical TickMath per-bit 128.128 magic constants: factor for bit b is
# round(2^128 / 1.0001^(2^b / 2)), applied when abs(tick) has bit b set.
_MAGIC_FACTORS: tuple[int, ...] = (
    0xFFFCB933BD6FAD37AA2D162D1A594001,
    0xFFF97272373D413259A46990580E213A,
    0xFFF2E50F5F656932EF12357CF3C7FDCC,
    0xFFE5CACA7E10E4E61C3624EAA0941CD0,
    0xFFCB9843D60F6159C9DB58835C926644,
    0xFF973B41FA98C081472E6896DFB254C0,
    0xFF2EA16466C96A3843EC78B326B52861,
    0xFE5DEE046A99A2A811C461F1969C3053,
    0xFCBE86C7900A88AEDCFFC83B479AA3A4,
    0xF987A7253AC413176F2B074CF7815E54,
    0xF3392B0822B70005940C7A398E4B70F3,
    0xE7159475A2C29B7443B29C7FA6E889D9,
    0xD097F3BDFD2022B8845AD8F792AA5825,
    0xA9F746462D870FDF8A65DC1F90E061E5,
    0x70D869A156D2A1B890BB3DF62BAF32F7,
    0x31BE135F97D08FD981231505542FCFA6,
    0x9AA508B5B7A84E1C677DE54F3E99BC9,
    0x5D6AF8DEDB81196699C329225EE604,
    0x2216E584F5FA1EA926041BEDFE98,
    0x48A170391F7DC42444E8FA2,
)
_U256_MAX = (1 << 256) - 1


def get_sqrt_ratio_at_tick(tick: int) -> int:
    """sqrt(1.0001^tick) * 2^96 — canonical Uniswap V3 TickMath.

    The integer algorithm verbatim: per-bit 128.128 magic-constant
    products over ``abs(tick)``, ratio inverted for ``tick > 0``, then
    ``>> 32`` rounded UP. Pinned vectors (DECISIONS.md):

        get_sqrt_ratio_at_tick(0)       == 79228162514264337593543950336
        get_sqrt_ratio_at_tick(-887272) == 4295128739
        get_sqrt_ratio_at_tick(887272)  ==
            1461446703485210103287273052203988822378723970342

    Raises ``ValidationError`` if ``abs(tick) > 887272``.
    """
    abs_tick = abs(tick)
    if abs_tick > MAX_TICK:
        raise ValidationError(f"tick must satisfy |tick| <= {MAX_TICK}, got {tick}")
    ratio = _MAGIC_FACTORS[0] if abs_tick & 1 else 1 << 128
    for bit in range(1, 20):
        if abs_tick & (1 << bit):
            ratio = ratio * _MAGIC_FACTORS[bit] >> 128
    if tick > 0:
        ratio = _U256_MAX // ratio
    return (ratio >> 32) + (1 if ratio % (1 << 32) else 0)


def amounts_for_liquidity(
    liquidity: int,
    sqrt_price_x96: int,
    tick: int,
    tick_lower: int,
    tick_upper: int,
) -> tuple[int, int]:
    """(amount0_raw, amount1_raw) for a position — pinned (DECISIONS.md).

    With ``sqrtA = get_sqrt_ratio_at_tick(tick_lower)``, ``sqrtB =
    get_sqrt_ratio_at_tick(tick_upper)``, ``sqrtP = sqrt_price_x96``
    and ``L = liquidity``::

        tick < tick_lower:   amount0 = ((L << 96) * (sqrtB - sqrtA)
                                        // sqrtB) // sqrtA
                             amount1 = 0
        tick >= tick_upper:  amount0 = 0
                             amount1 = L * (sqrtB - sqrtA) // 2**96
        else (in range):     amount0 = ((L << 96) * (sqrtB - sqrtP)
                                        // sqrtB) // sqrtP
                             amount1 = L * (sqrtP - sqrtA) // 2**96

    Integer floor everywhere — never rounding.
    """
    sqrt_a = get_sqrt_ratio_at_tick(tick_lower)
    sqrt_b = get_sqrt_ratio_at_tick(tick_upper)
    if tick < tick_lower:
        amount0 = ((liquidity << 96) * (sqrt_b - sqrt_a) // sqrt_b) // sqrt_a
        return amount0, 0
    if tick >= tick_upper:
        return 0, liquidity * (sqrt_b - sqrt_a) // 2**96
    amount0 = (
        (liquidity << 96) * (sqrt_b - sqrt_price_x96) // sqrt_b
    ) // sqrt_price_x96
    amount1 = liquidity * (sqrt_price_x96 - sqrt_a) // 2**96
    return amount0, amount1


class UniswapV3Adapter:
    """Uniswap V3 on Ethereum mainnet via the NFT position manager."""

    id: str = "uniswap-v3"
    chains: frozenset[str] = frozenset({"eip155:1"})
    position_manager: str = "0xc36442b4a4522e871399cd717abdd847ab11fe88"
    factory_address: str = "0x1f98431c8ad98523631ae4a59f267346ea31f984"

    def discover(self, ctx: DiscoveryContext) -> ContractSet:
        """Emit the single manager descriptor (category='amm-nft-manager')."""
        return ContractSet.of(
            ContractDescriptor(
                adapter_id=self.id,
                chain_id=ctx.chain_id,
                address=self.position_manager,
                category="amm-nft-manager",
            )
        )

    def resolve(
        self, ctx: ResolveContext, contracts: ContractSet
    ) -> list[Position]:
        """One RAW CONTRACT_POSITION per NFT the address holds.

        Skips a token_id iff ``liquidity == 0`` and both tokensOwed
        are 0. Underlyings: SUPPLIED amount0/amount1 (zero side
        omitted) then CLAIMABLE tokensOwed0/1 when nonzero.
        """
        positions: list[Position] = []
        for descriptor in contracts:
            manager = descriptor.address
            nft_count = ctx.reader.call(manager, "balanceOf", (ctx.address,))
            for index in range(nft_count):
                token_id = ctx.reader.call(
                    manager, "tokenOfOwnerByIndex", (ctx.address, index)
                )
                position = self._resolve_nft(ctx, manager, token_id)
                if position is not None:
                    positions.append(position)
        return positions

    def _resolve_nft(
        self, ctx: ResolveContext, manager: str, token_id: object
    ) -> Position | None:
        """Resolve one NFT to a position, or ``None`` if it is empty."""
        (
            _nonce,
            _operator,
            token0,
            token1,
            fee,
            tick_lower,
            tick_upper,
            liquidity,
            _fee_growth0,
            _fee_growth1,
            tokens_owed0,
            tokens_owed1,
        ) = ctx.reader.call(manager, "positions", (token_id,))
        if liquidity == 0 and tokens_owed0 == 0 and tokens_owed1 == 0:
            return None
        pool = ctx.reader.call(
            self.factory_address, "getPool", (token0, token1, fee)
        )
        sqrt_price_x96, tick, *_ = ctx.reader.call(pool, "slot0")
        decimals0 = ctx.reader.call(token0, "decimals")
        decimals1 = ctx.reader.call(token1, "decimals")
        amount0, amount1 = amounts_for_liquidity(
            liquidity, sqrt_price_x96, tick, tick_lower, tick_upper
        )
        asset0 = f"{ctx.chain_id}/erc20:{token0.lower()}"
        asset1 = f"{ctx.chain_id}/erc20:{token1.lower()}"
        underlyings = []
        for asset_id, raw, decimals, meta_type in (
            (asset0, amount0, decimals0, MetaType.SUPPLIED),
            (asset1, amount1, decimals1, MetaType.SUPPLIED),
            (asset0, tokens_owed0, decimals0, MetaType.CLAIMABLE),
            (asset1, tokens_owed1, decimals1, MetaType.CLAIMABLE),
        ):
            if raw:
                underlyings.append(
                    Underlying(
                        asset_id=asset_id,
                        quantity=Quantity(raw, decimals),
                        meta_type=meta_type,
                    )
                )
        return Position(
            id=position_id(self.id, ctx.chain_id, manager, str(token_id)),
            adapter_id=self.id,
            chain_id=ctx.chain_id,
            contract_address=manager,
            kind=PositionKind.CONTRACT_POSITION,
            position_type=PositionType.DEPOSIT,
            protocol_module=ProtocolModule.LIQUIDITY_POOL,
            group_id=group_id_for(self.id, ctx.chain_id, pool),
            underlyings=tuple(underlyings),
            range=Range(tick_lower, tick_upper, tick_lower <= tick < tick_upper),
        )
