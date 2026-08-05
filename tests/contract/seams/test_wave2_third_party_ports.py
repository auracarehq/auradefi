"""SEAM AUDIT: wave 0.1.1-wave2: the ports as a THIRD PARTY sees them.

Every in-repo test drives ``MemorySyncState`` and ``MemoryLedger``, which
work partly by accident of behaviour their ``Protocol`` never promised.
This file binds implementations written from the DECLARED docstrings
alone, no method the interface does not state, no return shape it does
not state, and wraps each one in :class:`DeclaredOnly`, a proxy that
raises ``AttributeError`` the moment a consumer reaches for something the
``Protocol`` never declared. A host that satisfies the published contract
and nothing more must still work; if it does not, the seam is a lie.

Two seams are under audit:

* ``SyncStatePort`` (``src/auradefi/embed/state.py``). Order
  ``embed-ids-loop`` #21 makes ``sync()`` enumerate connections from this
  port instead of an in-process list, which means the host's store
  becomes the ONLY durable record. ``Auradefi.__init__`` isinstance-checks
  ``source`` against both of its Protocols ("the failure belongs at bind
  time, not at the first background tick") but does NOT check
  ``sync_state``, so a store that is a valid 0.1.0 port silently
  contributes nothing.
* ``LedgerPort`` (``src/auradefi/ledger/port.py``): order
  ``ledger-reorg``'s declared seam says ``plan_reorg``'s output is
  consumed by a write path it does not own. The only surface a host is
  promised is ``upsert`` + ``mark_removed``; ``apply_reorg`` exists on
  both in-repo backends and is declared by NEITHER the port nor the
  reorg module, so it cannot be part of the contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from auradefi.clock import FrozenClock
from auradefi.embed.models import ConnectionRecord, SyncState
from auradefi.embed.state import SyncStatePort
from auradefi.embed.sync import SyncEngine
from auradefi.errors import (
    ConflictError,
    CursorError,
    NotFoundError,
    ValidationError,
)
from auradefi.ledger.models import (
    Direction,
    Entry,
    LedgerTransaction,
    SyncEvent,
    SyncEventKind,
    SyncPage,
    payload_equal,
)
from auradefi.ledger.port import LedgerPort
from auradefi.ledger.reorg import plan_reorg
from auradefi.money.quantity import Quantity

CHAIN = "eip155:1"
ADDRESS = "0x2222222222222222222222222222222222222222"
TENANT = "usr_seam_thirdparty"
NATIVE = "eip155:1/slip44:60"

ROWS = (
    {"blockNumber": "300", "hash": "0xf1", "timeStamp": "1700000010"},
    {"blockNumber": "299", "hash": "0xf2", "timeStamp": "1700000009"},
)


def declared_surface(protocol: type) -> frozenset[str]:
    """Every public name the ``Protocol`` itself declares.

    This is the whole contract a host is entitled to read. Anything a
    consumer calls that is not in here is a promise nobody made.
    """
    return frozenset(name for name in dir(protocol) if not name.startswith("_"))


class DeclaredOnly:
    """Expose ONLY the names ``protocol`` declares; anything else raises.

    The point of the proxy is that the failure is loud and names the
    method, instead of quietly succeeding because the in-repo class
    happens to have it.
    """

    def __init__(self, target: Any, protocol: type) -> None:
        """Wrap ``target``, gated by ``protocol``'s declared surface."""
        self._target = target
        self._allowed = declared_surface(protocol)
        self._protocol = protocol.__name__
        self.reached: list[str] = []

    def __getattr__(self, name: str) -> Any:
        """Delegate a DECLARED name; raise for anything else."""
        allowed = object.__getattribute__(self, "_allowed")
        if name not in allowed:
            raise AttributeError(
                f"a consumer reached for {name!r}, which "
                f"{object.__getattribute__(self, '_protocol')} does not "
                f"declare: declared surface is {sorted(allowed)}"
            )
        object.__getattribute__(self, "reached").append(name)
        return getattr(object.__getattribute__(self, "_target"), name)


