"""Contract tests for auradefi.ledger.backends.sqlmodel (SPEC §8, §6.4).

SqlModelLedger sits behind the HOST's session factory: zero I/O at
construction, no engine building, no DDL — and semantics IDENTICAL to
the pinned reference backend (MemoryLedger), verified both against
hardcoded goldens and differentially against the reference itself.

Everything runs on in-memory sqlite (file-free, socket-free — the
autouse offline guard stays satisfied). Cursor literals are
``f"{seq:020d}"`` and transaction ids come from the pinned id algorithm
— derived independently via ``python3 -c``, never from the code under
test. The backend is constructed INSIDE test bodies so a stub fails
with NotImplementedError instead of erroring during fixture setup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import Session

from auradefi.errors import (
    CursorError,
    NotFoundError,
    TenantIsolationError,
    ValidationError,
)
from auradefi.ledger.backends import sqlmodel as sqlmodel_backend
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.ledger.backends.models import (
    LedgerTransactionRow,
    TenantSeqRow,
    metadata,
)
from auradefi.ledger.backends.sqlmodel import SqlModelLedger
from auradefi.ledger.models import SyncEventKind, payload_equal
from auradefi.ledger.port import LedgerPort
from auradefi.ledger.reorg import ReorgPlan
from auradefi.money.quantity import Quantity

# Derived independently; NEVER regenerate from the implementation.
# f"{seq:020d}" — 20 ASCII digits, lexicographic order == numeric order.
CURSOR_0 = "00000000000000000000"
CURSOR_1 = "00000000000000000001"
CURSOR_2 = "00000000000000000002"
CURSOR_3 = "00000000000000000003"
CURSOR_4 = "00000000000000000004"
CURSOR_5 = "00000000000000000005"
CURSOR_6 = "00000000000000000006"

# chain eip155:1, acct_1 (pinned id algorithm over chain|tx_hash|account).
ID_A = "txn_fb618872cdc184c0"  # 0xaaa
ID_B = "txn_9d8e7888ce01e8a5"  # 0xbbb
ID_BB2 = "txn_6b20cedf697f79fb"  # 0xbb2 — the reorg replacement
ID_C = "txn_07c85f8766037afc"  # 0xccc
ID_D = "txn_5a76d20d6d9b55d6"  # 0xddd — never written anywhere

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
MS = 1_754_000_000_000

# Single-entry wire golden (rule #2) — derived independently.
GOLDEN_ONE = (
    '[{"asset_id":"eip155:1/slip44:60","decimals":18,'
    '"direction":"in","raw":"1500000000000000000"}]'
)


def _bare_engine():
    """In-memory sqlite engine with NO schema — DDL is the test's job."""
    return create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


def _ledger(engine) -> SqlModelLedger:
    """Bind a fresh host-style session factory over ``engine``."""
    return SqlModelLedger(lambda: Session(engine))


def _exploding_factory() -> Session:
    raise AssertionError("a session was opened before tenant validation")


def _triples(events):
    return [
        (
            event.kind,
            event.transaction.id,
            event.transaction.last_modified_seq,
            event.transaction.removed,
        )
        for event in events
    ]


@pytest.fixture
def engine():
    engine = _bare_engine()
    metadata.create_all(engine)  # the TEST owns DDL — the backend never may
    return engine


@pytest.fixture
def txn_a(make_txn):
    return make_txn(id=ID_A, tx_hash="0xaaa")


@pytest.fixture
def txn_b(make_txn):
    return make_txn(id=ID_B, tx_hash="0xbbb")


class TestConstructorIsInert:
    def test_zero_db_work_until_the_host_creates_schema(self):
        # SPEC §8: we never open a connection the host didn't hand us.
        engine = _bare_engine()
        _ledger(engine)
        assert inspect(engine).get_table_names() == []
        metadata.create_all(engine)
        assert {
            "auradefi_ledger_transactions",
            "auradefi_ledger_seqs",
        } <= set(inspect(engine).get_table_names())

    def test_constructor_never_calls_the_factory(self):
        # ZERO I/O in the constructor: the factory must stay untouched.
        _ledger_over_bomb = SqlModelLedger(_exploding_factory)
        assert _ledger_over_bomb is not None

    def test_backend_source_builds_no_engine_and_emits_no_ddl(self):
        source = Path(sqlmodel_backend.__file__).read_text(encoding="utf-8")
        assert "create_engine" not in source
        assert "create_all" not in source


