"""SyncStatePort protocol + MemorySyncState semantics (SPEC §8; rules #6, #12).

MemoryLedger's tenant hygiene, copied: every method validates
``tenant_id`` first (non-str / empty / whitespace →
``TenantIsolationError``), and one tenant's records are invisible to
every other tenant — with a test that tries.

Tenant-id literals reuse the goldens pinned in tests/embed/test_models.py
(derived independently via ``python3 -c`` from the DECISIONS formulas).
"""

from __future__ import annotations

import pytest

from auradefi.embed.models import ConnectionRecord, SyncState
from auradefi.embed.state import MemorySyncState, SyncStatePort
from auradefi.errors import ConflictError, TenantIsolationError

TENANT_A = "usr_1e63721d071ea2d9"  # embed | host-user-1
TENANT_B = "usr_d6ace495d5f89481"  # embed | host-user-2

CONN_1 = "conn_b116094c537a85e6"
CONN_2 = "conn_3a8b8993bc6953a9"

MS = 1_754_000_000_000  # ms-epoch, matches the repo's frozen clock era

BAD_TENANTS = ["", "   ", "\t\n", 42, None, b"usr_1e63721d071ea2d9"]


def make_record(**overrides) -> ConnectionRecord:
    fields = {
        "id": CONN_1,
        "chain_id": "eip155:1",
        "address": "0x" + "1" * 40,
        "created_at_ms": MS,
    }
    fields.update(overrides)
    return ConnectionRecord(**fields)


class HostSyncState:
    """A host's own duck-typed implementation — matching shape only."""

    def get_state(self, tenant_id: str, connection_id: str) -> SyncState:
        return SyncState()

    def put_state(self, tenant_id: str, connection_id: str, state: SyncState) -> None:
        return None

    def connections(self, tenant_id: str) -> tuple[ConnectionRecord, ...]:
        return ()

    def add_connection(self, tenant_id: str, record: ConnectionRecord) -> None:
        return None


class NotAPort:
    def get_state(self, tenant_id: str, connection_id: str) -> SyncState:
        return SyncState()


class TestProtocol:
    def test_memory_sync_state_satisfies_the_port(self):
        assert isinstance(MemorySyncState(), SyncStatePort)

    def test_a_host_class_satisfies_the_port_structurally(self):
        # Rule #12: no base class to import, no registration.
        assert isinstance(HostSyncState(), SyncStatePort)

    def test_a_class_missing_methods_does_not_satisfy_the_port(self):
        assert not isinstance(NotAPort(), SyncStatePort)


class TestGetState:
    def test_empty_store_returns_the_fresh_default(self):
        store = MemorySyncState()
        assert store.get_state(TENANT_A, CONN_1) == SyncState()

    def test_fresh_default_is_the_explicit_zero_state(self):
        store = MemorySyncState()
        assert store.get_state(TENANT_A, CONN_1) == SyncState(0, None, False, 0)

    def test_absent_connection_under_a_populated_tenant_is_still_default(self):
        store = MemorySyncState()
        store.put_state(TENANT_A, CONN_1, SyncState(live_cursor=42))
        assert store.get_state(TENANT_A, CONN_2) == SyncState()


class TestPutState:
    def test_put_then_get_round_trips(self):
        store = MemorySyncState()
        state = SyncState(
            live_cursor=42,
            backfill_cursor=7,
            backfill_complete=True,
            last_sync_at_ms=MS,
        )
        store.put_state(TENANT_A, CONN_1, state)
        assert store.get_state(TENANT_A, CONN_1) == state

    def test_last_write_wins(self):
        store = MemorySyncState()
        store.put_state(TENANT_A, CONN_1, SyncState(live_cursor=1))
        store.put_state(TENANT_A, CONN_1, SyncState(live_cursor=2))
        assert store.get_state(TENANT_A, CONN_1) == SyncState(live_cursor=2)

    def test_states_are_keyed_per_connection(self):
        store = MemorySyncState()
        store.put_state(TENANT_A, CONN_1, SyncState(live_cursor=1))
        store.put_state(TENANT_A, CONN_2, SyncState(live_cursor=2))
        assert store.get_state(TENANT_A, CONN_1).live_cursor == 1
        assert store.get_state(TENANT_A, CONN_2).live_cursor == 2

    def test_huge_cursor_round_trips_exactly(self):
        # Cursors are ints; 10^77-scale values must survive untouched.
        big = 10**77 + 3
        store = MemorySyncState()
        store.put_state(TENANT_A, CONN_1, SyncState(live_cursor=big))
        assert store.get_state(TENANT_A, CONN_1).live_cursor == big


