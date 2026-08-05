"""SQL ledger backend over a HOST-OWNED session factory (SPEC §8).

``SqlModelLedger`` implements ``auradefi.ledger.port.LedgerPort`` with
semantics IDENTICAL to the pinned reference backend
(``auradefi.ledger.backends.memory.MemoryLedger``). Storage is a port
(rules #6/#12): the host binds its own session factory and keeps its own
migration story. We never open a connection the host didn't hand us:
this module builds no engine and emits no DDL; schema setup is the
host's job, against ``auradefi.ledger.backends.models.metadata``.

Seq allocation reads/bumps ``TenantSeqRow`` and is documented
single-writer; Postgres hardening is Phase 8.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace

from sqlmodel import Session, select

from auradefi.errors import (
    NotFoundError,
    TenantIsolationError,
    ValidationError,
)
from auradefi.ledger.backends.models import (
    LedgerTransactionRow,
    TenantSeqRow,
    row_to_transaction,
    transaction_to_row,
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


class SqlModelLedger:
    """``LedgerPort`` over host-owned SQLModel sessions (SPEC §8).

    Every public method validates ``tenant_id`` FIRST (non-empty,
    non-whitespace ``str``, else ``auradefi.errors.TenantIsolationError``)
    before touching any session, then runs one session/commit per call.
    Per-tenant monotonic seqs come from ``TenantSeqRow`` (first value 1).
    The counter lives in the DB, so a second binding over the same
    engine continues the sequence.
    """

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        """Bind the HOST's session factory. ZERO I/O happens here.

        The constructor stores the factory and nothing else: no engine
        is built, no connection opened, no DDL emitted. An empty
        database stays empty until the HOST creates the schema itself.
        """
        self._session_factory = session_factory

    def upsert(
        self, tenant_id: str, txns: Sequence[LedgerTransaction]
    ) -> list[SyncEvent]:
        """Insert or update ``txns`` in one tenant's store (SPEC §6.4).

        Diffs via ``auradefi.ledger.upsert.classify``; new/changed rows
        are written with the tenant's next monotonic seq from
        ``TenantSeqRow`` (first 1) and emit ADDED events ascending by
        seq. Payload-identical redelivery emits no event and bumps no
        seq, UNLESS the STORED row is removed, in which case the txn
        is resurrected (``removed=False``, bumped seq, ADDED).
        Resurrection keys on the STORED removed flag; incoming
        bookkeeping is never adopted. Duplicate ids within ``txns``
        raise ``auradefi.errors.ValidationError`` before any write.
        """
        self._check_tenant(tenant_id)
        with self._session_factory() as session:
            events = self._upsert(session, tenant_id, txns)
            session.commit()
        return events

    def sync(
        self, tenant_id: str, cursor: str | None = None, limit: int = 100
    ) -> SyncPage:
        """Page of changes since ``cursor``, ascending seq (SPEC §6.4).

        Pages rows with ``last_modified_seq > decode_cursor(cursor)``
        ascending: REMOVED iff the stored row is removed, else ADDED.
        ``next_cursor`` encodes the last event's seq, or the decoded
        input when the page is empty; ``has_more`` is accurate. A
        malformed cursor raises ``auradefi.errors.CursorError``; a
        ``limit`` below 1 raises ``auradefi.errors.ValidationError``.
        """
        self._check_tenant(tenant_id)
        if limit < 1:
            raise ValidationError(f"limit must be >= 1, got {limit}")
        since = decode_cursor(cursor)
        # One row beyond the page answers has_more without a count.
        statement = (
            select(LedgerTransactionRow)
            .where(LedgerTransactionRow.tenant_id == tenant_id)
            .where(LedgerTransactionRow.last_modified_seq > since)
            .order_by(LedgerTransactionRow.last_modified_seq)
            .limit(limit + 1)
        )
        with self._session_factory() as session:
            rows = list(session.exec(statement))
            page = [row_to_transaction(row) for row in rows[:limit]]
        events = tuple(
            SyncEvent(
                kind=(
                    SyncEventKind.REMOVED
                    if txn.removed
                    else SyncEventKind.ADDED
                ),
                transaction=txn,
            )
            for txn in page
        )
        last_seq = page[-1].last_modified_seq if page else since
        return SyncPage(
            events=events,
            next_cursor=encode_cursor(last_seq),
            has_more=len(rows) > limit,
        )

    def get(self, tenant_id: str, txn_id: str) -> LedgerTransaction:
        """Fetch one transaction within THIS tenant (rule #6).

        Raises ``auradefi.errors.NotFoundError`` when the id does not
        exist in this tenant. Another tenant's transaction is
        indistinguishable from a missing one.
        """
        self._check_tenant(tenant_id)
        with self._session_factory() as session:
            row = session.get(LedgerTransactionRow, (tenant_id, txn_id))
            if row is None:
                raise NotFoundError(f"transaction not found: {txn_id!r}")
            return row_to_transaction(row)

    def mark_removed(
        self, tenant_id: str, txn_ids: Sequence[str]
    ) -> list[SyncEvent]:
        """Mark transactions removed (reorg semantics), one tenant.

        Any unknown id raises ``auradefi.errors.NotFoundError`` BEFORE
        any write. Live rows get ``removed=True`` with a bumped seq and
        emit REMOVED events ascending by seq; an already-removed id is
        a silent no-op (no event, no seq bump).
        """
        self._check_tenant(tenant_id)
        with self._session_factory() as session:
            events = self._mark_removed(session, tenant_id, txn_ids)
            session.commit()
        return events

    def apply_reorg(self, tenant_id: str, plan: ReorgPlan) -> list[SyncEvent]:
        """Apply a ``ReorgPlan``: mark_removed then upsert, atomically.

        Duplicate ids within ``plan.add`` raise
        ``auradefi.errors.ValidationError`` BEFORE any write. Both
        halves run INSIDE ONE session/commit: a failure anywhere rolls
        the whole plan back. The tenant is never left half-reorged.
        Returns REMOVED then ADDED events ascending by seq.
        """
        self._check_tenant(tenant_id)
        seen: set[str] = set()
        for txn in plan.add:
            if txn.id in seen:
                raise ValidationError(
                    f"duplicate transaction id within plan.add: {txn.id!r}"
                )
            seen.add(txn.id)
        with self._session_factory() as session:
            events = self._mark_removed(session, tenant_id, plan.remove_ids)
            events.extend(self._upsert(session, tenant_id, plan.add))
            session.commit()
        return events

    def _upsert(
        self,
        session: Session,
        tenant_id: str,
        txns: Sequence[LedgerTransaction],
    ) -> list[SyncEvent]:
        """``upsert`` inside a caller-owned session; never commits.

        Composed by :meth:`apply_reorg` so a whole plan shares one
        transaction.
        """
        stored = self._load(session, tenant_id, (txn.id for txn in txns))
        plan = classify(
            txns,
            {i: row_to_transaction(row) for i, row in stored.items()},
        )
        write_ids = {t.id for t in plan.new} | {t.id for t in plan.changed}
        # Resurrection keys on the STORED removed flag (incoming
        # bookkeeping is never trusted): a payload-identical redelivery
        # of a removed row means it is canonical again.
        write_ids.update(
            txn_id for txn_id in plan.unchanged if stored[txn_id].removed
        )
        events: list[SyncEvent] = []
        for txn in txns:
            if txn.id not in write_ids:
                continue
            fresh = replace(
                txn,
                removed=False,
                last_modified_seq=self._next_seq(session, tenant_id),
            )
            row = session.merge(transaction_to_row(tenant_id, fresh))
            events.append(
                SyncEvent(
                    kind=SyncEventKind.ADDED,
                    transaction=row_to_transaction(row),
                )
            )
        return events

    def _mark_removed(
        self, session: Session, tenant_id: str, txn_ids: Sequence[str]
    ) -> list[SyncEvent]:
        """``mark_removed`` inside a caller-owned session; never commits."""
        rows = self._load(session, tenant_id, txn_ids)
        for txn_id in txn_ids:
            if txn_id not in rows:
                raise NotFoundError(f"transaction not found: {txn_id!r}")
        events: list[SyncEvent] = []
        for txn_id in txn_ids:
            row = rows[txn_id]
            if row.removed:
                continue
            row.removed = True
            row.last_modified_seq = self._next_seq(session, tenant_id)
            events.append(
                SyncEvent(
                    kind=SyncEventKind.REMOVED,
                    transaction=row_to_transaction(row),
                )
            )
        return events

    def _load(
        self, session: Session, tenant_id: str, txn_ids: Iterable[str]
    ) -> dict[str, LedgerTransactionRow]:
        """The tenant's rows for ``txn_ids``; missing ids are absent."""
        rows: dict[str, LedgerTransactionRow] = {}
        for txn_id in txn_ids:
            if txn_id in rows:
                continue
            row = session.get(LedgerTransactionRow, (tenant_id, txn_id))
            if row is not None:
                rows[txn_id] = row
        return rows

    def _check_tenant(self, tenant_id: str) -> None:
        """Reject anything but a non-empty, non-whitespace str tenant id."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise TenantIsolationError(
                "tenant_id must be a non-empty, non-whitespace string"
            )

    def _next_seq(self, session: Session, tenant_id: str) -> int:
        """Bump and return one tenant's DB-resident seq (first value 1).

        Single-writer by documentation: the read-modify-write is
        serialised by the caller's transaction, and Postgres hardening
        (``SELECT ... FOR UPDATE``) lands in Phase 8.
        """
        counter = session.get(TenantSeqRow, tenant_id)
        if counter is None:
            counter = TenantSeqRow(tenant_id=tenant_id, seq=0)
            session.add(counter)
        counter.seq += 1
        return counter.seq
