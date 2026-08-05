"""Upsert diffing for the ledger (SPEC §6.4 sync semantics).

``classify`` is a pure function: it splits an incoming batch against the
stored state (keyed by id) into new / changed / unchanged buckets using
``auradefi.ledger.models.payload_equal``, so backend bookkeeping
(``last_modified_seq``, ``removed``) never makes a re-delivered
transaction look changed. Backends compose it; it performs no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from auradefi.errors import ValidationError
from auradefi.ledger.models import LedgerTransaction, payload_equal


@dataclass(frozen=True, slots=True)
class UpsertPlan:
    """Result of :func:`classify`: what to insert, update, and skip.

    ``new`` and ``changed`` carry the incoming transactions to write;
    ``unchanged`` carries ids only. There is nothing to write for them.
    """

    new: tuple[LedgerTransaction, ...]
    changed: tuple[LedgerTransaction, ...]
    unchanged: tuple[str, ...]


def classify(
    incoming: Sequence[LedgerTransaction],
    existing: Mapping[str, LedgerTransaction],
) -> UpsertPlan:
    """Pure diff of ``incoming`` against ``existing`` (keyed by id).

    - id absent from ``existing`` -> ``new``
    - id present and ``payload_equal`` is False -> ``changed``
    - id present and ``payload_equal`` is True -> ``unchanged`` (id only)
    - duplicate ids WITHIN ``incoming`` ->
      ``auradefi.errors.ValidationError``, before any classification

    Incoming order is preserved within each bucket. Neither argument is
    mutated.
    """
    seen: set[str] = set()
    for txn in incoming:
        if txn.id in seen:
            raise ValidationError(
                f"duplicate transaction id within incoming batch: {txn.id!r}"
            )
        seen.add(txn.id)

    new: list[LedgerTransaction] = []
    changed: list[LedgerTransaction] = []
    unchanged: list[str] = []
    for txn in incoming:
        stored = existing.get(txn.id)
        if stored is None:
            new.append(txn)
        elif payload_equal(txn, stored):
            unchanged.append(txn.id)
        else:
            changed.append(txn)
    return UpsertPlan(
        new=tuple(new), changed=tuple(changed), unchanged=tuple(unchanged)
    )
