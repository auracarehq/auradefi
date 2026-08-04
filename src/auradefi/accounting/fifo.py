"""FIFO (first-in, first-out) consumption-order selector (SPEC §9).

One public function — ``select`` — returning an ordered consumption
**plan**. The plan is advice, not an effect: it mutates nothing, computes
no cost, and prorates nothing. Proration, cost math, and lot mutation are
centralised in ``LotLedger.consume``; a selector only decides *which lots,
in what order, for how much*.

Wave independence is structural, not conventional: this module reads
exactly two attributes of a lot — ``opened_at_ms`` (ms-epoch ``int``) and
``quantity_remaining`` (``auradefi.money.quantity.Quantity``) — and
imports ``Lot`` only under ``TYPE_CHECKING``. There is no runtime
cross-module import, so anything lot-shaped selects correctly and the
module stays importable on its own.

Pure: ``auradefi.money`` semantics plus the standard library.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only; never imported at runtime
    from collections.abc import Sequence

    from auradefi.accounting.lots import Lot
    from auradefi.money.quantity import Quantity


def select(lots: Sequence[Lot], needed: Quantity) -> list[tuple[Lot, Quantity]]:
    """Plan the FIFO consumption of ``needed`` across ``lots``.

    Order: ascending ``(opened_at_ms, input position)`` — oldest first,
    ties broken by the *earlier* position in ``lots``. The walk takes
    ``min(unmet need, lot.quantity_remaining)`` from every lot whose
    ``quantity_remaining.raw > 0`` and stops the moment the plan sums to
    ``needed``.

    Returns ``(lot, take)`` pairs in consumption order. Every ``take`` is
    a positive ``Quantity`` at ``needed.decimals``; a lot contributing
    nothing never appears. The lot objects returned are the very objects
    passed in — identity is what lets the caller mutate them — and
    ``lots`` itself is never reordered in place.

    Shortage is never an error: when the live lots hold less than
    ``needed`` the plan sums to exactly the total held, and the caller
    books the shortfall (DECISIONS: shortfall semantics — pre-history is a
    data-quality fact, not an exception). ``needed.raw <= 0`` yields
    ``[]`` and no lot is inspected at all.

    Raises ``DecimalsMismatchError`` (``auradefi.errors``) naturally, out
    of ``Quantity`` arithmetic, when a live lot's ``quantity_remaining``
    carries different ``decimals`` than ``needed``. Drained lots
    (``raw == 0``) and negative-remaining lots are filtered by the
    ``raw > 0`` test *before* any arithmetic touches them, so their scale
    is never compared.
    """
    if needed.raw <= 0:
        return []
    return _walk(_oldest_first(lots), needed)


def _oldest_first(lots: Sequence[Lot]) -> list[Lot]:
    """``lots`` ordered by ascending ``opened_at_ms``, ties by position.

    ``sorted`` is stable, so equal timestamps keep their input order, and
    the result is a new list — ``lots`` is never reordered in place.
    """
    return sorted(lots, key=lambda candidate: candidate.opened_at_ms)


def _walk(ordered: list[Lot], needed: Quantity) -> list[tuple[Lot, Quantity]]:
    """Take ``min(unmet, remaining)`` down ``ordered`` until the need is met.

    The ``raw > 0`` guard precedes every comparison, so a drained lot of a
    foreign scale is skipped rather than raising. Running out of live lots
    simply ends the walk: the plan then sums to the total held.
    """
    unmet = needed
    plan: list[tuple[Lot, Quantity]] = []
    for candidate in ordered:
        available = candidate.quantity_remaining
        if available.raw <= 0:
            continue
        take = available if available < unmet else unmet
        plan.append((candidate, take))
        unmet = unmet - take
        if unmet.raw == 0:
            break
    return plan