class TestConnections:
    def test_empty_tenant_yields_an_empty_tuple(self):
        store = MemorySyncState()
        got = store.connections(TENANT_A)
        assert got == ()
        assert isinstance(got, tuple)

    def test_add_then_list_round_trips(self):
        store = MemorySyncState()
        record = make_record()
        store.add_connection(TENANT_A, record)
        assert store.connections(TENANT_A) == (record,)

    def test_creation_order_is_preserved_not_sorted(self):
        store = MemorySyncState()
        first = make_record(id="conn_cccccccccccccccc")
        second = make_record(id="conn_aaaaaaaaaaaaaaaa")
        third = make_record(id="conn_bbbbbbbbbbbbbbbb")
        store.add_connection(TENANT_A, first)
        store.add_connection(TENANT_A, second)
        store.add_connection(TENANT_A, third)
        assert store.connections(TENANT_A) == (first, second, third)

    def test_duplicate_add_raises_conflict_carrying_the_existing_id(self):
        store = MemorySyncState()
        record = make_record()
        store.add_connection(TENANT_A, record)
        with pytest.raises(ConflictError) as excinfo:
            store.add_connection(TENANT_A, record)
        assert excinfo.value.existing_id == record.id

    def test_conflict_keys_on_id_and_keeps_the_original_record(self):
        store = MemorySyncState()
        original = make_record()
        store.add_connection(TENANT_A, original)
        later = make_record(created_at_ms=MS + 60_000)
        with pytest.raises(ConflictError) as excinfo:
            store.add_connection(TENANT_A, later)
        assert excinfo.value.existing_id == original.id
        assert store.connections(TENANT_A) == (original,)


class TestTenantIsolation:
    def test_tenant_b_sees_none_of_tenant_a_records(self):
        store = MemorySyncState()
        store.add_connection(TENANT_A, make_record())
        store.put_state(TENANT_A, CONN_1, SyncState(live_cursor=42))
        assert store.connections(TENANT_B) == ()
        assert store.get_state(TENANT_B, CONN_1) == SyncState()

    def test_same_connection_id_is_addable_under_both_tenants(self):
        store = MemorySyncState()
        record = make_record()
        store.add_connection(TENANT_A, record)
        store.add_connection(TENANT_B, record)  # no ConflictError across tenants
        assert store.connections(TENANT_A) == (record,)
        assert store.connections(TENANT_B) == (record,)

    def test_put_state_under_one_tenant_never_leaks_to_another(self):
        store = MemorySyncState()
        store.put_state(TENANT_A, CONN_1, SyncState(live_cursor=99))
        store.put_state(TENANT_B, CONN_1, SyncState(live_cursor=1))
        assert store.get_state(TENANT_A, CONN_1).live_cursor == 99
        assert store.get_state(TENANT_B, CONN_1).live_cursor == 1


@pytest.mark.parametrize("bad", BAD_TENANTS)
class TestTenantHygiene:
    """Non-str / empty / whitespace tenant_id → TenantIsolationError,
    on EVERY method, before any work."""

    def test_get_state_rejects_bad_tenant(self, bad):
        with pytest.raises(TenantIsolationError):
            MemorySyncState().get_state(bad, CONN_1)

    def test_put_state_rejects_bad_tenant(self, bad):
        with pytest.raises(TenantIsolationError):
            MemorySyncState().put_state(bad, CONN_1, SyncState())

    def test_connections_rejects_bad_tenant(self, bad):
        with pytest.raises(TenantIsolationError):
            MemorySyncState().connections(bad)

    def test_add_connection_rejects_bad_tenant(self, bad):
        with pytest.raises(TenantIsolationError):
            MemorySyncState().add_connection(bad, make_record())