class TestPortConformance:
    def test_isinstance_of_ledger_port(self, engine):
        assert isinstance(_ledger(engine), LedgerPort) is True

    def test_structural_not_nominal(self):
        # Rule #12: satisfied by shape, never by inheritance.
        assert LedgerPort not in SqlModelLedger.__mro__


# method name -> call exercising it with a given tenant_id
_CALLS = {
    "upsert": lambda led, t: led.upsert(t, []),
    "sync": lambda led, t: led.sync(t),
    "get": lambda led, t: led.get(t, ID_A),
    "mark_removed": lambda led, t: led.mark_removed(t, [ID_A]),
    "apply_reorg": lambda led, t: led.apply_reorg(t, ReorgPlan((), ())),
}


class TestTenantIdValidation:
    """tenant_id is validated FIRST — before any session is opened.

    The factory raises AssertionError if called, so a backend that opens
    a session before validating fails these tests loudly.
    """

    @pytest.mark.parametrize("method", sorted(_CALLS))
    @pytest.mark.parametrize("bad", ["", " ", "\t", "  \t "], ids=repr)
    def test_blank_tenant_raises_before_any_session(self, method, bad):
        led = SqlModelLedger(_exploding_factory)
        with pytest.raises(TenantIsolationError):
            _CALLS[method](led, bad)

    @pytest.mark.parametrize("method", sorted(_CALLS))
    @pytest.mark.parametrize("bad", [None, 7], ids=repr)
    def test_non_str_tenant_raises_before_any_session(self, method, bad):
        led = SqlModelLedger(_exploding_factory)
        with pytest.raises(TenantIsolationError):
            _CALLS[method](led, bad)


