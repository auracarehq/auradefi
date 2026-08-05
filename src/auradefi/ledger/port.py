"""Persistence port for the transaction ledger (SPEC §6.4; rules #6, #12).

``LedgerPort`` is the structural interface every ledger backend implements
(in-memory, SQL, a host's own store). It is a ``runtime_checkable``
``Protocol``: an embedding host satisfies it by matching the shape
(rule #12), no base class to import, no registration.

Every method is tenant-scoped (rule #6): ``tenant_id`` is the first
argument everywhere, and no call may read or write across tenants.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from auradefi.ledger.models import LedgerTransaction, SyncEvent, SyncPage


@runtime_checkable
class LedgerPort(Protocol):
    """Structural contract for ledger persistence backends.

    Tenant-scoped throughout (rule #6). Sync events are ordered by
    ascending last-modified sequence, SPEC §6.4: last-modified order,
    NOT transaction date, and clients page until ``has_more`` is
    ``False`` before persisting the cursor.
    """

    def upsert(
        self, tenant_id: str, txns: Sequence[LedgerTransaction]
    ) -> list[SyncEvent]:
        """Insert or update transactions inside one tenant's ledger.

        Tenant-scoped (rule #6). Returns the resulting events ordered by
        ascending last-modified seq (SPEC §6.4). A re-delivered
        transaction whose payload is unchanged (``payload_equal``) emits
        no event, UNLESS the stored row is removed, in which case the
        transaction is re-added: stored with ``removed=False``, a bumped
        seq, and an ADDED event (SPEC §6.4 re-add semantics; alternative
        backends must copy this).
        """
        raise NotImplementedError

    def sync(
        self, tenant_id: str, cursor: str | None = None, limit: int = 100
    ) -> SyncPage:
        """Page of changes for one tenant since ``cursor`` (SPEC §6.4).

        Tenant-scoped (rule #6). Events are ordered by ascending
        last-modified seq: last-modified order, NOT transaction date, so
        an old row that changes reappears. ``cursor=None`` starts from the
        beginning; a malformed cursor raises ``auradefi.errors.CursorError``.
        Clients page until ``has_more`` is ``False`` before persisting
        ``next_cursor``.
        """
        raise NotImplementedError

    def get(self, tenant_id: str, txn_id: str) -> LedgerTransaction:
        """Fetch one transaction within the caller's tenant scope.

        Tenant-scoped (rule #6). Raises ``auradefi.errors.NotFoundError``
        when the id does not exist inside this tenant. Another tenant's
        transaction is indistinguishable from a missing one.
        """
        raise NotImplementedError

    def mark_removed(
        self, tenant_id: str, txn_ids: Sequence[str]
    ) -> list[SyncEvent]:
        """Mark transactions removed (reorg semantics) inside one tenant.

        Tenant-scoped (rule #6). Returns ``REMOVED`` events ordered by
        ascending last-modified seq (SPEC §6.4); a reorg is ``removed``
        plus a later re-``added``, never an in-place mutation.
        """
        raise NotImplementedError
