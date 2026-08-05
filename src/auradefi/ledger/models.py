"""Ledger models and deterministic transaction identity (SPEC §4.4, §6.4).

Value objects for the persistence layer: the immutable ``LedgerTransaction``
with its ``Entry`` movements, the deterministic ``transaction_id`` pinned in
docs/internal/DECISIONS.md, payload equality that ignores backend bookkeeping fields,
and the sync-event envelope (``SyncEvent``/``SyncPage``) that
``auradefi.ledger.port.LedgerPort`` speaks.

All timestamps are ms-epoch ints; amounts are exact ``Quantity`` values,
never floats.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields
from enum import StrEnum

from auradefi.money.quantity import Quantity

_BOOKKEEPING_FIELDS = frozenset({"last_modified_seq", "removed"})


class Direction(StrEnum):
    """Which way a movement goes relative to the owning account."""

    IN = "in"
    OUT = "out"
    SELF = "self"


class SyncEventKind(StrEnum):
    """What happened to a transaction since the caller's cursor (SPEC §6.4).

    A chain reorg is ``REMOVED`` followed by a fresh ``ADDED``: a
    first-class event pair, never a magic boolean.
    """

    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class Entry:
    """One movement of a single asset inside a transaction."""

    asset_id: str
    quantity: Quantity
    direction: Direction


@dataclass(frozen=True, slots=True)
class LedgerTransaction:
    """A persisted transaction: identity, timing, movements, bookkeeping.

    ``id`` is deterministic. See :func:`transaction_id`. ``initiated_at``
    is a ms-epoch int; ``confirmed_at`` is ``None`` until confirmation.
    ``removed`` and ``last_modified_seq`` are backend bookkeeping and are
    excluded from :func:`payload_equal`.
    """

    id: str
    chain_id: str
    tx_hash: str
    account_id: str
    block_number: int | None
    initiated_at: int
    confirmed_at: int | None
    entries: tuple[Entry, ...]
    removed: bool = False
    last_modified_seq: int = 0


@dataclass(frozen=True, slots=True)
class SyncEvent:
    """One ledger change: a transaction was added or removed."""

    kind: SyncEventKind
    transaction: LedgerTransaction


@dataclass(frozen=True, slots=True)
class SyncPage:
    """One page of sync events, ordered by ascending last-modified seq.

    Clients page until ``has_more`` is ``False`` before persisting
    ``next_cursor`` (SPEC §6.4).
    """

    events: tuple[SyncEvent, ...]
    next_cursor: str
    has_more: bool


def transaction_id(chain_id: str, tx_hash: str, account_id: str) -> str:
    """Deterministic transaction id (DECISIONS pinned; SPEC §4.4).

    ``"txn_" + sha256(f"{chain_id}|{tx_hash}|{account_id}".encode())
    .hexdigest()[:16]``. The same (chain, hash, account) triple always
    yields the same id; changing any component yields a different id.
    """
    digest = hashlib.sha256(
        f"{chain_id}|{tx_hash}|{account_id}".encode()
    ).hexdigest()
    return f"txn_{digest[:16]}"


def payload_equal(a: LedgerTransaction, b: LedgerTransaction) -> bool:
    """True when every field matches EXCEPT ``last_modified_seq``/``removed``.

    Backend bookkeeping never makes two payloads different: upsert uses
    this to decide whether a re-delivered transaction actually changed.
    """
    return all(
        getattr(a, field.name) == getattr(b, field.name)
        for field in fields(LedgerTransaction)
        if field.name not in _BOOKKEEPING_FIELDS
    )