class TestGoldenSemantics:
    """The acceptance script, hardcoded — memory-reference semantics."""

    def test_upsert_two_txns_added_events_seqs_1_2(self, engine, txn_a, txn_b):
        led = _ledger(engine)
        events = led.upsert(TENANT_A, [txn_a, txn_b])
        assert _triples(events) == [
            (SyncEventKind.ADDED, ID_A, 1, False),
            (SyncEventKind.ADDED, ID_B, 2, False),
        ]
        assert payload_equal(led.get(TENANT_A, ID_A), txn_a) is True

    def test_full_lifecycle_script_seqs_1_through_5(
        self, engine, txn_a, txn_b, make_txn
    ):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a, txn_b])  # ADDED seqs 1, 2

        # Identical redelivery: no events, seq still 2.
        assert led.upsert(TENANT_A, [txn_a, txn_b]) == []
        assert led.get(TENANT_A, ID_A).last_modified_seq == 1
        assert led.sync(TENANT_A).next_cursor == CURSOR_2

        # Changed payload: ADDED at seq 3.
        changed_a = make_txn(
            id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000
        )
        events = led.upsert(TENANT_A, [changed_a])
        assert _triples(events) == [(SyncEventKind.ADDED, ID_A, 3, False)]

        # mark_removed: REMOVED at seq 4.
        events = led.mark_removed(TENANT_A, [ID_A])
        assert _triples(events) == [(SyncEventKind.REMOVED, ID_A, 4, True)]

        # Identical redelivery of the REMOVED row resurrects: ADDED seq 5.
        events = led.upsert(TENANT_A, [changed_a])
        assert _triples(events) == [(SyncEventKind.ADDED, ID_A, 5, False)]
        stored = led.get(TENANT_A, ID_A)
        assert stored.removed is False
        assert stored.last_modified_seq == 5
        assert stored.confirmed_at == MS + 24_000

    def test_incoming_bookkeeping_never_adopted(self, engine, txn_a, make_txn):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])
        redelivered = make_txn(
            id=ID_A, tx_hash="0xaaa", last_modified_seq=500, removed=True
        )
        assert led.upsert(TENANT_A, [redelivered]) == []
        stored = led.get(TENANT_A, ID_A)
        assert stored.last_modified_seq == 1
        assert stored.removed is False

    def test_mixed_batch_writes_in_incoming_order(
        self, engine, txn_a, txn_b, make_txn
    ):
        # Seqs follow INCOMING order, not bucket order: a backend that
        # writes plan.new before plan.changed gets these two backwards.
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a, txn_b])  # seqs 1, 2
        changed_a = make_txn(
            id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000
        )
        new_c = make_txn(id=ID_C, tx_hash="0xccc")
        events = led.upsert(TENANT_A, [txn_b, changed_a, new_c])
        assert _triples(events) == [
            (SyncEventKind.ADDED, ID_A, 3, False),
            (SyncEventKind.ADDED, ID_C, 4, False),
        ]
        assert led.get(TENANT_A, ID_B).last_modified_seq == 2  # untouched

    def test_empty_batches_are_noops_that_burn_no_seq(self, engine, txn_a):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])  # seq 1
        assert led.upsert(TENANT_A, []) == []
        assert led.mark_removed(TENANT_A, []) == []
        assert led.apply_reorg(TENANT_A, ReorgPlan((), ())) == []
        assert led.sync(TENANT_A).next_cursor == CURSOR_1

    def test_upsert_events_equal_sync_events(self, engine, txn_a, txn_b):
        # The events upsert returns ARE the stored rows sync will show.
        led = _ledger(engine)
        events = led.upsert(TENANT_A, [txn_a, txn_b])
        assert events == list(led.sync(TENANT_A).events)

    def test_event_stream_matches_the_memory_reference(
        self, engine, txn_a, txn_b, make_txn
    ):
        # Differential harness: the same script through both backends
        # must produce identical event streams and sync pages.
        changed_a = make_txn(
            id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000
        )
        replacement = make_txn(id=ID_BB2, tx_hash="0xbb2")
        script = [
            lambda led: led.upsert(TENANT_A, [txn_a, txn_b]),
            lambda led: led.upsert(TENANT_A, [txn_a]),
            lambda led: led.upsert(TENANT_A, [changed_a]),
            lambda led: led.mark_removed(TENANT_A, [ID_A]),
            lambda led: led.upsert(TENANT_A, [changed_a]),
            lambda led: led.apply_reorg(
                TENANT_A, ReorgPlan(remove_ids=(ID_B,), add=(replacement,))
            ),
        ]
        sql_led = _ledger(engine)
        reference = MemoryLedger()
        for step in script:
            assert _triples(step(sql_led)) == _triples(step(reference))
        sql_page = sql_led.sync(TENANT_A, limit=3)
        ref_page = reference.sync(TENANT_A, limit=3)
        assert _triples(sql_page.events) == _triples(ref_page.events)
        assert sql_page.next_cursor == ref_page.next_cursor
        assert sql_page.has_more == ref_page.has_more


class TestGet:
    def test_returns_the_stored_transaction(self, engine, txn_a):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])
        stored = led.get(TENANT_A, ID_A)
        assert stored.id == ID_A
        assert stored.tx_hash == "0xaaa"
        assert payload_equal(stored, txn_a) is True

    def test_unknown_id_raises_not_found(self, engine):
        led = _ledger(engine)
        with pytest.raises(NotFoundError):
            led.get(TENANT_A, ID_D)

    def test_pending_txn_nulls_survive_the_db_round_trip(
        self, engine, make_txn
    ):
        # A pending txn has block_number/confirmed_at None, never 0 —
        # nullable columns must come back as None through the ORM.
        txn = make_txn(
            id=ID_A, tx_hash="0xaaa", block_number=None, confirmed_at=None
        )
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn])
        stored = led.get(TENANT_A, ID_A)
        assert stored.block_number is None
        assert stored.confirmed_at is None
        assert payload_equal(stored, txn) is True


