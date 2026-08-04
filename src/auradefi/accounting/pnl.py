"""Incremental PnL over the lot ledger — the arbitrary-date engine (SPEC §9).

Zerion pre-computes PnL at fixed marks and errors out when more than 3,000
transactions sit between a requested date and the nearest one, so
arbitrary-date PnL is effectively unavailable on an active wallet.
:func:`pnl_at` answers it by REPLAY: one pass over the events at or before
the cutoff, then a report — nothing pre-computed, no clock read, a date is
merely where the replay stops. :data:`METHODS` is the pluggability;
:func:`process` is incremental and snapshotable.

FIFO/LIFO/HIFO cost from the pieces the selector consumed; ACB costs from
the per-asset :class:`AcbPool`, an overlay over lots that remain ground
truth for open-lot reporting (DECISIONS "ACB pooling"). Outrunning the
units held never raises (DECISIONS "Shortfall semantics") and unknowns
propagate rather than becoming zero (DECISIONS "None-propagation (PnL)").

Pure: ``auradefi.money``, ``auradefi.accounting`` and the standard library.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType

from auradefi.accounting import acb, fifo, hifo, lifo
from auradefi.accounting.acb import AcbPool
from auradefi.accounting.lots import (
    AccountingEvent, AcquisitionEvent, ConsumedPiece, DisposalEvent, Lot,
    LotLedger, LotSelector, exact_mul, fraction_to_money,
)
from auradefi.errors import CurrencyMismatchError, DecimalsMismatchError, ValidationError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

#: Currency a report falls back to when nothing in the stream was priced.
DEFAULT_CURRENCY = "USD"

#: Plaid's ``position_type`` for a held lot; SHORT is reserved (SPEC §6.2).
POSITION_LONG = "LONG"


class _MethodTable(dict[str, LotSelector]):
    """:data:`METHODS`'s type: an unknown name raises ``ValidationError``,
    never a bare ``KeyError`` escaping the ``auradefi.errors`` taxonomy."""

    def __missing__(self, method: str) -> LotSelector:
        """Raise ``ValidationError`` naming the methods that do exist."""
        raise ValidationError(
            f"unknown accounting method {method!r}; SPEC §9 names "
            f"{', '.join(sorted(self))}"
        )


#: The four costing methods SPEC §9 names, mapped to their selectors.
#: Indexing with anything else raises ``ValidationError``.
METHODS: Mapping[str, LotSelector] = _MethodTable(
    {"fifo": fifo.select, "lifo": lifo.select, "hifo": hifo.select, "acb": acb.select}
)


@dataclass(frozen=True, slots=True)
class DisposalRecord:
    """One disposal's realised outcome. ``cost_basis`` crosses the
    Fraction->Money boundary, flagging ``"rounded_basis"`` when that
    rounded; ``realized`` is ``proceeds - cost_basis`` only when BOTH are
    known, else ``None`` with ``"missing_proceeds"``/``"missing_cost"``."""

    at_ms: int
    asset_id: str
    quantity: Quantity
    proceeds: Money | None
    cost_basis: Money | None
    realized: Money | None
    missing_basis: bool
    flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AssetPnL:
    """One asset's slice of a report; ``unrealized`` is ``None`` when held
    without a mark or complete basis, and an exact zero when flat."""

    realized: Money
    unrealized: Money | None
    quantity_held: Quantity


@dataclass(frozen=True, slots=True)
class TaxLot:
    """An open lot in Plaid's ``tax_lots[]`` shape (DECISIONS "Plaid TaxLot
    mapping", SPEC §6.2). ``quantity``, ``cost_basis`` and
    ``current_value`` describe what is LEFT; ``purchase_price`` is
    ``cost_total / quantity_original``, a fact of the acquisition that
    does not move as the lot is drawn down."""

    institution_lot_id: str
    original_purchase_datetime: int
    quantity: Decimal
    purchase_price: Money | None
    cost_basis: Money | None
    current_value: Money | None
    position_type: str = POSITION_LONG


@dataclass(frozen=True, slots=True)
class PnLReport:
    """PnL as of one instant, under one method. ``realized`` sums the
    disposals whose outcome is KNOWN, ``missing_realized_count`` counts
    those left out, and ``open_lots`` sorts by
    ``(asset_id, opened_at_ms, lot_id)``."""

    as_of_ms: int
    method: str
    realized: Money
    missing_realized_count: int
    unrealized: Money | None
    per_asset: Mapping[str, AssetPnL]
    open_lots: tuple[TaxLot, ...]


@dataclass(slots=True)
class PnLState:
    """Replay state — MUTABLE by the deviation ``Lot`` and ``AcbPool``
    make: an accumulator :func:`process` advances in place. ``currency``
    is ``None`` until the first priced ``Money`` fixes it, after which
    every priced value must agree. ``pools`` fills only under ``"acb"``;
    ``scales`` remembers each asset's ``decimals``, so an asset sold to
    nothing still reports zero at its own scale."""

    method: str
    currency: str | None = None
    last_at_ms: int = 0
    disposals: list[DisposalRecord] = field(default_factory=list)
    ledgers: dict[str, LotLedger] = field(default_factory=dict)
    pools: dict[str, AcbPool] = field(default_factory=dict)
    scales: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """``ValidationError`` unless ``method`` is one of :data:`METHODS`."""
        METHODS[self.method]  # the lookup IS the validation


class _ReplayLedger(LotLedger):
    """A ``LotLedger`` that drops exhausted lots from its working set.

    Same lot ids, same proration, same exception types as the base class,
    but a disposal costs O(open lots) rather than O(lots ever opened): the
    base rescans the whole history twice per disposal, which is quadratic
    over a replay — three times slower at 50,000 events, and over the
    phase 9 budget. History is not retained (``lots`` == ``open_lots``);
    lot ids stay stable, their counters being a separate dict.
    """

    def _plan(
        self, needed: Quantity, selector: LotSelector
    ) -> list[tuple[Lot, Quantity]]:
        """Drop spent lots, then validate the selector's plan in full.
        Membership is checked by asset and remaining units rather than
        against a dict of every lot."""
        self._lots = [lot for lot in self._lots if lot.quantity_remaining.raw > 0]
        plan = list(selector(self._lots, needed))
        budgets: dict[int, int] = {}
        total = 0
        for lot, take in plan:
            if lot.asset_id != self._asset_id or lot.quantity_remaining.raw <= 0:
                raise ValidationError(f"lot {lot.lot_id!r} is not in this ledger")
            if not isinstance(take, Quantity):
                raise ValidationError(f"take must be a Quantity, got {take!r}")
            if take.decimals != needed.decimals:
                raise DecimalsMismatchError(f"take scale {take.decimals}")
            budget = budgets.get(id(lot), lot.quantity_remaining.raw)
            if not 0 < take.raw <= budget:
                raise ValidationError(f"take {take.raw} outside 1..{budget}")
            budgets[id(lot)] = budget - take.raw
            total += take.raw
        if total > needed.raw:
            raise ValidationError(f"plan takes {total}, more than needed {needed.raw}")
        return plan


def process(
    events: Iterable[AccountingEvent],
    method: str,
    state: PnLState | None = None,
) -> PnLState:
    """Replay ``events`` into ``state`` (a fresh one when ``None``).

    An acquisition opens a lot (and, under ``"acb"``, joins the pool); a
    disposal consumes the selector's plan and records the outcome. The
    state is advanced IN PLACE and returned, so
    ``process(tail, m, process(head, m))`` reports identically to one pass
    over ``head + tail``. ``ValidationError`` for an unknown ``method``,
    one disagreeing with ``state.method``, or an event older than
    ``state.last_at_ms`` — input must be monotonic because a lot ledger
    cannot un-consume, though equal timestamps keep caller order.
    ``CurrencyMismatchError`` on a priced value in another currency.
    """
    if state is None:
        state = PnLState(method)
    elif state.method != method:
        raise ValidationError(
            f"state replays {state.method!r}, not {method!r} — a lot ledger "
            f"cannot switch costing method mid-stream"
        )
    selector = METHODS[method]
    pooled = method == "acb"
    for event in events:
        if event.at_ms < state.last_at_ms:
            raise ValidationError(
                f"event at {event.at_ms} precedes {state.last_at_ms}; replay "
                f"input must be monotonic"
            )
        ledger = state.ledgers.get(event.asset_id)
        if ledger is None:
            ledger = state.ledgers[event.asset_id] = _ReplayLedger(event.asset_id)
        state.scales[event.asset_id] = event.quantity.decimals
        if isinstance(event, AcquisitionEvent):
            _fix_currency(state, event.cost)
            ledger.open_lot(event)
            if pooled:
                _pool(state, event.asset_id).acquire(event.quantity, event.cost)
        else:
            state.disposals.append(_dispose(state, ledger, event, selector, pooled))
        state.last_at_ms = event.at_ms
    return state


def _fix_currency(state: PnLState, priced: Money | None) -> None:
    """Bind the stream to the first priced currency; disagreement raises.
    Pools opened before anything was priced adopt it too — such a pool is
    empty or poisoned, so retagging invents no number."""
    if priced is None:
        return
    if state.currency is None:
        state.currency = priced.currency
        for pool in state.pools.values():
            pool.currency = priced.currency
    elif priced.currency != state.currency:
        raise CurrencyMismatchError(
            f"stream is denominated in {state.currency!r}, got {priced.currency!r}"
        )


def _pool(state: PnLState, asset_id: str) -> AcbPool:
    """The asset's ACB pool, opened in the stream's currency on demand."""
    pool = state.pools.get(asset_id)
    if pool is None:
        pool = state.pools[asset_id] = AcbPool(state.currency or DEFAULT_CURRENCY)
    return pool


def _dispose(
    state: PnLState,
    ledger: LotLedger,
    event: DisposalEvent,
    selector: LotSelector,
    pooled: bool,
) -> DisposalRecord:
    """Consume the plan and book one disposal, flagged in the order the
    facts land: shortfall, rounding, unknown cost, unknown proceeds."""
    _fix_currency(state, event.proceeds)
    consumed, shortfall = ledger.consume(event.quantity, selector)
    currency = state.currency or DEFAULT_CURRENCY
    if pooled:  # per-lot portions are meaningless under a pooled average
        pool = _pool(state, event.asset_id)
        held = min(event.quantity.raw, pool.quantity_raw)
        basis = pool.dispose(Quantity(held, event.quantity.decimals))
    else:  # None as soon as one consumed lot was unpriced
        basis = Fraction(0)
        for _lot, _take, portion in consumed:
            if portion is None:
                basis = None
                break
            basis += portion
    missing_basis = shortfall.raw > 0
    flags = ["missing_basis"] if missing_basis else []
    cost_basis: Money | None = None
    if basis is None:
        flags.append("missing_cost")
    else:
        cost_basis, exact = fraction_to_money(basis, currency)
        if not exact:
            flags.append("rounded_basis")
    realized: Money | None = None
    if event.proceeds is None:
        flags.append("missing_proceeds")
    elif cost_basis is not None:
        realized = event.proceeds - cost_basis
    return DisposalRecord(
        event.at_ms, event.asset_id, event.quantity, event.proceeds,
        cost_basis, realized, missing_basis, tuple(flags),
    )


def report(state: PnLState, as_of_ms: int, marks: Mapping[str, Money]) -> PnLReport:
    """Summarise ``state`` as of ``as_of_ms`` against ``marks``.

    ``realized`` is the exact sum of the disposals whose realised amount
    is known, and zero when none are. ``unrealized`` is the exact sum over
    assets of ``mark x held - remaining basis``, and ``None`` if ANY held
    asset lacks a mark or complete basis — under ``"acb"`` that basis is
    the POOL's, what the method actually costs with, so it will not equal
    the surviving lots'. A mark in another currency raises
    ``CurrencyMismatchError``; one for an unheld asset is ignored.
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
        elif total is not None:
            total += gap
        per_asset[asset_id] = AssetPnL(
            by_asset.get(asset_id, zero),
            None if gap is None else fraction_to_money(gap, currency)[0],
            held,
        )
        rows.extend(
            (asset_id, lot.opened_at_ms, lot.lot_id, _tax_lot(lot, mark, currency))
            for lot in open_lots
        )
    rows.sort(key=lambda row: row[:3])
    return PnLReport(
        as_of_ms, state.method, realized, missing,
        None if total is None else fraction_to_money(total, currency)[0],
        MappingProxyType(per_asset), tuple(row[3] for row in rows),
    )


