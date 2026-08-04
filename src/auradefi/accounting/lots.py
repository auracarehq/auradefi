"""Lot ledger, taxable events, and the exact-math boundary (SPEC §9, §3.3).

``accounting/`` is PURE: it reads ledger values and money values and does
arithmetic on them. No I/O, no HTTP client, no clock — an event's time
comes from the transaction that produced it, never from ``now()``. That
is what makes arbitrary-date PnL replayable rather than pinned to
pre-computed marks (SPEC §9, the thing Zerion cannot do).

This module holds the primitives every costing method shares:

* :class:`AcquisitionEvent` / :class:`DisposalEvent` — the taxable events
  :func:`derive_events` distils out of ``LedgerTransaction`` rows.
  ``Direction.SELF`` entries, transactions listed in
  ``internal_transfer_ids``, and reorg-removed transactions produce
  NOTHING: without ``is_internal_transfer`` every self-transfer reads as
  income and every tax report is wrong (SPEC §9).
* :class:`Lot` / :class:`LotLedger` — the open lots of ONE asset,
  consumed through a pluggable ``selector``. FIFO/LIFO/HIFO/ACB live in
  sibling modules and plug in here; they never import one another.
* :func:`exact_mul` and :func:`fraction_to_money` — every internal lot
  computation is exact rational arithmetic; rounding exists only at the
  ``Fraction`` -> ``Money`` boundary and is always flagged.

Duplication waiver (DECISIONS.md): :func:`exact_mul` is a value-identical
restatement of ``positions.drill.exact_mul`` — the layer contract forbids
``accounting`` -> ``positions``. Both trees pin the same golden vectors,
so drift is a red test rather than a debate.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction

from auradefi.errors import (
    CurrencyMismatchError,
    DecimalsMismatchError,
    ValidationError,
)
from auradefi.ledger.models import Direction, LedgerTransaction
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

#: Significant digits used when a Fraction cannot be represented exactly
#: as a Decimal (DECISIONS "Fraction->Money boundary", ROUND_HALF_EVEN).
SIGNIFICANT_DIGITS = 28


def _require_positive(quantity: Quantity) -> None:
    """Raise ``ValidationError`` unless ``quantity`` is a ``Quantity`` with
    ``raw > 0`` — zero or negative units are a caller bug, not a movement."""
    if not isinstance(quantity, Quantity):
        raise ValidationError(f"quantity must be a Quantity, got {quantity!r}")
    if quantity.raw <= 0:
        raise ValidationError(f"quantity.raw must be > 0, got {quantity.raw}")


@dataclass(frozen=True, slots=True)
class AcquisitionEvent:
    """Units of one asset entered the account at ``at_ms``.

    ``cost`` is the fee-INCLUSIVE total paid, carried only when the
    caller supplied it — pricing stays outside this module, so ``None``
    means "basis unknown", never "basis zero". ``quantity.raw`` must be
    ``> 0`` (``ValidationError`` otherwise): an event of nothing is a
    caller bug, and a negative acquisition is a disposal.
    """

    at_ms: int
    asset_id: str
    quantity: Quantity
    cost: Money | None
    source_tx_id: str

    def __post_init__(self) -> None:
        """``ValidationError`` unless ``quantity`` is a ``Quantity`` with
        ``raw > 0``."""
        _require_positive(self.quantity)


@dataclass(frozen=True, slots=True)
class DisposalEvent:
    """Units of one asset left the account at ``at_ms``.

    ``proceeds`` is the fee-inclusive total received, carried only when
    supplied; ``None`` means "proceeds unknown". ``quantity.raw`` must be
    ``> 0`` (``ValidationError`` otherwise).
    """

    at_ms: int
    asset_id: str
    quantity: Quantity
    proceeds: Money | None
    source_tx_id: str

    def __post_init__(self) -> None:
        """``ValidationError`` unless ``quantity`` is a ``Quantity`` with
        ``raw > 0``."""
        _require_positive(self.quantity)


#: Either taxable event, in one chronologically ordered stream.
AccountingEvent = AcquisitionEvent | DisposalEvent

#: Direction -> event class; SELF is absent on purpose — moving your own
#: coins is not a taxable event (SPEC §9).
_EVENT_BY_DIRECTION = {Direction.IN: AcquisitionEvent, Direction.OUT: DisposalEvent}


@dataclass(slots=True)
class Lot:
    """One acquisition's surviving units and basis.

    MUTABLE by deliberate, documented deviation from the frozen-value
    house style: a lot is ``LotLedger``-internal state that a disposal
    decrements in place. Everything a caller receives out of this module
    is either frozen or a copy of these numbers.

    ``cost_total`` is the fee-inclusive Money as supplied;
    ``cost_remaining`` is the un-disposed part of it as an EXACT
    ``Fraction`` — the whole point of the domain is that basis never
    drifts through repeated proration. Both are ``None`` together when
    the acquisition was unpriced.
    """

    lot_id: str
    opened_at_ms: int
    asset_id: str
    quantity_original: Quantity
    quantity_remaining: Quantity
    cost_total: Money | None
    cost_remaining: Fraction | None
    source_tx_id: str


#: ``selector(open_lots, needed) -> [(lot, take), ...]`` — the plan a
#: costing method returns. Lots arrive in open order, already filtered to
#: those with units remaining.
LotSelector = Callable[[Sequence[Lot], Quantity], Sequence[tuple[Lot, Quantity]]]

#: One executed piece of a plan: the lot, the units taken, and the exact
#: rational basis that went with them (``None`` when the lot is unpriced).
ConsumedPiece = tuple[Lot, Quantity, Fraction | None]


def lot_id(source_tx_id: str, asset_id: str, seq: int) -> str:
    """Deterministic lot id (DECISIONS pinned; SPEC §9).

    ``"lot_" + sha256(f"{source_tx_id}|{asset_id}|{seq}".encode())
    .hexdigest()[:16]``, where ``seq`` is the zero-based ordinal among
    acquisitions sharing ``(source_tx_id, asset_id)``. It is a wire
    contract — Plaid's ``institution_lot_id`` — so the same acquisition
    always reports the same id across runs and across backends.
    """
    digest = hashlib.sha256(f"{source_tx_id}|{asset_id}|{seq}".encode())
    return f"lot_{digest.hexdigest()[:16]}"


class LotLedger:
    """The open lots of ONE asset, in acquisition order.

    Single-asset by construction: an event for another asset is a
    ``ValidationError``, because cross-asset pooling is the bug that
    silently corrupts a tax report. All lots share one scale
    (``DecimalsMismatchError`` otherwise) and one cost currency
    (``CurrencyMismatchError`` otherwise).
    """

    def __init__(self, asset_id: str) -> None:
        """Open an empty ledger for ``asset_id``."""
        self._asset_id = asset_id
        self._lots: list[Lot] = []
        self._seq: dict[tuple[str, str], int] = {}
        self._decimals: int | None = None
        self._currency: str | None = None

    @property
    def asset_id(self) -> str:
        """The one asset this ledger costs."""
        return self._asset_id

    @property
    def lots(self) -> tuple[Lot, ...]:
        """Every lot ever opened, in the order it was opened —
        exhausted ones included, so lot history stays reportable."""
        return tuple(self._lots)

    @property
    def open_lots(self) -> tuple[Lot, ...]:
        """The lots with ``quantity_remaining.raw > 0``, in open order."""
        return tuple(lot for lot in self._lots if lot.quantity_remaining.raw > 0)

    def open_lot(self, event: AcquisitionEvent) -> Lot:
        """Append a lot for ``event`` and return it.

        ``lot_id`` follows the pin, with ``seq`` counting acquisitions
        that share ``(source_tx_id, asset_id)`` over this ledger's whole
        lifetime — exhausted lots still occupy their ordinal, so ids are
        never reused. ``quantity_remaining`` starts at the full
        ``quantity``; ``cost_remaining`` starts at
        ``Fraction(event.cost.amount)``, or ``None`` when unpriced.

        Raises ``ValidationError`` if ``event.asset_id`` is not this
        ledger's asset, ``DecimalsMismatchError`` if its scale differs
        from the lots already held, and ``CurrencyMismatchError`` if its
        cost currency differs from the currency already established.
        """
        cost, decimals = event.cost, event.quantity.decimals
        if event.asset_id != self._asset_id:
            raise ValidationError(f"ledger holds {self._asset_id!r} only")
        if self._decimals not in (None, decimals):
            raise DecimalsMismatchError(f"lot scale {self._decimals}, got {decimals}")
        if cost is not None:
            if self._currency not in (None, cost.currency):
                raise CurrencyMismatchError(f"ledger costs in {self._currency!r}")
            self._currency = cost.currency
        self._decimals = decimals
        key = (event.source_tx_id, event.asset_id)
        seq = self._seq.get(key, 0)
        self._seq[key] = seq + 1
        lot = Lot(
            lot_id=lot_id(event.source_tx_id, event.asset_id, seq),
            opened_at_ms=event.at_ms, asset_id=event.asset_id,
            quantity_original=event.quantity, quantity_remaining=event.quantity,
            cost_total=cost,
            cost_remaining=None if cost is None else Fraction(cost.amount),
            source_tx_id=event.source_tx_id,
        )
        self._lots.append(lot)
        return lot

    def _plan(
        self, needed: Quantity, selector: LotSelector
    ) -> list[tuple[Lot, Quantity]]:
        """The selector's plan, fully validated BEFORE anything is
        decremented — a half-applied plan would corrupt basis silently."""
        plan = list(selector(self.open_lots, needed))
        budgets = {id(lot): lot.quantity_remaining.raw for lot in self._lots}
        total = 0
        for lot, take in plan:
            budget = budgets.get(id(lot))
            if budget is None:
                raise ValidationError(f"lot {lot.lot_id!r} is not in this ledger")
            if not isinstance(take, Quantity):
                raise ValidationError(f"take must be a Quantity, got {take!r}")
            if take.decimals != needed.decimals:
                raise DecimalsMismatchError(f"take scale {take.decimals}")
            if not 0 < take.raw <= budget:
                raise ValidationError(f"take {take.raw} outside 1..{budget}")
            budgets[id(lot)] = budget - take.raw
            total += take.raw
        if total > needed.raw:
            raise ValidationError(f"plan takes {total}, more than needed {needed.raw}")
        return plan

    def consume(
        self, needed: Quantity, selector: LotSelector
    ) -> tuple[list[ConsumedPiece], Quantity]:
        """Dispose ``needed`` units against the plan ``selector`` returns.

        ``selector(open_lots, needed)`` returns ``[(lot, take), ...]``.
        Each take is prorated for basis EXACTLY::

            portion = Fraction(lot.cost_total.amount) * take.raw
                      / lot.quantity_original.raw

        (``None`` when ``lot.cost_total`` is ``None``), then the lot is
        decremented in place: ``quantity_remaining -= take`` and
        ``cost_remaining -= portion``.

        Returns ``(consumed, shortfall)``. ``shortfall`` is
        ``needed`` minus everything taken and is the second half of the
        contract, NOT an error: a disposal exceeding held lots NEVER
        raises (DECISIONS "Shortfall semantics") — pre-history is a
        data-quality fact, and the engine books the uncovered remainder
        as a zero-cost synthetic flagged ``missing_basis``.

        ``DecimalsMismatchError`` if ``needed`` is not at the ledger's
        scale. ``ValidationError`` for an incoherent plan: a take of
        ``raw <= 0``, a take exceeding its lot's remaining units, a lot
        not held by this ledger, or a plan totalling more than
        ``needed``.
        """
        if self._decimals not in (None, needed.decimals):
            raise DecimalsMismatchError(f"ledger scale {self._decimals}")
        consumed: list[ConsumedPiece] = []
        taken = 0
        for lot, take in self._plan(needed, selector):
            portion: Fraction | None = None
            if lot.cost_total is not None and lot.cost_remaining is not None:
                basis = Fraction(lot.cost_total.amount)
                portion = basis * take.raw / lot.quantity_original.raw
                lot.cost_remaining -= portion
            lot.quantity_remaining -= take
            consumed.append((lot, take, portion))
            taken += take.raw
        return consumed, Quantity(needed.raw - taken, needed.decimals)


def derive_events(
    transactions: Sequence[LedgerTransaction],
    internal_transfer_ids: frozenset[str] = frozenset(),
) -> tuple[AccountingEvent, ...]:
    """Distil ledger transactions into the taxable event stream (SPEC §9).

    Per entry: ``Direction.IN`` yields an ``AcquisitionEvent`` with
    ``cost=None``, ``Direction.OUT`` a ``DisposalEvent`` with
    ``proceeds=None`` — this module carries totals only when a caller
    supplies them, so pricing stays out of the accounting layer.

    Three things produce NO event at all:

    * ``Direction.SELF`` entries — moving your own coins is not income;
    * every entry of a transaction whose ``id`` is in
      ``internal_transfer_ids`` — the whole transaction is skipped, both
      legs, which is what ``is_internal_transfer`` is for;
    * every entry of a ``removed=True`` transaction — a reorged-away
      transaction never happened.

    ``at_ms`` is ``confirmed_at`` when it is not ``None``, else
    ``initiated_at`` (DECISIONS "Accounting event time"). The result is
    sorted by ``at_ms`` STABLY, so entries sharing a timestamp keep the
    order they arrived in — replay is deterministic.
    """
    events: list[AccountingEvent] = []
    for transaction in transactions:
        if transaction.removed or transaction.id in internal_transfer_ids:
            continue
        at_ms = transaction.confirmed_at
        if at_ms is None:
            at_ms = transaction.initiated_at
        for entry in transaction.entries:
            build = _EVENT_BY_DIRECTION.get(entry.direction)
            if build is None:  # Direction.SELF — not a taxable event
                continue
            events.append(
                build(at_ms, entry.asset_id, entry.quantity, None, transaction.id)
            )
    events.sort(key=lambda event: event.at_ms)
    return tuple(events)


def exact_mul(a: Decimal, b: Decimal) -> Decimal:
    """Context-free exact product of two decimals (DECISIONS pinned).

    Sign is the XOR of the operand signs, the coefficient is the integer
    product of the operand coefficients, the exponent is their sum.
    Never context-rounded: a 40-digit operand survives intact, and
    ``exact_mul(Decimal('10'), Decimal('3584.17'))`` is exactly
    ``Decimal('35841.70')``, trailing zero preserved.

    Value-identical to ``positions.drill.exact_mul`` under the
    duplication waiver — accounting may not import positions.
    """
    a_sign, a_digits, a_exponent = a.as_tuple()
    b_sign, b_digits, b_exponent = b.as_tuple()
    a_coefficient = int("".join(map(str, a_digits)))
    b_coefficient = int("".join(map(str, b_digits)))
    digits = tuple(int(char) for char in str(a_coefficient * b_coefficient))
    return Decimal((a_sign ^ b_sign, digits, a_exponent + b_exponent))


def fraction_to_money(value: Fraction, currency: str = "USD") -> tuple[Money, bool]:
    """The single rounding boundary (DECISIONS "Fraction->Money boundary").

    Returns ``(money, is_exact)``. When ``value`` reduced has a
    denominator of the form ``2**a * 5**b`` the decimal expansion
    terminates: the Money is EXACT at whatever length that takes — 35
    significant digits if that is what ``1/2**50`` needs — and
    ``is_exact`` is ``True``. The exact branch scales by
    ``10 ** max(a, b)``, the shortest terminating form, so the amount
    carries no padding zeros: ``Fraction(3, 4)`` is ``Decimal("0.75")``.
    Otherwise the value is rounded
    ROUND_HALF_EVEN to :data:`SIGNIFICANT_DIGITS` and ``is_exact`` is
    ``False``, which the caller reports as the ``"rounded_basis"`` flag.

    Rounding lives here and nowhere else: every lot and pool computation
    upstream is exact rational, so this is the only place a cent can be
    lost, and it always says so.
    """
    residual, twos, fives = value.denominator, 0, 0
    while residual % 2 == 0:
        residual, twos = residual // 2, twos + 1
    while residual % 5 == 0:
        residual, fives = residual // 5, fives + 1
    if residual != 1:
        with localcontext() as context:
            context.prec = SIGNIFICANT_DIGITS
            context.rounding = ROUND_HALF_EVEN
            rounded = Decimal(value.numerator) / Decimal(value.denominator)
        return Money(rounded, currency), False
    scale = max(twos, fives)
    scaled = value.numerator * 10**scale // value.denominator
    digits = tuple(int(char) for char in str(abs(scaled)))
    return Money(Decimal((1 if scaled < 0 else 0, digits, -scale)), currency), True
