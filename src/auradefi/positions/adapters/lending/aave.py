"""Aave v3 lending adapter (SPEC §4.3, §5.4; DECISIONS.md "Aave scaling").

SPEC §4.3, verbatim: "Aave supply = ``lending`` + ``deposit``. Aave
borrow = ``lending`` + ``loan``" — and the AppToken/ContractPosition
asymmetry applies within ONE protocol: an aToken is fungible and
priceable (``APP_TOKEN``); variable debt is non-transferable — you
cannot add it to MetaMask — so a borrow is a ``CONTRACT_POSITION``.
``BORROWED`` alone carries the sign; ``resolve()`` never emits negative
quantities.

Fork economics (SPEC §5.4, Zapper's 15-line subclass): a fork
deployment (Spark, Avalanche, ...) subclasses :class:`AaveV3Adapter`
overriding ``id`` / ``chains`` / ``pool`` / ``markets`` ONLY — every id,
descriptor and position derives from those four class attributes.

Everything emitted is RAW (``price``/``value`` both ``None``): raw
balances persist and re-drill against fresh prices without an RPC
(SPEC §5.3). The only chain seam is ``ctx.reader`` (no HTTP here —
positions/ is not an IO domain).

Pinned scaling (DECISIONS.md): ``health_factor = Quantity(hf_raw,
18).as_decimal()``; ``ltv = Quantity(ltv_bp, 4).as_decimal()``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from auradefi.money.quantity import Quantity
from auradefi.positions.models import (
    GroupInfo,
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

_SUPPLY = "lending-supply"
_BORROW = "lending-borrow"


@dataclass(frozen=True, slots=True)
class Market:
    """One Aave v3 reserve: the two position-bearing token contracts
    plus the underlying's identity and scale.

    ``a_token`` (supply receipt, rebasing 1:1 — ``balanceOf`` IS the
    underlying amount) and ``variable_debt_token`` (non-transferable
    debt tracker) are the descriptor addresses, because THOSE are the
    contracts that appear in a user's tokentx history (SPEC §5.2).
    ``underlying_caip19`` is canonical CAIP-19, never an ``ast_`` id;
    ``decimals`` scales both tokens' raw ``balanceOf`` results.
    """

    a_token: str
    variable_debt_token: str
    underlying_caip19: str
    decimals: int


class AaveV3Adapter:
    """Aave v3 on Ethereum mainnet (SPEC §5.4 adapter contract).

    Class attributes are the WHOLE fork surface — subclasses override
    ``id`` / ``chains`` / ``pool`` / ``markets`` and nothing else.

    ``discover()`` is address-blind (SPEC §5.1) and pure over the
    ``markets`` table: per market it emits two descriptors — the
    lowercased aToken with ``category='lending-supply'`` and the
    lowercased variable-debt token with ``category='lending-borrow'``,
    each carrying ``underlyings=(underlying_caip19,)`` and
    ``meta=(('pool', <pool lowercased>),)``.

    ``resolve()`` runs only over the SURVIVING descriptors (the §5.2
    pre-filter): per supply descriptor, ``call(a_token, 'balanceOf',
    (address,))``; if raw > 0 emit a RAW ``APP_TOKEN`` position
    (``position_type=DEPOSIT``, ``protocol_module=LENDING``, one
    ``SUPPLIED`` underlying). Per borrow descriptor, ``call(debt_token,
    'balanceOf', (address,))``; if raw > 0 emit a RAW
    ``CONTRACT_POSITION`` (``position_type=LOAN``, same module, one
    ``BORROWED`` underlying). Ids are the pinned ``position_id(id,
    chain, token)`` / ``group_id_for(id, chain, pool_lower)`` — the
    Pool is the risk unit, so every position shares one ``group_id``
    (SPEC §4.3). If at least one position was emitted, ONE call to
    ``call(pool, 'getUserAccountData', (address,))`` yields
    ``(tc, td, ab, clt, ltv_bp, hf_raw)`` and a ``GroupInfo(
    health_factor=Quantity(hf_raw, 18).as_decimal(),
    ltv=Quantity(ltv_bp, 4).as_decimal(), liquidation_price=None)`` is
    attached to the FIRST emitted position only (drill merges — the
    group is the risk unit). Zero positions emitted → no
    ``getUserAccountData`` call at all.
    """

    id: str = "aave-v3"
    chains: frozenset[str] = frozenset({"eip155:1"})
    pool: str = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
    markets: tuple[Market, ...] = ()

    def discover(self, ctx: DiscoveryContext) -> ContractSet:
        """Two static descriptors per market; no reader calls needed."""
        pool_meta = (("pool", self.pool.lower()),)
        descriptors: list[ContractDescriptor] = []
        for market in self.markets:
            for address, category in (
                (market.a_token, _SUPPLY),
                (market.variable_debt_token, _BORROW),
            ):
                descriptors.append(
                    ContractDescriptor(
                        adapter_id=self.id,
                        chain_id=ctx.chain_id,
                        address=address,
                        category=category,
                        underlyings=(market.underlying_caip19,),
                        meta=pool_meta,
                    )
                )
        return ContractSet.of(*descriptors)

    def resolve(
        self, ctx: ResolveContext, contracts: ContractSet
    ) -> list[Position]:
        """RAW positions for ``ctx.address`` over surviving descriptors."""
        pool = self.pool.lower()
        group = group_id_for(self.id, ctx.chain_id, pool)
        positions = [
            *self._emit(ctx, contracts, group, _SUPPLY),
            *self._emit(ctx, contracts, group, _BORROW),
        ]
        if not positions:
            return []
        account = ctx.reader.call(pool, "getUserAccountData", (ctx.address,))
        _tc, _td, _ab, _clt, ltv_bp, hf_raw = account
        info = GroupInfo(
            health_factor=Quantity(hf_raw, 18).as_decimal(),
            ltv=Quantity(ltv_bp, 4).as_decimal(),
            liquidation_price=None,
        )
        positions[0] = replace(positions[0], group_info=info)
        return positions

    def _emit(
        self,
        ctx: ResolveContext,
        contracts: ContractSet,
        group: str,
        category: str,
    ) -> list[Position]:
        """RAW positions for one category, in ``ContractSet`` order.

        Reads ``balanceOf`` only for surviving descriptors of
        ``category`` that map back to one of ``self.markets``; emits
        iff raw > 0 (never a negative quantity — BORROWED alone
        carries the sign). Results are coerced with ``int()`` so a
        malformed reader response (``None``, non-numeric) raises
        instead of silently dropping a position — SPEC §5.4: a
        resolver that raises is caught, logged, and drops only its
        own slice.
        """
        if category == _SUPPLY:
            by_token = {m.a_token.lower(): m for m in self.markets}
            kind = PositionKind.APP_TOKEN
            position_type = PositionType.DEPOSIT
            meta_type = MetaType.SUPPLIED
        else:
            by_token = {
                m.variable_debt_token.lower(): m for m in self.markets
            }
            kind = PositionKind.CONTRACT_POSITION
            position_type = PositionType.LOAN
            meta_type = MetaType.BORROWED
        emitted: list[Position] = []
        for descriptor in contracts:
            if descriptor.adapter_id != self.id:
                continue
            if descriptor.category != category:
                continue
            market = by_token.get(descriptor.address)
            if market is None:
                continue
            raw = int(
                ctx.reader.call(descriptor.address, "balanceOf", (ctx.address,))
            )
            if raw <= 0:
                continue
            emitted.append(
                Position(
                    id=position_id(
                        self.id, ctx.chain_id, descriptor.address
                    ),
                    adapter_id=self.id,
                    chain_id=ctx.chain_id,
                    contract_address=descriptor.address,
                    kind=kind,
                    position_type=position_type,
                    protocol_module=ProtocolModule.LENDING,
                    group_id=group,
                    underlyings=(
                        Underlying(
                            asset_id=market.underlying_caip19,
                            quantity=Quantity(raw, market.decimals),
                            meta_type=meta_type,
                        ),
                    ),
                )
            )
        return emitted