class HostSyncState:
    """A ``SyncStatePort`` written from its docstrings, nothing else.

    ``get_state`` returns a fresh ``SyncState()`` when nothing was
    stored; ``put_state`` is last-write-wins; ``connections`` is creation
    order; ``add_connection`` raises ``ConflictError`` carrying
    ``existing_id`` on a duplicate id and never overwrites. Tenant
    hygiene is the port's stated rule, so ``tenant_id`` keys everything.
    """

    def __init__(self) -> None:
        self.states: dict[tuple[str, str], SyncState] = {}
        self.records: dict[str, list[ConnectionRecord]] = {}

    def get_state(self, tenant_id: str, connection_id: str) -> SyncState:
        """Stored state, or a fresh default."""
        return self.states.get((tenant_id, connection_id), SyncState())

    def put_state(
        self, tenant_id: str, connection_id: str, state: SyncState
    ) -> None:
        """Last write wins."""
        self.states[(tenant_id, connection_id)] = state

    def connections(self, tenant_id: str) -> tuple[ConnectionRecord, ...]:
        """This tenant's records, in creation order."""
        return tuple(self.records.get(tenant_id, ()))

    def add_connection(self, tenant_id: str, record: ConnectionRecord) -> None:
        """Register a record; a duplicate id is a ``ConflictError``."""
        existing = self.records.setdefault(tenant_id, [])
        if any(stored.id == record.id for stored in existing):
            raise ConflictError(
                f"connection already exists: {record.id!r}", existing_id=record.id
            )
        existing.append(record)

    def tenants(self) -> tuple[str, ...]:
        """Every tenant this store knows, first-seen order.

        Present because order ``embed-ids-loop`` #21 requires the port to
        grow a tenant-enumeration method. Its NAME is not yet published,
        which is itself the risk this file exists to surface: a host
        cannot implement a method it has not been told about.
        """
        seen: list[str] = []
        for tenant_id in list(self.records) + [key[0] for key in self.states]:
            if tenant_id not in seen:
                seen.append(tenant_id)
        return tuple(seen)


class HostLedger:
    """A ``LedgerPort`` written from its docstrings, nothing else.

    Four methods, exactly as declared. Its cursor encoding is its OWN:
    the port promises only that a malformed cursor raises
    ``CursorError``, so a host is free to choose the format, and this one
    deliberately differs from the in-repo codec.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, LedgerTransaction]] = {}
        self.seq: dict[str, int] = {}

    def _next(self, tenant_id: str) -> int:
        """This tenant's next monotonic last-modified seq, from 1."""
        self.seq[tenant_id] = self.seq.get(tenant_id, 0) + 1
        return self.seq[tenant_id]

    def upsert(self, tenant_id: str, txns) -> list[SyncEvent]:
        """Insert, update, or RESURRECT: the declared upsert contract.

        "A re-delivered transaction whose payload is unchanged emits no
        event, UNLESS the stored row is removed, in which case the
        transaction is re-added: stored with ``removed=False``, a bumped
        seq, and an ADDED event."
        """
        store = self.rows.setdefault(tenant_id, {})
        events: list[SyncEvent] = []
        for txn in txns:
            stored = store.get(txn.id)
            if stored is not None and payload_equal(txn, stored):
                if not stored.removed:
                    continue
            row = LedgerTransaction(
                id=txn.id,
                chain_id=txn.chain_id,
                tx_hash=txn.tx_hash,
                account_id=txn.account_id,
                block_number=txn.block_number,
                initiated_at=txn.initiated_at,
                confirmed_at=txn.confirmed_at,
                entries=txn.entries,
                removed=False,
                last_modified_seq=self._next(tenant_id),
            )
            store[txn.id] = row
            events.append(SyncEvent(SyncEventKind.ADDED, row))
        return sorted(events, key=lambda event: event.transaction.last_modified_seq)

    def sync(
        self, tenant_id: str, cursor: str | None = None, limit: int = 100
    ) -> SyncPage:
        """Ascending-seq page since ``cursor``; REMOVED iff the row is."""
        since = 0
        if cursor is not None:
            if not cursor.startswith("hostcur:") or not cursor[8:].isdigit():
                raise CursorError(f"malformed cursor: {cursor!r}")
            since = int(cursor[8:])
        ordered = sorted(
            (
                row
                for row in self.rows.get(tenant_id, {}).values()
                if row.last_modified_seq > since
            ),
            key=lambda row: row.last_modified_seq,
        )
        window = ordered[:limit]
        events = tuple(
            SyncEvent(
                SyncEventKind.REMOVED if row.removed else SyncEventKind.ADDED, row
            )
            for row in window
        )
        last = window[-1].last_modified_seq if window else since
        return SyncPage(
            events=events,
            next_cursor=f"hostcur:{last}",
            has_more=len(ordered) > len(window),
        )

    def get(self, tenant_id: str, txn_id: str) -> LedgerTransaction:
        """One transaction, or ``NotFoundError`` inside this tenant."""
        row = self.rows.get(tenant_id, {}).get(txn_id)
        if row is None:
            raise NotFoundError(f"no such transaction: {txn_id!r}")
        return row

    def mark_removed(self, tenant_id: str, txn_ids) -> list[SyncEvent]:
        """Set ``removed=True`` with a bumped seq; REMOVED events."""
        store = self.rows.setdefault(tenant_id, {})
        events: list[SyncEvent] = []
        for txn_id in txn_ids:
            stored = store.get(txn_id)
            if stored is None or stored.removed:
                continue
            row = LedgerTransaction(
                id=stored.id,
                chain_id=stored.chain_id,
                tx_hash=stored.tx_hash,
                account_id=stored.account_id,
                block_number=stored.block_number,
                initiated_at=stored.initiated_at,
                confirmed_at=stored.confirmed_at,
                entries=stored.entries,
                removed=True,
                last_modified_seq=self._next(tenant_id),
            )
            store[txn_id] = row
            events.append(SyncEvent(SyncEventKind.REMOVED, row))
        return events