def _held(open_lots: tuple[Lot, ...], decimals: int) -> tuple[Quantity, Fraction | None]:
    """Surviving units and their exact basis (``None`` if a lot is unpriced)."""
    units, basis = 0, Fraction(0)
    for lot in open_lots:
        units += lot.quantity_remaining.raw
        if lot.cost_remaining is None:
            basis = None
        elif basis is not None:
            basis += lot.cost_remaining
    return Quantity(units, decimals), basis


def _tax_lot(lot: Lot, mark: Money | None, currency: str) -> TaxLot:
    """One open lot in Plaid's shape, marked when a mark exists."""
    remaining = lot.quantity_remaining.as_decimal()
    price = None if lot.cost_total is None else fraction_to_money(
        Fraction(lot.cost_total.amount) / Fraction(lot.quantity_original.as_decimal()),
        currency,
    )[0]
    basis = None if lot.cost_remaining is None else fraction_to_money(
        lot.cost_remaining, currency
    )[0]
    value = None if mark is None else Money(exact_mul(mark.amount, remaining), currency)
    return TaxLot(lot.lot_id, lot.opened_at_ms, remaining, price, basis, value)


def pnl_at(
    events: Iterable[AccountingEvent],
    method: str,
    at_ms: int,
    marks: Mapping[str, Money],
) -> PnLReport:
    """PnL at an ARBITRARY date — the thing Zerion cannot do (SPEC §9).
    Replays only the events at or before ``at_ms``, in ONE pass, and
    reports as of the cutoff — equivalent to filtering, :func:`process`
    and :func:`report`, with no marks pre-computed at fixed dates."""
    admitted = (event for event in events if event.at_ms <= at_ms)
    return report(process(admitted, method), at_ms, marks)