class TestMarkRemoved:
    def test_removed_event_state_and_seq_bump(self, engine, txn_a):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])  # seq 1
        events = led.mark_removed(TENANT_A, [ID_A])
        assert _triples(events) == [(SyncEventKind.REMOVED, ID_A, 2, True)]
        stored = led.get(TENANT_A, ID_A)
        assert stored.removed is True
        assert stored.last_modified_seq == 2

    def test_already_removed_is_a_silent_noop(self, engine, txn_a):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])
        led.mark_removed(TENANT_A, [ID_A])  # seq 2
        assert led.mark_removed(TENANT_A, [ID_A]) == []
        assert led.get(TENANT_A, ID_A).last_modified_seq == 2

    def test_any_unknown_id_raises_before_any_write(self, engine, txn_a):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])  # seq 1
        with pytest.raises(NotFoundError):
            led.mark_removed(TENANT_A, [ID_A, ID_D])  # ID_D unknown
        stored = led.get(TENANT_A, ID_A)
        assert stored.removed is False  # the known id was NOT removed
        assert stored.last_modified_seq == 1


class TestSyncPaging:
    def test_two_writes_page_one_by_one(self, engine, txn_a, txn_b):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a, txn_b])

        p1 = led.sync(TENANT_A, None, limit=1)
        assert _triples(p1.events) == [(SyncEventKind.ADDED, ID_A, 1, False)]
        assert p1.has_more is True
        assert p1.next_cursor == CURSOR_1

        p2 = led.sync(TENANT_A, p1.next_cursor, limit=1)
        assert _triples(p2.events) == [(SyncEventKind.ADDED, ID_B, 2, False)]
        assert p2.has_more is False
        assert p2.next_cursor == CURSOR_2

    def test_empty_ledger_page(self, engine):
        led = _ledger(engine)
        page = led.sync(TENANT_A)
        assert page.events == ()
        assert page.next_cursor == CURSOR_0
        assert page.has_more is False

    @pytest.mark.parametrize(
        "bad", ["abc", "42", "0000000000000000000", "-0000000000000000001"]
    )
    def test_malformed_cursor_raises_cursor_error(self, engine, bad):
        led = _ledger(engine)
        with pytest.raises(CursorError):
            led.sync(TENANT_A, bad)

    @pytest.mark.parametrize("limit", [0, -1])
    def test_limit_below_one_raises_validation_error(self, engine, limit):
        led = _ledger(engine)
        with pytest.raises(ValidationError):
            led.sync(TENANT_A, limit=limit)

    def test_empty_page_echoes_the_decoded_input_cursor(self, engine, txn_a):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])
        page = led.sync(TENANT_A, CURSOR_5)
        assert page.events == ()
        assert page.next_cursor == CURSOR_5
        assert page.has_more is False

    def test_removed_iff_stored_row_is_removed(self, engine, txn_a):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])
        led.mark_removed(TENANT_A, [ID_A])
        page = led.sync(TENANT_A)
        # State-based: ONE event per row, at its current seq.
        assert _triples(page.events) == [
            (SyncEventKind.REMOVED, ID_A, 2, True)
        ]
        assert page.next_cursor == CURSOR_2

    def test_modified_row_reappears_once_at_its_new_seq(
        self, engine, txn_a, txn_b, make_txn
    ):
        # SPEC §6.4: last-modified order, NOT transaction date.
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a, txn_b])  # A=1, B=2
        changed_a = make_txn(
            id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000
        )
        led.upsert(TENANT_A, [changed_a])  # A=3
        page = led.sync(TENANT_A)
        assert [e.transaction.id for e in page.events] == [ID_B, ID_A]
        assert [
            e.transaction.last_modified_seq for e in page.events
        ] == [2, 3]
        assert page.next_cursor == CURSOR_3

    def test_duplicate_ids_in_batch_raise_and_write_nothing(
        self, engine, txn_a, txn_b, make_txn
    ):
        led = _ledger(engine)
        twin = make_txn(id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000)
        with pytest.raises(ValidationError):
            led.upsert(TENANT_A, [txn_a, twin])
        with pytest.raises(NotFoundError):
            led.get(TENANT_A, ID_A)
        page = led.sync(TENANT_A)
        assert page.events == ()
        assert page.next_cursor == CURSOR_0
        # The seq counter was never touched: the next write gets 1.
        events = led.upsert(TENANT_A, [txn_b])
        assert events[0].transaction.last_modified_seq == 1


