"""Incremental PnL over the lot ledger: the replay engine (SPEC §9).

Zerion pre-computes PnL at fixed marks and errors out when more than
3,000 transactions sit between a requested date and the nearest one, so
arbitrary-date PnL is effectively unavailable on an active wallet.
:func:`pnl_at` answers it by REPLAY instead: one pass over the events at
or before the cutoff, then a report. Nothing is pre-computed, no clock is
read, and a date is merely where the replay stops, which is why any
instant costs the same as any other.

This module is the ADVANCING half of the engine; the projection that
turns its state into an answer lives in
:mod:`auradefi.accounting.report`, and the dependency runs one way only.
What is here:

* :data:`METHODS`: the pluggability. SPEC §9 names four costing
  methods, and indexing this table with anything else raises
  ``ValidationError`` rather than leaking a ``KeyError``.
* :class:`PnLState` / :func:`process`: incremental and snapshotable.
  Replaying head then tail into one state must report identically to
  replaying head + tail in a single pass, which is what makes a long
  history reportable at many cutoffs without re-reading it each time.
* :class:`DisposalRecord`: one disposal's realised outcome, flagged.
* ``_ReplayLedger``. The optimisation that keeps the whole thing inside
  the phase 9 budget (SPEC §11).

Two pins govern the numbers. FIFO/LIFO/HIFO take cost from the pieces the
selector actually consumed; ACB takes it from the per-asset
:class:`~auradefi.accounting.acb.AcbPool`, an overlay whose average is
what that method costs with, while lots remain ground truth for open-lot
reporting (DECISIONS "ACB pooling"). And outrunning the units held never
raises, pre-history is a data-quality fact, booked as a zero-cost
synthetic and flagged (DECISIONS "Shortfall semantics"), while an
unknown propagates as ``None`` rather than becoming a zero (DECISIONS
"None-propagation (PnL)").

Pure: ``auradefi.money``, ``auradefi.accounting`` and the standard
library. No I/O, no clock. An event's time comes from the transaction
that produced it.

:func:`report`, :class:`PnLReport` and :data:`DEFAULT_CURRENCY` are
imported from the sibling module and re-exported here: they are part of
this module's own signatures, and callers established before the split
import them from this path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction

from auradefi.accounting import acb, fifo, hifo, lifo
from auradefi.accounting.acb import AcbPool
from auradefi.accounting.lots import (
    AccountingEvent, AcquisitionEvent, DisposalEvent, Lot, LotLedger,
    LotSelector, fraction_to_money,
)
from auradefi.accounting.report import DEFAULT_CURRENCY, PnLReport, report
from auradefi.errors import CurrencyMismatchError, DecimalsMismatchError, ValidationError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

__all__ = [
    "DEFAULT_CURRENCY",
    "METHODS",
    "DisposalRecord",
    "PnLReport",
    "PnLState",
    "pnl_at",
    "process",
    "report",
]


class _MethodTable(dict[str, LotSelector]):
    """:data:`METHODS`'s type: a mapping whose miss is a domain error.

    A bare ``KeyError`` escaping into a caller would be the one exception
    this package raises that is not part of the ``auradefi.errors``
    taxonomy, so a typo'd method name would surface as an unhandled
    builtin rather than as the ``ValidationError`` every other bad input
    produces.
    """

    def __missing__(self, method: str) -> LotSelector:
        """Raise ``ValidationError`` naming the methods that do exist."""
        raise ValidationError(
            f"unknown accounting method {method!r}; SPEC §9 names "
            f"{', '.join(sorted(self))}"
        )


#: The four costing methods SPEC §9 names, mapped to the selector
#: functions themselves. The methods live in sibling modules and are
#: plugged in here, never imported by one another. Indexing with any
#: other name raises ``ValidationError``.
METHODS: Mapping[str, LotSelector] = _MethodTable(
    {"fifo": fifo.select, "lifo": lifo.select, "hifo": hifo.select, "acb": acb.select}
)


@dataclass(frozen=True, slots=True)
class DisposalRecord:
    """One disposal's realised outcome, as the engine booked it.

    ``cost_basis`` is where the exact rational basis crosses the
    ``Fraction`` -> ``Money`` boundary; when that conversion had to round,
    ``flags`` carries ``"rounded_basis"`` (DECISIONS "Fraction->Money
    boundary"). It is ``None``, flagged ``"missing_cost"``, when a
    consumed lot was unpriced.

    ``realized`` is ``proceeds - cost_basis`` only when BOTH are known,
    and ``None`` otherwise, flagged ``"missing_proceeds"`` and/or
    ``"missing_cost"``: an unknown outcome is never reported as a zero
    gain (DECISIONS "None-propagation (PnL)").

    ``missing_basis`` is the separate, orthogonal fact that the disposal
    outran the units held. The uncovered remainder was booked at zero
    cost and flagged ``"missing_basis"`` (DECISIONS "Shortfall
    semantics"). A shortfall does NOT make ``realized`` unknown; it makes
    it optimistic, and says so.

    ``flags`` is ordered as the facts land: shortfall, rounding, unknown
    cost, unknown proceeds.
    """

    at_ms: int
    asset_id: str
    quantity: Quantity
    proceeds: Money | None
    cost_basis: Money | None
    realized: Money | None
    missing_basis: bool
    flags: tuple[str, ...]


@dataclass(slots=True)
class PnLState:
    """Replay state: the accumulator :func:`process` advances in place.

    MUTABLE by the same deliberate deviation from the frozen-value house
    style that :class:`~auradefi.accounting.lots.Lot` and
    :class:`~auradefi.accounting.acb.AcbPool` make: replay decrements
    lots, and a state that copied itself per event would rebuild the
    whole ledger 50,000 times. Everything a caller receives OUT of the
    engine, :class:`DisposalRecord`, and everything in
    :mod:`auradefi.accounting.report`, is frozen.

    ``currency`` is ``None`` until the first priced ``Money`` fixes it,
    after which every priced value in the stream must agree
    (``CurrencyMismatchError`` otherwise); a stream that was never priced
    reports in :data:`DEFAULT_CURRENCY`. ``last_at_ms`` is the monotonic
    watermark that lets a snapshot be resumed safely. ``pools`` fills
    only under ``"acb"``. ``scales`` remembers each asset's ``decimals``,
    so an asset sold down to nothing still reports zero at its own scale
    rather than at scale 0.
    """

    method: str
    currency: str | None = None
    last_at_ms: int = 0
    disposals: list[DisposalRecord] = field(default_factory=list)
    ledgers: dict[str, LotLedger] = field(default_factory=dict)
    pools: dict[str, AcbPool] = field(default_factory=dict)
    scales: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """``ValidationError`` unless ``method`` is one of :data:`METHODS`:
        a state built for a method that does not exist would fail much
        later, mid-replay, with lots already consumed."""
        METHODS[self.method]  # the lookup IS the validation


class _ReplayLedger(LotLedger):
    """A ``LotLedger`` that drops exhausted lots from its working set.

    Same lot ids, same proration, same exception types as the base class,
    but a disposal costs O(open lots) rather than O(lots ever opened).
    The base class rescans the whole history twice per disposal, once to
    build ``open_lots`` for the selector, once to build the budget dict,
    which is quadratic over a replay: three times slower at 50,000
    events, and over the phase 9 budget (SPEC §11). The perf gate exists
    precisely because a quadratic engine passes every golden vector while
    missing the point of SPEC §9.

    The trade is that history is NOT retained: ``lots`` equals
    ``open_lots``, so this ledger cannot report on closed lots. Nothing
    in the reporting projection needs them. Lot ids stay stable
    regardless, their per-transaction counters living in a separate dict
    that is never pruned, so an id is never reused.
    """

    def _plan(
        self, needed: Quantity, selector: LotSelector
    ) -> list[tuple[Lot, Quantity]]:
        """Drop spent lots, then validate the selector's plan IN FULL
        before anything is decremented. A half-applied plan would
        corrupt basis silently.

        Membership is checked by asset identity and remaining units
        rather than against a dict built from every lot ever opened,
        which is what removes the second full scan. The validation is
        otherwise identical to the base class: a take must be a
        ``Quantity`` at the disposal's scale, within its lot's remaining
        units, and the plan may not total more than ``needed``.
        """
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

    An acquisition opens a lot, and, under ``"acb"``, joins the pool; a
    disposal consumes the selector's plan and books a
    :class:`DisposalRecord`. The state is advanced IN PLACE and also
    returned, so ``process(tail, m, process(head, m))`` reports
    identically to one pass over ``head + tail``. That equivalence is the
    whole point: a caller can replay a wallet once and take reports at
    many cutoffs, instead of re-reading the history per cutoff.

    ``events`` is consumed lazily, so a generator that filters by time
    never materialises the events it discards.

    Raises ``ValidationError`` for an unknown ``method``, for a ``method``
    disagreeing with ``state.method``, a lot ledger cannot switch
    costing method mid-stream, because the lots it already consumed were
    chosen by the old one, and for an event older than
    ``state.last_at_ms``: input must be monotonic, since a lot ledger
    cannot un-consume. Equal timestamps are allowed and keep caller
    order, which is what makes replay deterministic when a block's
    transactions share an instant. Raises ``CurrencyMismatchError`` on a
    priced value denominated in another currency.
    """
    if state is None:
        state = PnLState(method)
    elif state.method != method:
        raise ValidationError(
            f"state replays {state.method!r}, not {method!r}: a lot ledger "
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

    Mixing denominations silently is the failure that makes a tax report
    wrong without looking wrong, so the first priced ``Money`` fixes the
    stream and every later one must match. Pools opened before anything
    was priced adopt the currency too: such a pool is either empty or
    already poisoned by an unpriced acquisition, so retagging it invents
    no number.
    """
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
    """The asset's ACB pool, opened in the stream's currency on demand.

    Pools are per-asset by construction: cross-asset averaging is the bug
    that corrupts a tax report silently (DECISIONS "ACB pooling").
    """
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
    """Consume the plan and book one disposal.

    Under ``"acb"`` the basis comes from the pool's average, capped at
    the units the pool actually holds. Per-lot portions are meaningless
    once cost has been pooled, so the lots are still consumed (they stay
    ground truth for open-lot reporting) but do not supply the number.
    Under the other three methods the basis is the exact sum of the
    portions the selector consumed, and becomes ``None`` as soon as ONE
    consumed lot was unpriced rather than under-counting a partial sum.

    Flags are appended in the order the facts land: shortfall, rounding,
    unknown cost, unknown proceeds. A shortfall never raises: it books
    the uncovered units at zero cost (DECISIONS "Shortfall semantics").
    """
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


def pnl_at(
    events: Iterable[AccountingEvent],
    method: str,
    at_ms: int,
    marks: Mapping[str, Money],
) -> PnLReport:
    """PnL at an ARBITRARY date. The thing Zerion cannot do (SPEC §9).

    Replays only the events at or before ``at_ms``, in ONE pass, and
    reports as of that cutoff. Exactly equivalent to filtering the stream
    by time, calling :func:`process` and then
    :func:`~auradefi.accounting.report.report`, no marks are
    pre-computed at fixed dates, and no cutoff is privileged over any
    other, so a date 3,000 transactions from the nearest month end costs
    what any other date costs.

    The filter is a generator, so events after the cutoff are skipped
    without being materialised. ``marks`` prices the units still held at
    the cutoff and follows the same rules as in
    :func:`~auradefi.accounting.report.report`.
    """
    admitted = (event for event in events if event.at_ms <= at_ms)
    return report(process(admitted, method), at_ms, marks)
