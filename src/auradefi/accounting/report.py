"""The reporting projection over replayed PnL state (SPEC §9, SPEC §6.2).

:mod:`auradefi.accounting.pnl` replays a taxable event stream into a
``PnLState``; this module projects that state into the value a caller
reads. The two halves split along the direction of data flow rather than
by size: replay ADVANCES state, a report only READS it. Nothing here
mutates a lot, consumes units or draws down an ACB pool, so one state may
be reported at as many instants, against as many marks, as the caller
likes, in any order, and the answers never depend on which reports were
taken first. That property is what makes ``pnl_at`` cheap enough to
answer an arbitrary date (SPEC §9, the thing Zerion cannot do).

Three frozen value types carry the answer:

* :class:`PnLReport` — the whole picture at one instant under one method.
* :class:`AssetPnL` — one asset's slice of that picture.
* :class:`TaxLot` — one surviving lot already in Plaid's ``tax_lots[]``
  shape (SPEC §6.2, DECISIONS "Plaid TaxLot mapping"), so nothing
  downstream needs a second mapping layer to reach the wire.

All three are frozen and ``per_asset`` is a ``MappingProxyType``: a
report is a SNAPSHOT of state the caller does not own, and a plain dict
would let a caller write through it into the engine's view of the world.

Two pinned rules govern every number below. Unknowns propagate rather
than collapsing to zero (DECISIONS "None-propagation (PnL)"): a held
asset without a mark, or without a complete basis, makes ``unrealized``
``None``, and a disposal whose realised amount is unknown is COUNTED in
``missing_realized_count`` instead of being summed as nothing. And
rounding happens exactly once, at the ``Fraction`` -> ``Money`` boundary
(DECISIONS "Fraction->Money boundary"): totals accumulate as exact
rationals and are converted at the end, so a report never sums numbers
that have each already been rounded.

Pure: ``auradefi.money``, ``auradefi.accounting`` and the standard
library — no I/O and no clock. ``as_of_ms`` is supplied by the caller and
never read from ``now()``, which is what keeps a report replayable rather
than pinned to the moment it was taken.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType
from typing import TYPE_CHECKING

from auradefi.accounting.lots import Lot, exact_mul, fraction_to_money
from auradefi.errors import CurrencyMismatchError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

if TYPE_CHECKING:  # a report READS replay state; the dependency is one-way
    from auradefi.accounting.pnl import PnLState

#: The one flag this module emits, pinned in docs/internal/DECISIONS.md
#: ("Fraction->Money boundary … always flagged").
ROUNDED_BASIS = "rounded_basis"

#: What ``PnLReport.unrealized`` subtracted. ACB costs from a per-asset
#: running POOL; every other method costs from the lots themselves, and
#: only in the pool case do the two figures legitimately differ (#16).
BASIS_FROM_POOL = "pool"
BASIS_FROM_LOTS = "lots"

#: Currency a report falls back to when NOTHING in the stream was priced.
#: An unpriced stream still has to report a zero, and a zero needs a
#: denomination; the first priced ``Money`` seen during replay overrides
#: this for the rest of the stream (see ``PnLState.currency``).
DEFAULT_CURRENCY = "USD"

#: Plaid's ``position_type`` for a held lot (SPEC §6.2). SHORT is
#: reserved and never emitted: this engine costs disposals against
#: acquisitions, and an overdraw is booked as a zero-cost synthetic
#: rather than a negative holding (DECISIONS "Shortfall semantics").
POSITION_LONG = "LONG"


@dataclass(frozen=True, slots=True)
class AssetPnL:
    """One asset's slice of a :class:`PnLReport`.

    ``realized`` sums only the disposals of this asset whose realised
    amount is KNOWN, and is an exact zero in the report's currency when
    the asset has none — an asset that only ever had unknown outcomes
    reports zero here while the report's ``missing_realized_count``
    records that something was left out.

    ``unrealized`` is ``None`` when units are still held but lack a mark
    or a complete basis (DECISIONS "None-propagation (PnL)"), and an
    exact zero when the asset is flat: nothing is held, so there is
    nothing a missing mark could make uncertain, and no mark is required.

    ``quantity_held`` carries the asset's own scale, taken from the last
    event seen for it, so an asset sold down to nothing still reports
    zero at 18 decimals rather than at scale 0.

    ``flags`` records boundary facts the numbers alone cannot carry.
    ``"rounded_basis"`` means at least one figure here left the exact
    rational domain at the Fraction->Money boundary, so it is accurate to
    28 significant digits rather than exactly right. DECISIONS pins that
    this boundary "is always flagged"; without the flag a rounded figure
    is indistinguishable from an exact one (RELEASE_0.1.1 §5 #29).
    """

    realized: Money
    unrealized: Money | None
    quantity_held: Quantity
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaxLot:
    """One open lot in Plaid's ``tax_lots[]`` shape (SPEC §6.2, DECISIONS
    "Plaid TaxLot mapping").

    ``institution_lot_id`` is the deterministic id from
    :func:`auradefi.accounting.lots.lot_id`, so the same acquisition
    reports the same lot across runs and across backends.

    ``quantity``, ``cost_basis`` and ``current_value`` all describe what
    is LEFT of the lot: units remaining, the exact un-disposed part of
    the basis rounded once into ``Money``, and those units at the mark.
    ``purchase_price`` is the exception — it is
    ``cost_total / quantity_original``, a fact of the ACQUISITION that
    does not move as the lot is drawn down, so a half-consumed lot still
    reports the price it was bought at.

    ``purchase_price`` and ``cost_basis`` are ``None`` together when the
    acquisition was unpriced; ``current_value`` is ``None`` when the
    asset has no mark. Only lots with units remaining are ever emitted.
    """

    institution_lot_id: str
    original_purchase_datetime: int
    quantity: Decimal
    purchase_price: Money | None
    cost_basis: Money | None
    current_value: Money | None
    position_type: str = POSITION_LONG
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PnLReport:
    """PnL as of one instant, under one costing method.

    ``as_of_ms`` is the instant the caller asked about, carried through
    verbatim; it is not read from a clock and need not coincide with any
    event. ``method`` is the method the state was replayed under — a
    report cannot be re-costed, because the lot ledger behind it has
    already been consumed one particular way.

    ``realized`` is the exact sum of the disposals whose realised amount
    is KNOWN, and ``missing_realized_count`` is how many were left out of
    that sum; a caller that ignores the count will silently read an
    understated total, which is why the count is not optional.
    ``unrealized`` is ``None`` if ANY held asset is uncertain, so it is
    the conservative whole-portfolio figure rather than a partial sum —
    the per-asset detail in ``per_asset`` is where the known parts stay
    visible.

    ``open_lots`` is sorted by ``(asset_id, opened_at_ms, lot_id)``: a
    total order with no ties, so the wire output is byte-stable across
    runs even when two lots were opened in the same millisecond.

    UNDER ``method="acb"``, ``unrealized`` AND ``TaxLot.cost_basis`` DO NOT
    AGREE, and that is correct. ACB costs from a per-asset running POOL, so
    a disposal consumes ``pool_cost × take/pool`` and leaves the pool
    reduced proportionally; the lots behind it are untouched and keep
    reporting their own remaining basis, because they stay ground truth for
    lot-level reporting (docs/internal/DECISIONS.md, "ACB pooling"). Buy 1 at 10, 1
    at 20 and 1 at 15, sell one, and the pool holds 30 while the surviving
    lots sum to 35 — a permanent, intended gap of 5.

    Summing ``TaxLot.cost_basis`` and comparing it with what ``unrealized``
    implies is therefore the wrong check, and it is an easy one to reach for.
    :attr:`basis_source` names which cost ``unrealized`` actually subtracted,
    and :attr:`unrealized_basis` and :attr:`open_lots_basis` expose both
    figures, so the difference is inspectable rather than something a caller
    has to reverse-engineer and mistake for a bug.
    """

    as_of_ms: int
    method: str
    realized: Money
    missing_realized_count: int
    unrealized: Money | None
    per_asset: Mapping[str, AssetPnL]
    open_lots: tuple[TaxLot, ...]
    flags: tuple[str, ...] = ()
    #: ``"pool"`` under ACB, ``"lots"`` for every lot-tracking method — which
    #: cost :attr:`unrealized` subtracted. Never cosmetic: under ``"pool"``
    #: it is the ONLY signal that summing the lots will give another number.
    basis_source: str = BASIS_FROM_LOTS
    #: The cost :attr:`unrealized` actually subtracted, or ``None`` when
    #: ``unrealized`` is ``None``. Under ACB this is the pool's cost.
    unrealized_basis: Money | None = None

    @property
    def open_lots_basis(self) -> Money | None:
        """Sum of every open lot's ``cost_basis``, or ``None`` if any is.

        The lot-side counterpart to :attr:`unrealized_basis`. Equal to it
        under every lot-tracking method and deliberately NOT equal under
        ACB. ``None`` when any open lot is unpriced — an unknown basis is
        declared, never summed as zero.
        """
        if not self.open_lots:
            return Money(Decimal(0), self.realized.currency)
        total = Decimal(0)
        for lot in self.open_lots:
            if lot.cost_basis is None:
                return None
            total += lot.cost_basis.amount
        return Money(total, self.realized.currency)


def report(state: PnLState, as_of_ms: int, marks: Mapping[str, Money]) -> PnLReport:
    """Summarise ``state`` as of ``as_of_ms`` against ``marks``.

    ``state`` is READ, never advanced: the caller may report the same
    state repeatedly, at different instants and against different marks,
    and each answer is independent of the others. ``as_of_ms`` is
    recorded on the result and is otherwise inert — the state has already
    been replayed to the cutoff the caller chose, so a report does not
    re-filter events by time.

    ``realized`` is the exact sum of the disposals whose realised amount
    is known, and an exact zero when there are none; every disposal left
    out is counted in ``missing_realized_count`` rather than treated as a
    zero (DECISIONS "None-propagation (PnL)").

    ``unrealized`` is the exact sum over assets of
    ``mark x units held - remaining basis``, and ``None`` if ANY held
    asset lacks a mark or lacks a complete basis. Under ``"acb"`` the
    basis subtracted is the POOL's, not the surviving lots' — the pool is
    what that method actually costs with, so the two will not agree and
    the lots remain ground truth only for open-lot reporting (DECISIONS
    "ACB pooling"). An asset holding nothing needs no mark and
    contributes an exact zero.

    ``marks`` maps asset id to a unit price in the report's currency. A
    mark in another currency raises ``CurrencyMismatchError`` — silently
    mixing denominations is the failure that makes a tax report wrong
    without looking wrong. A mark for an asset that is not held is
    ignored, so a caller may pass one price table for a whole portfolio.

    The report's currency is the one the stream fixed on its first priced
    ``Money``, or :data:`DEFAULT_CURRENCY` when nothing was ever priced.
    """
    currency = state.currency or DEFAULT_CURRENCY
    for asset_id, mark in marks.items():
        if mark.currency != currency:
            raise CurrencyMismatchError(
                f"mark for {asset_id!r} is {mark.currency!r}, report is {currency!r}"
            )
    zero = Money(Decimal(0), currency)
    realized, by_asset, missing = zero, {}, 0
    for record in state.disposals:
        if record.realized is None:
            missing += 1
            continue
        realized = realized + record.realized
        running = by_asset.get(record.asset_id)
        by_asset[record.asset_id] = (
            record.realized if running is None else running + record.realized
        )
    per_asset: dict[str, AssetPnL] = {}
    rows: list[tuple[str, int, str, TaxLot]] = []
    total: Fraction | None = Fraction(0)
    # Accumulated alongside `total` and with the SAME None-propagation, so
    # the reported basis can never disagree with the figure derived from it.
    total_basis: Fraction | None = Fraction(0)
    for asset_id, ledger in state.ledgers.items():
        open_lots = ledger.open_lots
        held, basis = _held(open_lots, state.scales.get(asset_id, 0))
        if state.method == "acb":
            pool = state.pools.get(asset_id)
            basis = None if pool is None else pool.cost
        mark = marks.get(asset_id)
        gap: Fraction | None = None
        if held.raw == 0:  # nothing left to be wrong about; no mark needed
            gap = Fraction(0)
        elif mark is not None and basis is not None:
            gap = Fraction(exact_mul(mark.amount, held.as_decimal())) - basis
        if gap is None:
            total = None
            total_basis = None
        elif total is not None:
            total += gap
        if total_basis is not None:
            # Nothing held means nothing was costed: contribute zero rather
            # than a stale pool figure, matching the gap's own zero above.
            total_basis += Fraction(0) if held.raw == 0 else (basis or Fraction(0))
        # The `is_exact` bit is CAPTURED, not indexed away: DECISIONS pins
        # that this boundary "is always flagged", and every site here used
        # to drop it, so a rounded figure read as an exact one (§5 #29).
        asset_flags: tuple[str, ...] = ()
        if gap is None:
            gap_money = None
        else:
            gap_money, gap_exact = fraction_to_money(gap, currency)
            if not gap_exact:
                asset_flags = (ROUNDED_BASIS,)
        lot_rows = [
            (asset_id, lot.opened_at_ms, lot.lot_id, _tax_lot(lot, mark, currency))
            for lot in open_lots
        ]
        if any(ROUNDED_BASIS in row[3].flags for row in lot_rows):
            asset_flags = (ROUNDED_BASIS,)
        per_asset[asset_id] = AssetPnL(
            by_asset.get(asset_id, zero), gap_money, held, asset_flags
        )
        rows.extend(lot_rows)
    rows.sort(key=lambda row: row[:3])
    if total is None:
        total_money, total_exact = None, True
    else:
        total_money, total_exact = fraction_to_money(total, currency)
    # The report flags if ITS total rounded or if anything it contains did:
    # a caller reading only the top-level figure must still learn that some
    # number underneath it is not exact.
    report_flags: tuple[str, ...] = ()
    if not total_exact or any(
        ROUNDED_BASIS in slice_.flags for slice_ in per_asset.values()
    ):
        report_flags = (ROUNDED_BASIS,)
    basis_money = (
        None
        if total_money is None or total_basis is None
        else fraction_to_money(total_basis, currency)[0]
    )
    return PnLReport(
        as_of_ms, state.method, realized, missing, total_money,
        MappingProxyType(per_asset), tuple(row[3] for row in rows), report_flags,
        BASIS_FROM_POOL if state.method == "acb" else BASIS_FROM_LOTS,
        basis_money,
    )


def _held(open_lots: tuple[Lot, ...], decimals: int) -> tuple[Quantity, Fraction | None]:
    """Surviving units and their exact remaining basis.

    The basis stays a ``Fraction`` — every lot's ``cost_remaining`` is
    exact rational, and summing them before the single rounding boundary
    is what stops a portfolio total from drifting by the accumulated
    error of its parts (DECISIONS "Fraction->Money boundary").

    Returns ``None`` for the basis as soon as ONE lot is unpriced: a
    partial sum over the priced lots would understate the basis while
    looking like a complete answer (DECISIONS "None-propagation (PnL)").
    ``decimals`` is the asset's own scale, applied so a fully drawn-down
    asset reports zero units at the scale it traded in.
    """
    units, basis = 0, Fraction(0)
    for lot in open_lots:
        units += lot.quantity_remaining.raw
        if lot.cost_remaining is None:
            basis = None
        elif basis is not None:
            basis += lot.cost_remaining
    return Quantity(units, decimals), basis


def _tax_lot(lot: Lot, mark: Money | None, currency: str) -> TaxLot:
    """Project one open lot into Plaid's wire shape.

    ``purchase_price`` divides the ORIGINAL cost by the ORIGINAL
    quantity, so it is invariant as the lot is consumed; ``cost_basis``
    rounds what is left of the exact ``cost_remaining``; ``current_value``
    marks the remaining units with :func:`exact_mul`, which is
    context-free so a 40-digit quantity survives the multiplication
    intact. All three are ``None`` when the input they need is missing —
    an unpriced lot has no price and no basis, an unmarked asset has no
    value — rather than defaulting to zero.
    """
    remaining = lot.quantity_remaining.as_decimal()
    exact = True
    if lot.cost_total is None:
        price = None
    else:
        price, price_exact = fraction_to_money(
            Fraction(lot.cost_total.amount)
            / Fraction(lot.quantity_original.as_decimal()),
            currency,
        )
        exact = exact and price_exact
    if lot.cost_remaining is None:
        basis = None
    else:
        basis, basis_exact = fraction_to_money(lot.cost_remaining, currency)
        exact = exact and basis_exact
    value = None if mark is None else Money(exact_mul(mark.amount, remaining), currency)
    return TaxLot(
        lot.lot_id, lot.opened_at_ms, remaining, price, basis, value,
        flags=() if exact else (ROUNDED_BASIS,),
    )
