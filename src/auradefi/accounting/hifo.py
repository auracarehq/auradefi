"""HIFO (highest-in, first-out) consumption-order selector (SPEC §9).

Same terms as ``auradefi.accounting.fifo``: ``select`` returns an ordered
consumption **plan**: advice, not an effect. It mutates nothing, computes
no basis and prorates nothing; proration and lot mutation live in
``LotLedger.consume``. Wave independence is structural: only
``opened_at_ms``, ``quantity_original``, ``quantity_remaining`` and
``cost_total`` are ever read, and ``Lot`` is imported under
``TYPE_CHECKING`` alone, so the greedy walk is restated here rather than
shared (docs/internal/DECISIONS.md "Duplication waiver extension").

HIFO consumes the most expensive basis first, which minimises realised
gain. The order is pinned:

1. lots with a known ``cost_total`` sort BEFORE lots without one. An
   unknown basis is consumed only after every priced lot is exhausted,
   because an unknown must never displace a known;
2. among priced lots, DESCENDING unit cost;
3. ties break oldest-first (``opened_at_ms`` ascending), then by the
   earlier position in ``lots``: one reproducible plan per input.

The key is the exact rational :func:`unit_cost`, never a float and never a
context-precision ``Decimal`` division: at the default 28 digits both of
those collapse ``10/3`` onto ``3.3333333333333333333333333333`` and
silently misorder the plan. Exact division is legitimate because a
selector only ever sees ONE asset's lots, so the quotient is only ever
compared with quotients of the same kind, no rounding is required, and
none happens.

Pure: ``auradefi.money`` semantics plus the standard library.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING

from auradefi.errors import ValidationError

if TYPE_CHECKING:  # pragma: no cover - typing only; never imported at runtime
    from collections.abc import Sequence

    from auradefi.accounting.lots import Lot
    from auradefi.money.quantity import Quantity


def unit_cost(lot: Lot) -> Fraction | None:
    """The lot's exact basis per base unit. HIFO's ordering key.

    ``Fraction(lot.cost_total.amount) / Fraction(lot.quantity_original.raw)``,
    an exact rational. The divisor is the ORIGINAL quantity, not the
    remaining one: unit cost is a fact of the acquisition and does not
    move as the lot is drawn down. This is the same ratio
    ``LotLedger.consume`` prorates with, so ordering and costing can never
    disagree.

    Public because it is the pinned key, not an implementation detail: a
    change to it reorders every HIFO plan ever computed.

    Returns ``None`` iff ``lot.cost_total is None``. Raises
    ``ValidationError`` (``auradefi.errors``) when a PRICED lot has
    ``quantity_original.raw == 0``: cost per unit over zero units is
    undefined, and a bare ``ZeroDivisionError`` would escape the error
    taxonomy.
    """
    cost_total = lot.cost_total
    if cost_total is None:
        return None
    original_raw = lot.quantity_original.raw
    if original_raw == 0:
        raise ValidationError(
            "unit cost is undefined for a priced lot of zero original quantity"
        )
    return Fraction(cost_total.amount) / Fraction(original_raw)


def select(lots: Sequence[Lot], needed: Quantity) -> list[tuple[Lot, Quantity]]:
    """Plan the HIFO consumption of ``needed`` across ``lots``.

    Order: priced lots by descending :func:`unit_cost`, then unpriced
    lots; within either group ties break by ascending
    ``(opened_at_ms, input position)``. The walk takes
    ``min(unmet need, lot.quantity_remaining)`` from every lot whose
    ``quantity_remaining.raw > 0`` and stops the moment the plan sums to
    ``needed``.

    Returns ``(lot, take)`` pairs in consumption order. Every ``take`` is
    a positive ``Quantity`` at ``needed.decimals``; a lot contributing
    nothing never appears. The lot objects returned are the very objects
    passed in, identity is what lets the caller mutate them, and
    ``lots`` itself is never reordered in place.

    Shortage is never an error: when the live lots hold less than
    ``needed`` the plan sums to exactly the total held, and the caller
    books the shortfall (DECISIONS "Shortfall semantics", pre-history is
    a data-quality fact, not an exception). ``needed.raw <= 0`` yields
    ``[]`` and no lot is inspected at all.

    Raises ``DecimalsMismatchError`` (``auradefi.errors``) naturally, out
    of ``Quantity`` arithmetic, when a live lot's ``quantity_remaining``
    carries different ``decimals`` than ``needed``. Drained lots
    (``raw == 0``) and negative-remaining lots are filtered by the
    ``raw > 0`` test *before* any arithmetic or any keying touches them,
    so neither their scale nor their unit cost is ever computed:
    a drained zero-quantity lot cannot raise out of :func:`unit_cost`.
    """
    if needed.raw <= 0:
        return []
    return _walk(_dearest_first(_live(lots)), needed)


def _live(lots: Sequence[Lot]) -> list[Lot]:
    """The lots still holding something, in input order.

    Filtering precedes both the keying and the arithmetic, which is what
    lets a spent lot of zero original quantity exist at all: its unit cost
    is never computed, so it cannot raise, and its scale is never compared.
    """
    return [candidate for candidate in lots if candidate.quantity_remaining.raw > 0]


def _rank(candidate: Lot) -> tuple[int, Fraction, int]:
    """``(unpriced?, -unit cost, opened_at_ms)``. Ascending IS HIFO order.

    The leading flag puts every priced lot ahead of every unpriced one, so
    the placeholder the unpriced branch carries is only ever compared with
    other placeholders. Negating the exact rational turns one ascending
    sort into a descending one, no second pass, no lossy key.
    """
    cost = unit_cost(candidate)
    if cost is None:
        return (1, Fraction(0), candidate.opened_at_ms)
    return (0, -cost, candidate.opened_at_ms)


def _dearest_first(live: list[Lot]) -> list[Lot]:
    """``live`` in HIFO order: dearest priced lot first, unpriced last.

    ``sorted`` is stable, so lots tying on the whole of :func:`_rank` keep
    their input order, one reproducible plan per input, and the result
    is a new list, so the caller's sequence is never reordered in place.
    """
    return sorted(live, key=_rank)


def _walk(ordered: list[Lot], needed: Quantity) -> list[tuple[Lot, Quantity]]:
    """Take ``min(unmet, remaining)`` down ``ordered`` until the need is met.

    ``ordered`` is already filtered to live lots, so every entry
    contributes a positive take. Running out of lots simply ends the walk:
    the plan then sums to the total held.
    """
    unmet = needed
    plan: list[tuple[Lot, Quantity]] = []
    for candidate in ordered:
        available = candidate.quantity_remaining
        take = available if available < unmet else unmet
        plan.append((candidate, take))
        unmet = unmet - take
        if unmet.raw == 0:
            break
    return plan
