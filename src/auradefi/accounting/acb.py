"""ACB — Canadian adjusted cost base, a pooled average (SPEC §9).

Under ACB there is no per-lot basis at all: every acquisition of an asset
melts into one running pool and a disposal consumes a pro-rata slice of
it. docs/DECISIONS.md "ACB pooling" pins the mechanics:

  * the pool is ``(total_cost: exact Fraction | None, total base-unit
    quantity)`` per asset;
  * an acquisition adds raw units and adds its cost;
  * a disposal consumes ``total_cost * take_raw / pool_raw`` EXACTLY, as a
    rational — no rounding lives in the pool. Rounding exists only at the
    ``Fraction``→``Money`` boundary, and it is always flagged;
  * an UNPRICED acquisition sets the pool cost to ``None`` PERMANENTLY.
    A poisoned pool is an honest "unknown basis" forever after; averaging
    an unknown into a known would manufacture a number nobody can defend,
    and later priced acquisitions cannot un-poison it.

Lots remain ground truth for open-lot reporting — the pool is a costing
overlay laid over them, never a replacement. That is why :func:`select`
still walks lots oldest-first: the engine needs the QUANTITY bookkeeping
(which lots are drawn down, and by how much) even though it ignores the
per-lot basis portions that walk would imply under FIFO.

That oldest-first walk is a deliberate restatement of ``fifo``'s, on the
identical terms (no mutation, no cost math in the selector, only
``opened_at_ms`` and ``quantity_remaining`` read, ``Lot`` imported under
``TYPE_CHECKING`` alone) — docs/DECISIONS.md "Duplication waiver
extension": same-wave disjoint ownership forbids the runtime import, and
golden vectors pin both copies.

Pure: ``auradefi.money`` semantics plus the standard library.
:class:`AcbPool` is the one mutable thing here, by design — it is a
running accumulator, like ``Lot`` inside ``LotLedger``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING

from auradefi.errors import (
    CurrencyMismatchError,
    DecimalsMismatchError,
    ValidationError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; never imported at runtime
    from collections.abc import Sequence

    from auradefi.accounting.lots import Lot
    from auradefi.money.fiat import Money
    from auradefi.money.quantity import Quantity


@dataclass(slots=True)
class AcbPool:
    """One asset's running pooled cost, in one currency.

    MUTABLE by the same deliberate deviation ``Lot`` makes: a pool is an
    accumulator that acquisitions and disposals move in place.

    ``cost`` starts at ``Fraction(0)`` and stays an exact rational for the
    pool's whole life — unless an unpriced acquisition sets it to
    ``None``, which is permanent. ``quantity_raw`` is the pooled base-unit
    count. ``decimals`` is learned from the first quantity the pool sees
    and every later quantity must match it — a pool that silently mixed
    scales would be wrong by powers of ten.
    """

    currency: str
    cost: Fraction | None = field(default_factory=lambda: Fraction(0))
    quantity_raw: int = 0
    decimals: int | None = None

    def __post_init__(self) -> None:
        """Validate the pool's own fields; ``ValidationError`` otherwise.

        ``currency`` must be a ``str``; ``quantity_raw`` a non-negative
        ``int``; ``decimals`` ``None`` or an ``int >= 0``. ``bool`` is
        rejected before the ``int`` check for ``quantity_raw`` and
        ``decimals`` — ``bool`` is an ``int`` subclass and is never an
        amount or a scale.
        """
        if not isinstance(self.currency, str):
            raise ValidationError(
                f"currency must be a str, got {type(self.currency).__name__}"
            )
        if isinstance(self.quantity_raw, bool):
            raise ValidationError("quantity_raw must be an int, got bool")
        if not isinstance(self.quantity_raw, int):
            raise ValidationError(
                f"quantity_raw must be an int, got "
                f"{type(self.quantity_raw).__name__}"
            )
        if self.quantity_raw < 0:
            raise ValidationError(
                f"quantity_raw must be >= 0, got {self.quantity_raw}"
            )
        if self.decimals is None:
            return
        if isinstance(self.decimals, bool):
            raise ValidationError("decimals must be an int, got bool")
        if not isinstance(self.decimals, int):
            raise ValidationError(
                f"decimals must be an int, got {type(self.decimals).__name__}"
            )
        if self.decimals < 0:
            raise ValidationError(f"decimals must be >= 0, got {self.decimals}")

    @property
    def poisoned(self) -> bool:
        """``True`` once an unpriced acquisition erased the pool's cost.

        Equivalent to ``self.cost is None``, named so callers can say what
        they mean.
        """
        return self.cost is None

    def _require_same_scale(self, quantity: Quantity) -> None:
        """Raise ``DecimalsMismatchError`` unless the scales agree.

        A pool that silently mixed scales would be wrong by powers of ten.
        A pool that has not yet learned a scale accepts any.
        """
        if self.decimals is not None and quantity.decimals != self.decimals:
            raise DecimalsMismatchError(
                f"decimals mismatch: {self.decimals} vs {quantity.decimals}"
            )

    def acquire(self, quantity: Quantity, cost: Money | None) -> None:
        """Add an acquisition to the pool, in place.

        ``quantity.raw`` is added to ``quantity_raw``. When ``cost`` is
        not ``None`` and the pool is not already poisoned,
        ``Fraction(cost.amount)`` is added to ``cost`` exactly.

        When ``cost is None`` the pool's ``cost`` becomes ``None``
        PERMANENTLY: later priced acquisitions add quantity but never
        resurrect a basis.

        The first acquisition of a fresh pool sets ``decimals``.

        Raises ``ValidationError`` if ``quantity.raw < 0`` — an
        acquisition of negative units is a caller bug, and a disposal is
        :meth:`dispose`. Raises ``CurrencyMismatchError`` if
        ``cost.currency`` differs from ``self.currency``, and
        ``DecimalsMismatchError`` if the pool already has ``decimals`` and
        ``quantity.decimals`` differs.
        """
        if quantity.raw < 0:
            raise ValidationError(
                f"an acquisition cannot be negative, got {quantity.raw} "
                f"base units (a disposal is dispose)"
            )
        self._require_same_scale(quantity)
        if cost is not None and cost.currency != self.currency:
            raise CurrencyMismatchError(
                f"currency mismatch: {self.currency!r} vs {cost.currency!r}"
            )
        if self.decimals is None:
            self.decimals = quantity.decimals
        self.quantity_raw += quantity.raw
        if cost is None:
            self.cost = None  # permanent: an honest unknown basis
        elif self.cost is not None:
            self.cost += Fraction(cost.amount)

    def dispose(self, quantity: Quantity) -> Fraction | None:
        """Consume ``quantity`` from the pool; return the basis consumed.

        With ``take_raw = quantity.raw`` and ``pool_raw = quantity_raw``,
        the consumed basis is the EXACT rational
        ``cost * take_raw / pool_raw``, and the pool becomes
        ``(cost - consumed, pool_raw - take_raw)``. Repeated disposals
        therefore sum back to the original pooled cost with zero drift —
        the property a ``Decimal`` pool cannot offer.

        Returns ``None`` — consuming no basis — when the pool is poisoned,
        at any ``take_raw``. The quantity bookkeeping still happens: a
        poisoned pool tracks units exactly.

        ``take_raw == 0`` returns ``Fraction(0)`` (or ``None`` when
        poisoned) and leaves the pool untouched, including on an empty
        pool, where the pro-rata division would otherwise be undefined.

        Raises ``ValidationError`` if ``take_raw < 0``, or if
        ``take_raw > quantity_raw`` — an overdraw, leaving the pool
        unchanged. Shortfall is the ENGINE's business: it clamps to what
        the pool holds and books the uncovered remainder (DECISIONS
        "Shortfall semantics"), so a pool asked to overdraw has already
        been handed a bug. Raises ``DecimalsMismatchError`` if
        ``quantity.decimals`` differs from the pool's.
        """
        take_raw = quantity.raw
        if take_raw < 0:
            raise ValidationError(
                f"a disposal cannot be negative, got {take_raw} base units"
            )
        self._require_same_scale(quantity)
        if take_raw > self.quantity_raw:
            raise ValidationError(
                f"disposal of {take_raw} base units overdraws a pool holding "
                f"{self.quantity_raw} — the engine clamps and books the "
                f"shortfall before it reaches the pool"
            )
        pooled = self.cost
        if take_raw == 0:
            return None if pooled is None else Fraction(0)
        if pooled is None:
            self.quantity_raw -= take_raw  # units stay exact while basis is not
            return None
        consumed = pooled * take_raw / self.quantity_raw
        self.cost = pooled - consumed
        self.quantity_raw -= take_raw
        return consumed


def select(lots: Sequence[Lot], needed: Quantity) -> list[tuple[Lot, Quantity]]:
    """Plan the ACB consumption of ``needed`` across ``lots``.

    Quantity bookkeeping only: the plan says WHICH lots are drawn down and
    by how much, so open-lot reporting stays truthful. The costing comes
    from :class:`AcbPool`, and the engine ignores whatever per-lot basis
    this ordering would have implied.

    Order: ascending ``(opened_at_ms, input position)`` — oldest first,
    ties broken by the earlier position in ``lots`` — identical to FIFO's,
    restated per the duplication waiver. The walk takes
    ``min(unmet need, lot.quantity_remaining)`` from every lot whose
    ``quantity_remaining.raw > 0`` and stops the moment the plan sums to
    ``needed``.

    Returns ``(lot, take)`` pairs in consumption order. Every ``take`` is
    a positive ``Quantity`` at ``needed.decimals``; a lot contributing
    nothing never appears. The lot objects returned are the very objects
    passed in, and ``lots`` itself is never reordered in place.

    Shortage is never an error: the plan sums to exactly the total held
    and the caller books the shortfall. ``needed.raw <= 0`` yields ``[]``
    and no lot is inspected at all.

    Raises ``DecimalsMismatchError`` (``auradefi.errors``) naturally, out
    of ``Quantity`` arithmetic, when a live lot's ``quantity_remaining``
    carries different ``decimals`` than ``needed``. Drained and
    negative-remaining lots are filtered by the ``raw > 0`` test *before*
    any arithmetic touches them, so their scale is never compared.
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