class TestTenantIsolation:
    """SPEC §13 attempted-leak contract (rule #6) — isolation that tries."""

    def test_tenant_b_get_on_a_id_raises_not_found(self, engine, txn_a):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])
        with pytest.raises(NotFoundError):
            led.get(TENANT_B, ID_A)

    def test_tenant_b_sync_sees_zero_of_tenant_a_events(self, engine, txn_a):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])
        page = led.sync(TENANT_B)
        assert page.events == ()
        assert page.next_cursor == CURSOR_0
        assert page.has_more is False

    def test_tenant_b_mark_removed_raises_and_a_is_untouched(
        self, engine, txn_a
    ):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])
        with pytest.raises(NotFoundError):
            led.mark_removed(TENANT_B, [ID_A])
        stored = led.get(TENANT_A, ID_A)
        assert stored.removed is False
        assert stored.last_modified_seq == 1

    def test_seq_counters_are_per_tenant(
        self, engine, txn_a, txn_b, make_txn
    ):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a, txn_b])  # A at seqs 1, 2
        events = led.upsert(TENANT_B, [make_txn(id=ID_C, tx_hash="0xccc")])
        assert events[0].transaction.last_modified_seq == 1
        assert led.sync(TENANT_B).next_cursor == CURSOR_1

    def test_same_id_in_two_tenants_are_independent_rows(
        self, engine, txn_a, make_txn
    ):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])
        led.upsert(TENANT_B, [txn_a])
        changed = make_txn(id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000)
        led.upsert(TENANT_A, [changed])
        assert led.get(TENANT_A, ID_A).confirmed_at == MS + 24_000
        assert led.get(TENANT_B, ID_A).confirmed_at == MS + 12_000
        assert led.get(TENANT_B, ID_A).last_modified_seq == 1


class TestPersistenceAcrossBindings:
    def test_second_binding_reads_rows_and_continues_the_seq(
        self, engine, txn_a, txn_b, make_txn
    ):
        # The counter lives in TenantSeqRow, not in the Python object.
        first = _ledger(engine)
        first.upsert(TENANT_A, [txn_a, txn_b])  # seqs 1, 2
        changed_a = make_txn(
            id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000
        )
        first.upsert(TENANT_A, [changed_a])  # seq 3
        first.mark_removed(TENANT_A, [ID_A])  # seq 4
        first.upsert(TENANT_A, [changed_a])  # resurrection, seq 5

        second = SqlModelLedger(lambda: Session(engine))  # NEW factory
        stored_a = second.get(TENANT_A, ID_A)
        assert stored_a.removed is False
        assert stored_a.last_modified_seq == 5
        assert stored_a.confirmed_at == MS + 24_000
        assert second.get(TENANT_A, ID_B).last_modified_seq == 2
        page = second.sync(TENANT_A)
        assert [e.transaction.id for e in page.events] == [ID_B, ID_A]
        assert page.next_cursor == CURSOR_5

        events = second.upsert(TENANT_A, [make_txn(id=ID_C, tx_hash="0xccc")])
        assert events[0].transaction.last_modified_seq == 6  # continues

        with Session(engine) as session:
            seq_row = session.get(TenantSeqRow, TENANT_A)
            assert seq_row is not None
            assert seq_row.seq == 6


class TestEntriesOnTheWire:
    def test_stored_row_carries_the_pinned_wire_golden(self, engine, txn_a):
        # Rule #2 through the whole stack: what actually lands in the
        # host's database is the canonical JSON with raw as a STRING.
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])
        with Session(engine) as session:
            row = session.get(LedgerTransactionRow, (TENANT_A, ID_A))
            assert row is not None
            assert row.entries_json == GOLDEN_ONE

    def test_78_digit_raw_survives_the_db_round_trip(
        self, engine, make_txn, make_entry
    ):
        huge = make_entry(quantity=Quantity(10**77, 18))
        txn = make_txn(id=ID_A, tx_hash="0xaaa", entries=(huge,))
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn])
        stored = led.get(TENANT_A, ID_A)
        assert stored.entries[0].quantity == Quantity(10**77, 18)
        assert stored.entries[0].quantity.raw == 10**77


