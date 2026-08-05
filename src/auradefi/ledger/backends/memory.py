"""In-memory ledger backend (SPEC §6.4; rules #6, #12; Phase 0).

``MemoryLedger`` implements ``auradefi.ledger.port.LedgerPort`` over
per-tenant isolated dict stores. It backs the test suite and is the
reference for backend semantics: per-tenant monotonic ``last_modified_seq``
starting at 1, idempotent upsert (payload-identical redelivery emits no
event and bumps no seq), removal as a first-class REMOVED event, and
state-based sync pages ordered by ascending last-modified seq.

Stdlib only, no ORM in Phase 0.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from auradefi.errors import (
    NotFoundError,
    TenantIsolationError,
    ValidationError,
)
from auradefi.ledger.cursors import decode_cursor, encode_cursor
from auradefi.ledger.models import (
    LedgerTransaction,
    SyncEvent,
    SyncEventKind,
    SyncPage,
)
from auradefi.ledger.reorg import ReorgPlan
from auradefi.ledger.upsert import classify


class MemoryLedger:
    """Dict-backed ``LedgerPort`` with hard per-tenant isolation.

    Constructed empty with no arguments: ``MemoryLedger()``, no tenants,
    every per-tenant seq counter starting from 0 (first write gets 1).

    Every method validates ``tenant_id`` first: anything that is not a
    non-empty, non-whitespace ``str`` raises
    ``auradefi.errors.TenantIsolationError``. One tenant's ids are
    indistinguishable from nonexistent ids for every other tenant.
    """

    def __init__(self) -> None:
        self._stores: dict[str, dict[str, LedgerTransaction]] = {}
        self._seqs: dict[str, int] = {}

    def upsert(
        self, tenant_id: str, txns: Sequence[LedgerTransaction]
    ) -> list[SyncEvent]:
        """Insert or update ``txns`` in one tenant's store.

        New or payload-changed transactions are stored with the tenant's
        next ``last_modified_seq`` (monotonic, starting at 1) and emit
        ADDED events ordered by ascending seq. Payload-identical incoming
        transactions emit NO event and bump NO seq (idempotence), unless
        the STORED row is removed, in which case the transaction is
        resurrected: stored with ``removed=False``, a bumped seq, and an
        ADDED event (SPEC §6.4: re-added is first-class). Incoming
        bookkeeping fields are never adopted. Duplicate ids within
        ``txns`` raise ``auradefi.errors.ValidationError`` before any
        write.
        """
        self._check_tenant(tenant_id)
        store = self._stores.setdefault(tenant_id, {})
        plan = classify(txns, store)
        write_ids = {t.id for t in plan.new} | {t.id for t in plan.changed}
        # Resurrection keys on the STORED removed flag (incoming
        # bookkeeping is never trusted): a payload-identical redelivery
        # of a removed row means it is canonical again.
        write_ids.update(
            txn_id for txn_id in plan.unchanged if store[txn_id].removed
        )
        events: list[SyncEvent] = []
        for txn in txns:
            if txn.id not in write_ids:
                continue
            row = replace(
                txn, removed=False, last_modified_seq=self._next_seq(tenant_id)
            )
            store[row.id] = row
            events.append(SyncEvent(kind=SyncEventKind.ADDED, transaction=row))
        return events

    def sync(
        self, tenant_id: str, cursor: str | None = None, limit: int = 100
    ) -> SyncPage:
        """Page of changes since ``cursor``, ascending last-modified seq.

        Emits one event per stored transaction with
        ``last_modified_seq > decode_cursor(cursor)``, REMOVED when the
        stored row is removed, else ADDED, up to ``limit``.
        ``next_cursor`` is ``encode_cursor`` of the last event's seq, or
        of the decoded input when the page is empty. ``has_more`` is True
        iff events beyond this page remain. A malformed cursor raises
        ``auradefi.errors.CursorError``; a ``limit`` below 1 raises
        ``auradefi.errors.ValidationError`` (a page that can hold nothing
        can never drain, so paging would loop forever). Clients page
        until ``has_more`` is False before persisting ``next_cursor``.
        """
        self._check_tenant(tenant_id)
        if limit < 1:
            raise ValidationError(f"limit must be >= 1, got {limit}")
        since = decode_cursor(cursor)
        rows = sorted(
            (
                row
                for row in self._stores.get(tenant_id, {}).values()
                if row.last_modified_seq > since
            ),
            key=lambda row: row.last_modified_seq,
        )
        page = rows[:limit]
        events = tuple(
            SyncEvent(
                kind=(
                    SyncEventKind.REMOVED if row.removed else SyncEventKind.ADDED
                ),
                transaction=row,
            )
            for row in page
        )
        last_seq = page[-1].last_modified_seq if page else since
        return SyncPage(
            events=events,
            next_cursor=encode_cursor(last_seq),
            has_more=len(rows) > limit,
        )

    def get(self, tenant_id: str, txn_id: str) -> LedgerTransaction:
        """Fetch one transaction from this tenant's store.

        Raises ``auradefi.errors.NotFoundError`` when the id does not
        exist in THIS tenant's store. Another tenant's transaction is
        indistinguishable from a missing one.
        """
        self._check_tenant(tenant_id)
        row = self._stores.get(tenant_id, {}).get(txn_id)
        if row is None:
            raise NotFoundError(f"transaction not found: {txn_id!r}")
        return row

    def mark_removed(
        self, tenant_id: str, txn_ids: Sequence[str]
    ) -> list[SyncEvent]:
        """Mark transactions removed (reorg semantics), one tenant.

        Sets ``removed=True`` with a bumped seq and emits REMOVED events
        ordered by ascending seq. An unknown id raises
        ``auradefi.errors.NotFoundError`` before any write; an
        already-removed id is a no-op emitting no event and bumping no
        seq.
        """
        self._check_tenant(tenant_id)
        store = self._stores.get(tenant_id, {})
        for txn_id in txn_ids:
            if txn_id not in store:
                raise NotFoundError(f"transaction not found: {txn_id!r}")
        events: list[SyncEvent] = []
        for txn_id in txn_ids:
            row = store[txn_id]
            if row.removed:
                continue
            row = replace(
                row, removed=True, last_modified_seq=self._next_seq(tenant_id)
            )
            store[txn_id] = row
            events.append(
                SyncEvent(kind=SyncEventKind.REMOVED, transaction=row)
            )
        return events

    def apply_reorg(self, tenant_id: str, plan: ReorgPlan) -> list[SyncEvent]:
        """Apply a :class:`ReorgPlan`: ``mark_removed`` then ``upsert``.

        Returns the composed events (REMOVED first, then ADDED) ordered
        by ascending seq. SPEC §6.4: a chain reorg is removed +
        re-added, first-class. Duplicate ids within ``plan.add`` raise
        ``auradefi.errors.ValidationError`` BEFORE any write, so a bad
        plan never leaves the tenant half-reorged.
        """
        self._check_tenant(tenant_id)
        seen: set[str] = set()
        for txn in plan.add:
            if txn.id in seen:
                raise ValidationError(
                    f"duplicate transaction id within plan.add: {txn.id!r}"
                )
            seen.add(txn.id)
        events = self.mark_removed(tenant_id, plan.remove_ids)
        events.extend(self.upsert(tenant_id, plan.add))
        return events

    def _check_tenant(self, tenant_id: str) -> None:
        """Reject anything but a non-empty, non-whitespace str tenant id."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise TenantIsolationError(
                "tenant_id must be a non-empty, non-whitespace string"
            )

    def _next_seq(self, tenant_id: str) -> int:
        """Bump and return one tenant's monotonic seq (first value 1)."""
        self._seqs[tenant_id] = self._seqs.get(tenant_id, 0) + 1
        return self._seqs[tenant_id]