class HostSource:
    """The one object the facade demands both seams from."""

    def fetch_txlist(
        self,
        chain_id: str,
        address: str,
        *,
        start_block: int,
        end_block: int,
        page: int,
        offset: int,
        sort: str,
    ) -> list[dict]:
        """One page of a stable total order over the window."""
        window = [
            row
            for row in ROWS
            if start_block <= int(row["blockNumber"]) <= end_block
        ]
        window.sort(
            key=lambda row: (int(row["blockNumber"]), row["hash"]),
            reverse=sort == "desc",
        )
        first = (page - 1) * offset
        return window[first : first + offset]

    def balances(self, *args: Any, **kwargs: Any) -> tuple:
        """The other seam the facade binds; unused by these tests."""
        return ()


def _decode(chain_id, address, account_id, rows) -> list[LedgerTransaction]:
    """A ``Decoder`` written from its declared signature, only."""
    return [
        LedgerTransaction(
            id="txn_" + row["hash"],
            chain_id=chain_id,
            tx_hash=row["hash"],
            account_id=account_id,
            block_number=int(row["blockNumber"]),
            initiated_at=int(row["timeStamp"]) * 1000,
            confirmed_at=int(row["timeStamp"]) * 1000,
            entries=(Entry(NATIVE, Quantity(1, 18), Direction.IN),),
        )
        for row in rows
    ]


def _facade(ledger: Any, state: Any):
    """An ``Auradefi`` over host-owned ports and a fixed clock."""
    from auradefi.embed.facade import Auradefi

    return Auradefi(
        ledger=ledger,
        source=HostSource(),
        prices=object(),
        clock=FrozenClock(2_000_000),
        sync_state=state,
        decoder=_decode,
        sync_page_size=2,
    )


class TestSyncStatePortSeam:
    """A host store that satisfies the declared Protocol and no more."""

    def test_the_declared_surface_is_what_a_host_can_read(self):
        """Pin the published surface so a silent addition is visible.

        A method the facade needs but the ``Protocol`` does not declare
        is invisible to every host: they cannot implement what they were
        never told about.
        """
        assert declared_surface(SyncStatePort) >= {
            "get_state",
            "put_state",
            "connections",
            "add_connection",
        }

    def test_no_consumer_reaches_past_the_declared_sync_state_surface(self):
        """Connect and sync through a proxy gated by the Protocol.

        Any ``AttributeError`` here names a method some consumer calls
        that ``SyncStatePort`` never promised.
        """
        store = DeclaredOnly(HostSyncState(), SyncStatePort)
        auradefi = _facade(HostLedger(), store)
        user = auradefi.user("seam-user-1")
        user.connect_address(CHAIN, ADDRESS)
        auradefi.sync(budget=5)
        assert set(store.reached) <= declared_surface(SyncStatePort)

    def test_a_host_owned_store_is_the_only_durable_record(self):
        """A restart keeps the store and loses the process.

        ``embed-ids-loop`` #21: after a rebind the in-process tenant list
        is empty, so the port is the only place the connection exists. A
        ``no_op=True`` here means "I could not see anything to do"
        reported as "nothing needed doing".
        """
        store = HostSyncState()
        ledger = HostLedger()
        first = _facade(ledger, store)
        first.user("seam-user-1").connect_address(CHAIN, ADDRESS)

        restarted = _facade(ledger, store)
        report = restarted.sync(budget=5)
        assert report.no_op is False, (
            "a fresh Auradefi over the SAME host store reported "
            f"no_op=True with {len(store.records[next(iter(store.records))])} "
            "connection(s) waiting in the port"
        )
        assert report.transactions_ingested > 0