class TestApplyReorg:
    def test_remove_then_readd_changed_with_consecutive_seqs(
        self, engine, txn_a, txn_b, make_txn
    ):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a, txn_b])  # seqs 1, 2
        changed_a = make_txn(
            id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000
        )
        plan = ReorgPlan(remove_ids=(ID_A,), add=(changed_a,))
        events = led.apply_reorg(TENANT_A, plan)
        assert _triples(events) == [
            (SyncEventKind.REMOVED, ID_A, 3, True),
            (SyncEventKind.ADDED, ID_A, 4, False),
        ]
        stored = led.get(TENANT_A, ID_A)
        assert stored.removed is False
        assert stored.last_modified_seq == 4
        assert stored.confirmed_at == MS + 24_000
        # Survivor untouched.
        assert led.get(TENANT_A, ID_B).last_modified_seq == 2

    def test_duplicate_add_ids_raise_and_touch_nothing(
        self, engine, txn_a, txn_b, make_txn
    ):
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])  # seq 1
        twin_1 = make_txn(id=ID_BB2, tx_hash="0xbb2")
        twin_2 = make_txn(
            id=ID_BB2, tx_hash="0xbb2", confirmed_at=MS + 24_000
        )
        plan = ReorgPlan(remove_ids=(ID_A,), add=(twin_1, twin_2))
        with pytest.raises(ValidationError):
            led.apply_reorg(TENANT_A, plan)
        untouched = led.get(TENANT_A, ID_A)
        assert untouched.removed is False  # the remove never landed
        assert untouched.last_modified_seq == 1
        with pytest.raises(NotFoundError):
            led.get(TENANT_A, ID_BB2)  # the add never landed either
        # The seq counter was never bumped: the next write gets 2.
        events = led.upsert(TENANT_A, [txn_b])
        assert events[0].transaction.last_modified_seq == 2

    def test_unknown_remove_id_rolls_the_whole_plan_back(
        self, engine, txn_a, txn_b, make_txn
    ):
        # Atomicity: one session/commit for the whole plan — a failure
        # after the first removal must leave NOTHING applied.
        led = _ledger(engine)
        led.upsert(TENANT_A, [txn_a])  # seq 1
        plan = ReorgPlan(
            remove_ids=(ID_A, ID_D),  # ID_D was never written
            add=(make_txn(id=ID_C, tx_hash="0xccc"),),
        )
        with pytest.raises(NotFoundError):
            led.apply_reorg(TENANT_A, plan)
        stored = led.get(TENANT_A, ID_A)
        assert stored.removed is False
        assert stored.last_modified_seq == 1
        with pytest.raises(NotFoundError):
            led.get(TENANT_A, ID_C)
        events = led.upsert(TENANT_A, [txn_b])
        assert events[0].transaction.last_modified_seq == 2


class TestOneSessionPerPublicCall:
    def test_each_public_call_opens_exactly_one_session(
        self, engine, txn_a, txn_b
    ):
        # apply_reorg composes mark_removed + upsert INSIDE one session —
        # never one session per delegated half.
        opened: list[int] = []

        def counting_factory() -> Session:
            opened.append(1)
            return Session(engine)

        led = SqlModelLedger(counting_factory)
        assert opened == []  # constructor did zero I/O
        led.upsert(TENANT_A, [txn_a])
        assert len(opened) == 1
        led.sync(TENANT_A)
        assert len(opened) == 2
        led.get(TENANT_A, ID_A)
        assert len(opened) == 3
        led.mark_removed(TENANT_A, [ID_A])
        assert len(opened) == 4
        led.apply_reorg(TENANT_A, ReorgPlan(remove_ids=(), add=(txn_b,)))
        assert len(opened) == 5
