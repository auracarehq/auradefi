"""Connection and sync-state persistence port for embedding (SPEC §8).

``SyncStatePort`` is the structural interface a host satisfies to own
the storage of per-connection sync cursors (rule #12): a
``runtime_checkable`` ``Protocol`` — no base class to import, no
registration. ``MemorySyncState`` backs the test suite with
``MemoryLedger``'s tenant hygiene: every method validates ``tenant_id``
first and one tenant's records are invisible to every other tenant.

A SQL-backed implementation is deliberately deferred.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from auradefi.embed.models import ConnectionRecord, SyncState
from auradefi.errors import ConflictError, TenantIsolationError


@runtime_checkable
class SyncStatePort(Protocol):
    """Structural contract for embed sync-state persistence.

    Tenant-scoped throughout (rule #6): ``tenant_id`` is the first
    argument everywhere, and no call may read or write across tenants.
    """

    def get_state(self, tenant_id: str, connection_id: str) -> SyncState:
        """Return the stored :class:`SyncState` for one connection.

        A fresh default ``SyncState()`` when nothing was stored — a
        never-synced connection and an absent one are indistinguishable.
        """
        raise NotImplementedError

    def put_state(
        self, tenant_id: str, connection_id: str, state: SyncState
    ) -> None:
        """Store ``state`` for one connection; last write wins."""
        raise NotImplementedError

    def connections(self, tenant_id: str) -> tuple[ConnectionRecord, ...]:
        """All of one tenant's connection records, in creation order."""
        raise NotImplementedError

    def add_connection(self, tenant_id: str, record: ConnectionRecord) -> None:
        """Register a connection record under one tenant.

        A duplicate ``record.id`` within the tenant raises
        ``auradefi.errors.ConflictError`` carrying
        ``existing_id=record.id`` (SPEC §7.1 — Vezgo's 409 with
        ``existing_connection_id``, kept); the stored record is never
        overwritten.
        """
        raise NotImplementedError


class MemorySyncState:
    """Dict-backed :class:`SyncStatePort` with hard per-tenant isolation.

    Constructed empty with no arguments: ``MemorySyncState()``. Every
    method validates ``tenant_id`` first: anything that is not a
    non-empty, non-whitespace ``str`` raises
    ``auradefi.errors.TenantIsolationError`` (MemoryLedger's tenant
    hygiene, copied). One tenant's connection ids are indistinguishable
    from nonexistent ids for every other tenant.
    """

    def __init__(self) -> None:
        self._states: dict[str, dict[str, SyncState]] = {}
        self._records: dict[str, dict[str, ConnectionRecord]] = {}

    def get_state(self, tenant_id: str, connection_id: str) -> SyncState:
        """Stored state for one connection; ``SyncState()`` when absent."""
        self._check_tenant(tenant_id)
        return self._states.get(tenant_id, {}).get(connection_id, SyncState())

    def put_state(
        self, tenant_id: str, connection_id: str, state: SyncState
    ) -> None:
        """Store ``state`` under (tenant, connection); last write wins."""
        self._check_tenant(tenant_id)
        self._states.setdefault(tenant_id, {})[connection_id] = state

    def connections(self, tenant_id: str) -> tuple[ConnectionRecord, ...]:
        """This tenant's connection records, in creation order."""
        self._check_tenant(tenant_id)
        return tuple(self._records.get(tenant_id, {}).values())

    def add_connection(self, tenant_id: str, record: ConnectionRecord) -> None:
        """Register ``record``; duplicate id → ``ConflictError`` with
        ``existing_id=record.id``, original record retained."""
        self._check_tenant(tenant_id)
        records = self._records.setdefault(tenant_id, {})
        if record.id in records:
            raise ConflictError(
                f"connection already exists: {record.id!r}",
                existing_id=record.id,
            )
        records[record.id] = record

    def _check_tenant(self, tenant_id: str) -> None:
        """Reject anything but a non-empty, non-whitespace str tenant id."""
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise TenantIsolationError(
                "tenant_id must be a non-empty, non-whitespace string"
            )