class TestLedgerPortSeam:
    """The declared four methods must carry every consumer."""

    def test_the_engine_ingests_through_the_declared_ledger_surface(self):
        """``SyncEngine`` must not need a fifth ledger method."""
        ledger = HostLedger()
        gated = DeclaredOnly(ledger, LedgerPort)
        state = HostSyncState()
        engine = SyncEngine(
            gated, state, HostSource(), _decode, FrozenClock(2_000_000), 0, 2
        )
        connection = ConnectionRecord(
            id="conn_seam2", chain_id=CHAIN, address=ADDRESS, created_at_ms=0
        )
        report = engine.sync_connection(TENANT, connection, 5)
        assert report.transactions_ingested == 2
        assert set(gated.reached) <= declared_surface(LedgerPort)

    def test_apply_reorg_is_not_part_of_the_declared_contract(self):
        """Both in-repo backends have it; the port declares neither it nor
        any other way to apply a ``ReorgPlan`` atomically."""
        assert "apply_reorg" not in declared_surface(LedgerPort)

    def test_a_reorg_plan_resurrects_through_the_declared_surface_alone(self):
        """``ledger-reorg`` #22 end to end, on a host-written ledger.

        The plan's ``add`` bucket now carries a byte-identical row whose
        stored copy is ``removed``. The only application path a host is
        promised is ``mark_removed`` then ``upsert``, and ``upsert``'s
        docstring is what has to make the resurrection happen.
        """
        ledger = DeclaredOnly(HostLedger(), LedgerPort)
        row = LedgerTransaction(
            id="txn_resurrect",
            chain_id=CHAIN,
            tx_hash="0xdead",
            account_id="conn_seam2",
            block_number=500,
            initiated_at=1_700_000_000_000,
            confirmed_at=1_700_000_000_000,
            entries=(Entry(NATIVE, Quantity(1, 18), Direction.IN),),
        )
        ledger.upsert(TENANT, [row])
        ledger.mark_removed(TENANT, ["txn_resurrect"])
        assert ledger.get(TENANT, "txn_resurrect").removed is True

        stored = [ledger.get(TENANT, "txn_resurrect")]
        plan = plan_reorg(stored, [row], from_block=500)
        assert plan.add and plan.add[0].id == "txn_resurrect", (
            "an orphaned row back on chain with a byte-identical payload "
            "must be planned for re-add"
        )
        events = ledger.upsert(TENANT, list(plan.add))
        assert [event.kind for event in events] == [SyncEventKind.ADDED]
        assert ledger.get(TENANT, "txn_resurrect").removed is False
        assert set(ledger.reached) <= declared_surface(LedgerPort)


def test_the_facade_validates_both_the_source_and_the_state_seam():
    """Bind-time validation covers BOTH ports, not just ``source``.

    This seam originally recorded the asymmetry as a finding:
    ``Auradefi.__init__`` raised for a ``source`` missing either seam,
    "the failure belongs at bind time, not at the first background
    tick", while accepting any object at all as ``sync_state``. Once #21
    made ``sync()`` enumerate connections from that port, a store missing
    the new method failed exactly the way that docstring says it must
    not: hours later, on a background tick.

    The finding was fixed, so this now pins the fix. Both seams are
    checked where the host binds them.
    """
    from auradefi.embed.facade import Auradefi

    with pytest.raises(ValidationError):
        Auradefi(ledger=HostLedger(), source=object(), prices=object())

    class NotAPort:
        """No method of ``SyncStatePort`` at all."""

    with pytest.raises(ValidationError):
        _facade(HostLedger(), NotAPort())

    assert isinstance(NotAPort(), SyncStatePort) is False

    class MissingOnlyTenants:
        """Every SyncStatePort member except the one #21 added."""

        def get_state(self, tenant_id, connection_id): ...  # noqa: ANN001
        def put_state(self, tenant_id, connection_id, state): ...  # noqa: ANN001
        def connections(self, tenant_id): return ()  # noqa: ANN001
        def add_connection(self, tenant_id, record): ...  # noqa: ANN001

    with pytest.raises(ValidationError):
        _facade(HostLedger(), MissingOnlyTenants())
