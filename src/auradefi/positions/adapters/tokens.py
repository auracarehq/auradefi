"""ERC-20 fork helpers and the receipt-token adapter base (SPEC §5.4).

SPEC §5.4: the bar is LlamaFolio's claim that most adapters take under
an hour — "What makes that true is not the interface — it is the fork
helpers." This module IS those helpers for receipt tokens: plain
functions over :class:`~auradefi.positions.protocol.ContractReader`,
plus :class:`ReceiptTokenAdapter`, a base class whose subclasses declare
ONLY class attributes (Zapper's production Uniswap V2 integration was 15
lines and zero methods — aim there).

Deliberately NOT a general ``erc4626.py``: Zapper's
``Erc4626VaultTemplate`` was built and never adopted by a single app
(SPEC §5.4). The abstraction waits for the third caller.

Valuation is by redemption, never by price feed (SPEC §4.3: "call
previewRedeem/convertToAssets — quote what the user would actually get
out"). Pinned algorithm (DECISIONS.md "Receipt-token redemption",
breaking to change):

    underlying_raw = share_raw * rate_raw // 10**18

``rate_raw`` is an 18-decimal fixed point; integer floor division; the
identity rate ``10**18`` applies when ``rate_fn`` is ``None`` (rebasing
1:1 receipts like stETH, whose balance already IS the underlying).

Outputs are RAW positions — ``price`` and ``value`` both ``None`` —
persisted and re-drilled against fresh prices without an RPC (SPEC
§5.3). All amounts are ints/``Quantity``; never floats.

Layering: stdlib + ``auradefi.money`` + ``auradefi.positions`` only.
No I/O — ``ContractReader`` is the only chain-read seam.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

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
    ContractReader,
    ContractSet,
    DiscoveryContext,
    ResolveContext,
)

_RATE_ONE = 10**18


def erc20_balance(reader: ContractReader, token: str, holder: str) -> int:
    """``balanceOf(holder)`` at ``token`` via ``reader``, as a plain int.

    Reads ``reader.call(token, "balanceOf", (holder,))``. Arbitrary
    precision — a 10^77-scale balance passes through exactly.
    """
    return int(reader.call(token, "balanceOf", (holder,)))


def erc20_decimals(reader: ContractReader, token: str) -> int:
    """``decimals()`` at ``token`` via ``reader``, as a plain int.

    Reads ``reader.call(token, "decimals", ())``.
    """
    return int(reader.call(token, "decimals", ()))


def erc20_total_supply(reader: ContractReader, token: str) -> int:
    """``totalSupply()`` at ``token`` via ``reader``, as a plain int.

    Reads ``reader.call(token, "totalSupply", ())``.
    """
    return int(reader.call(token, "totalSupply", ()))


def caip19_for_erc20(chain_id: str, address: str) -> str:
    """Canonical CAIP-19 for an ERC-20: ``f"{chain_id}/erc20:{address.lower()}"``.

    Canonical CAIP-19 lowercases EVM addresses (DECISIONS.md "Asset id";
    SPEC rule #3 — deterministic and permanently stable).
    """
    return f"{chain_id}/erc20:{address.lower()}"


@dataclass(frozen=True, slots=True)
class ReceiptToken:
    """One receipt token a protocol issues for a staked/deposited asset.

    ``rate_fn`` is the zero-arg on-chain function returning the
    18-decimal redemption rate (e.g. rETH ``getExchangeRate``); ``None``
    means the receipt rebases 1:1 with its underlying (stETH) and the
    identity rate ``10**18`` applies. ``underlying_decimals`` travels
    here because you cannot format an amount without knowing the
    implementation's decimals (SPEC §4.2).
    """

    address: str
    underlying_caip19: str
    underlying_decimals: int
    rate_fn: str | None


class ReceiptTokenAdapter:
    """The fork-helper base: subclasses declare ONLY class attributes.

    Required on a subclass: ``id`` (DefiLlama slug — the join key),
    ``chains``, and ``receipts`` (a mapping keyed by CAIP-2 chain id to
    the receipt tokens on that chain). ``position_type`` and
    ``protocol_module`` default to STAKED × STAKED and may be
    overridden. Zero methods on the subclass body — the whole
    integration is data (SPEC §5.4).

    ``discover`` (SPEC §5.1: address-blind) emits one static
    ``ContractDescriptor(adapter_id=id, chain_id=ctx.chain_id,
    address=receipt, category="receipt-token",
    underlyings=(underlying_caip19,))`` per receipt on ``ctx.chain_id``,
    without touching the reader — the receipt set is declared
    configuration, not an enumeration.

    ``resolve``: per surviving descriptor (the set arrives partially
    populated or empty — SPEC §5.4), read ``share_raw`` via
    :func:`erc20_balance`; skip the receipt if zero (no rate call);
    else ``rate_raw = reader.call(receipt, rate_fn, ())`` if ``rate_fn``
    else ``10**18``, and emit ONE raw ``Position``:

    * ``kind=APP_TOKEN`` (the position IS a fungible token — SPEC §4.3)
    * ``position_type``/``protocol_module`` from the class attributes
    * ``id=position_id(id, chain_id, receipt)``,
      ``group_id=group_id_for(id, chain_id, receipt)`` (DECISIONS.md)
    * one SUPPLIED ``Underlying`` of ``underlying_caip19`` with
      ``Quantity(share_raw * rate_raw // 10**18, underlying_decimals)``
      — the pinned floor redemption; ``price``/``value`` both ``None``.

    An empty ``ContractSet`` yields ``[]`` without touching the reader.
    """

    id: str
    chains: frozenset[str]
    receipts: Mapping[str, tuple[ReceiptToken, ...]]
    position_type: PositionType = PositionType.STAKED
    protocol_module: ProtocolModule = ProtocolModule.STAKED

    def discover(self, ctx: DiscoveryContext) -> ContractSet:
        """One static descriptor per receipt on ``ctx.chain_id`` (see class docs)."""
        return ContractSet.of(
            *(
                ContractDescriptor(
                    adapter_id=self.id,
                    chain_id=ctx.chain_id,
                    address=receipt.address,
                    category="receipt-token",
                    underlyings=(receipt.underlying_caip19,),
                )
                for receipt in self.receipts.get(ctx.chain_id, ())
            )
        )

    def resolve(
        self, ctx: ResolveContext, contracts: ContractSet
    ) -> list[Position]:
        """One raw redemption-valued position per held receipt (see class docs)."""
        receipts_by_address = {
            receipt.address.lower(): receipt
            for receipt in self.receipts.get(ctx.chain_id, ())
        }
        positions: list[Position] = []
        for descriptor in contracts:
            # .get + continue, matching the Aave adapter. Descriptor sets are
            # "persisted between discovery runs" (ContractDescriptor), so one
            # can outlive the receipt table that produced it — a delisted
            # receipt, a renamed adapter, a set written by an older release.
            # Indexing raised KeyError on the FIRST such descriptor, and
            # because this loop builds its whole list before returning, that
            # dropped EVERY Lido/Rocket Pool position out of net_worth
            # instead of the one stale row (RELEASE_0.1.1 §5 #31).
            receipt = receipts_by_address.get(descriptor.address)
            if receipt is None:
                continue
            share_raw = erc20_balance(ctx.reader, descriptor.address, ctx.address)
            if share_raw == 0:
                continue
            if receipt.rate_fn is None:
                rate_raw = _RATE_ONE
            else:
                rate_raw = int(
                    ctx.reader.call(descriptor.address, receipt.rate_fn, ())
                )
            underlying_raw = share_raw * rate_raw // _RATE_ONE
            positions.append(
                Position(
                    id=position_id(self.id, descriptor.chain_id, descriptor.address),
                    adapter_id=self.id,
                    chain_id=descriptor.chain_id,
                    contract_address=descriptor.address,
                    kind=PositionKind.APP_TOKEN,
                    position_type=self.position_type,
                    protocol_module=self.protocol_module,
                    group_id=group_id_for(
                        self.id, descriptor.chain_id, descriptor.address
                    ),
                    underlyings=(
                        Underlying(
                            asset_id=receipt.underlying_caip19,
                            quantity=Quantity(
                                underlying_raw, receipt.underlying_decimals
                            ),
                            meta_type=MetaType.SUPPLIED,
                        ),
                    ),
                )
            )
        return positions
