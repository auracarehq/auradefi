"""Reorg planning (SPEC §6.4: a chain reorg is removed + re-added).

``plan_reorg`` is a pure function over the stored view of one chain and
the canonical view from a fork point. It emits a :class:`ReorgPlan` that
a backend applies as ``mark_removed`` + ``upsert`` — a first-class
REMOVED/ADDED event pair, never an in-place mutation or a magic boolean.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from auradefi.errors import ValidationError
from auradefi.ledger.models import LedgerTransaction, payload_equal


@dataclass(frozen=True, slots=True)
class ReorgPlan:
    """What a reorg changes: ids to remove, transactions to (re-)add."""

    remove_ids: tuple[str, ...]
    add: tuple[LedgerTransaction, ...]


def plan_reorg(
    existing_for_chain: Sequence[LedgerTransaction],
    canonical: Sequence[LedgerTransaction],
    from_block: int,
) -> ReorgPlan:
    """Pure diff of the stored view against the canonical chain view.

    - stored txns with ``block_number is not None and >= from_block``
      whose id is absent from ``canonical`` -> ``remove_ids`` (orphaned
      or replaced-old)
    - canonical txns that are new (id not stored) OR that differ from
      the stored txn by payload (``payload_equal`` False) -> ``add``
    - identical survivors appear in neither bucket

    Pending stored txns (``block_number is None``) are never removed.
    Duplicate ids WITHIN either argument raise
    ``auradefi.errors.ValidationError`` before any bucketing, so a
    backend applying the plan never fails halfway. Neither argument is
    mutated.
    """
    _require_unique_ids(existing_for_chain, "existing_for_chain")
    _require_unique_ids(canonical, "canonical")

    canonical_ids = {txn.id for txn in canonical}
    remove_ids: list[str] = []
    for txn in existing_for_chain:
        if txn.block_number is None or txn.block_number < from_block:
            continue
        if txn.id in canonical_ids:
            continue
        remove_ids.append(txn.id)

    stored_by_id = {txn.id: txn for txn in existing_for_chain}
    add: list[LedgerTransaction] = []
    for txn in canonical:
        stored = stored_by_id.get(txn.id)
        if stored is None or not payload_equal(txn, stored):
            add.append(txn)
    return ReorgPlan(remove_ids=tuple(remove_ids), add=tuple(add))


def _require_unique_ids(
    txns: Sequence[LedgerTransaction], label: str
) -> None:
    """Raise ``ValidationError`` on a duplicate id within ``txns``."""
    seen: set[str] = set()
    for txn in txns:
        if txn.id in seen:
            raise ValidationError(
                f"duplicate transaction id within {label}: {txn.id!r}"
            )
        seen.add(txn.id)
