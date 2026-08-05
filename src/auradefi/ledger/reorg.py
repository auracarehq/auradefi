"""Reorg planning (SPEC §6.4: a chain reorg is removed + re-added).

``plan_reorg`` is a pure function over the stored view of one chain and
the canonical view from a fork point. It emits a :class:`ReorgPlan` that
a backend applies as ``mark_removed`` + ``upsert``: a first-class
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
    - canonical txns that are new (id not stored), that differ from the
      stored txn by payload (``payload_equal`` False), or whose stored
      txn is ``removed`` -> ``add``
    - identical LIVE survivors appear in neither bucket

    The ``removed`` flag is part of the re-add decision, not a detail
    ``payload_equal`` may ignore: a row orphaned by an earlier reorg
    that is back in the canonical view is re-added even when its payload
    is byte-identical, otherwise it would stay removed forever.

    Pending stored txns (``block_number is None``) are never removed.
    Duplicate ids WITHIN either argument raise
    ``auradefi.errors.ValidationError`` before any bucketing, so a
    backend applying the plan never fails halfway. Neither argument is
    mutated.

    ONE CHAIN, enforced. The parameter is named ``existing_for_chain``
    and the whole diff assumes it: an id absent from ``canonical`` is
    read as orphaned. Feed it rows from two chains and every row of the
    OTHER chain is absent from this chain's canonical view, so a mainnet
    reorg plans the removal of live Polygon transactions: silent
    cross-chain data loss, and block numbers from different chains are
    not even comparable against ``from_block``. Nothing enforced this
    before 0.1.1, and #26 made it reachable by letting one address be
    connected on several chains at once. A mixed view is a caller error,
    so it raises rather than being quietly filtered: filtering would let
    a caller keep passing the wrong view and silently act on half of it.
    """
    _require_unique_ids(existing_for_chain, "existing_for_chain")
    _require_unique_ids(canonical, "canonical")
    _require_one_chain(existing_for_chain, canonical)

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
        if stored is None or stored.removed or not payload_equal(txn, stored):
            add.append(txn)
    return ReorgPlan(remove_ids=tuple(remove_ids), add=tuple(add))


def _require_one_chain(
    existing_for_chain: Sequence[LedgerTransaction],
    canonical: Sequence[LedgerTransaction],
) -> None:
    """Raise ``ValidationError`` unless every row names ONE chain."""
    chains = {txn.chain_id for txn in existing_for_chain} | {
        txn.chain_id for txn in canonical
    }
    if len(chains) > 1:
        raise ValidationError(
            "plan_reorg diffs ONE chain: got "
            f"{sorted(chains)}: pass this chain's rows only, or a reorg on "
            "one chain will plan the removal of another chain's live "
            "transactions"
        )


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
