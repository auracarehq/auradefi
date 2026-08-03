"""Projection of rich decoded transactions into the Phase-0 ledger (SPEC §6.4).

``ledger`` MAY import ``decode`` (tests/style ALLOWED_IMPORTS); ``decode``
may never import ``ledger``. This is the single place where the deliberate
duplicates (``decode.models.Direction`` / ``decode.models.transaction_id``,
DECISIONS.md "Duplication waiver") meet their ledger originals — the bridge
maps enum members by value, and golden vectors in
``tests/ledger/test_bridge.py`` pin both sides to the same bytes.

Pure function, no I/O, input never mutated.
"""

from __future__ import annotations

from auradefi.decode.models import Transaction
from auradefi.ledger.models import Direction, Entry, LedgerTransaction


def to_ledger_transaction(rich: Transaction) -> LedgerTransaction:
    """Project a rich decoded ``Transaction`` into a ``LedgerTransaction``.

    ``id`` / ``chain_id`` / ``tx_hash`` / ``account_id`` / ``block_number``
    / ``initiated_at`` / ``confirmed_at`` are carried verbatim. ``entries``
    is one ``Entry(asset_id=part.asset_id, quantity=part.quantity,
    direction=ledger.models.Direction(part.direction.value))`` per part, in
    ``parts`` order. Fees NEVER become entries (SPEC §4.4 — fees are
    siblings, never movements): a failed transaction with zero parts and a
    fee bridges to ``entries == ()``. Bookkeeping defaults apply
    (``removed=False``, ``last_modified_seq=0``). The input is never
    mutated; equal inputs produce equal outputs.
    """
    return LedgerTransaction(
        id=rich.id,
        chain_id=rich.chain_id,
        tx_hash=rich.tx_hash,
        account_id=rich.account_id,
        block_number=rich.block_number,
        initiated_at=rich.initiated_at,
        confirmed_at=rich.confirmed_at,
        entries=tuple(
            Entry(
                asset_id=part.asset_id,
                quantity=part.quantity,
                direction=Direction(part.direction.value),
            )
            for part in rich.parts
        ),
    )
