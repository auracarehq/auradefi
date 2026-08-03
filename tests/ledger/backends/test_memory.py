"""Contract tests for auradefi.ledger.backends.memory (SPEC §6.4, §13).

MemoryLedger is the reference backend: per-tenant isolation (rule #6),
per-tenant monotonic seq starting at 1, idempotent upsert, removal as a
first-class REMOVED event, and state-based sync pages ascending by
last-modified seq.

Cursor literals are ``f"{seq:020d}"`` (DECISIONS pinned) and transaction
ids come from the pinned id algorithm — both derived independently via
``python3 -c``, never from the code under test.
"""

from __future__ import annotations

import pytest

from auradefi.errors import (
    CursorError,
    NotFoundError,
    TenantIsolationError,
    ValidationError,
)
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.ledger.models import SyncEventKind, SyncPage, payload_equal
from auradefi.ledger.port import LedgerPort
from auradefi.ledger.reorg import ReorgPlan, plan_reorg

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
ID_D = "txn_5a76d20d6d9b55d6"  # 0xddd
ID_E = "txn_eca5ea7d6313e253"  # 0xeee

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
MS = 1_754_000_000_000
FORK = 19_000_000


@pytest.fixture
def ledger():
    return MemoryLedger()


@pytest.fixture
def txn_a(make_txn):
    return make_txn(id=ID_A, tx_hash="0xaaa")


@pytest.fixture
def txn_b(make_txn):
    return make_txn(id=ID_B, tx_hash="0xbbb")


@pytest.fixture
def five_txns(make_txn):
    return [
        make_txn(id=ID_A, tx_hash="0xaaa"),
        make_txn(id=ID_B, tx_hash="0xbbb"),
        make_txn(id=ID_C, tx_hash="0xccc"),
        make_txn(id=ID_D, tx_hash="0xddd"),
        make_txn(id=ID_E, tx_hash="0xeee"),
    ]


class TestPortConformance:
    def test_isinstance_of_ledger_port(self, ledger):
        assert isinstance(ledger, LedgerPort) is True

    def test_structural_not_nominal(self, ledger):
        # Rule #12: satisfied by shape, never by inheritance.
        assert LedgerPort not in type(ledger).__mro__


# method name -> call exercising it with a given tenant_id
_CALLS = {
    "upsert": lambda led, t: led.upsert(t, []),
    "sync": lambda led, t: led.sync(t),
    "get": lambda led, t: led.get(t, ID_A),
    "mark_removed": lambda led, t: led.mark_removed(t, [ID_A]),
    "apply_reorg": lambda led, t: led.apply_reorg(t, ReorgPlan((), ())),
}


class TestTenantIdValidation:
    @pytest.mark.parametrize("method", sorted(_CALLS))
    @pytest.mark.parametrize("bad", ["", " ", "\t", "\n", "  \t "], ids=repr)
    def test_blank_tenant_raises_on_every_method(self, ledger, method, bad):
        with pytest.raises(TenantIsolationError):
            _CALLS[method](ledger, bad)

    @pytest.mark.parametrize("method", sorted(_CALLS))
    def test_non_str_tenant_raises_on_every_method(self, ledger, method):
        with pytest.raises(TenantIsolationError):
            _CALLS[method](ledger, None)


class TestUpsert:
    def test_new_txn_added_event_and_seq_starts_at_1(self, ledger, txn_a):
        events = ledger.upsert(TENANT_A, [txn_a])
        assert len(events) == 1
        assert events[0].kind is SyncEventKind.ADDED
        assert events[0].transaction.id == ID_A
        stored = ledger.get(TENANT_A, ID_A)
        assert stored.last_modified_seq == 1
        assert stored.removed is False
        assert payload_equal(stored, txn_a) is True

    def test_batch_events_ascend_and_seqs_are_1_2(self, ledger, txn_a, txn_b):
        events = ledger.upsert(TENANT_A, [txn_a, txn_b])
        assert [e.kind for e in events] == [SyncEventKind.ADDED] * 2
        seqs = [e.transaction.last_modified_seq for e in events]
        assert seqs == sorted(seqs)
        assert ledger.get(TENANT_A, ID_A).last_modified_seq == 1
        assert ledger.get(TENANT_A, ID_B).last_modified_seq == 2

    def test_upsert_events_equal_sync_events(self, ledger, txn_a, txn_b):
        # The events upsert returns ARE the stored rows sync will show.
        events = ledger.upsert(TENANT_A, [txn_a, txn_b])
        page = ledger.sync(TENANT_A)
        assert events == list(page.events)

    def test_identical_redelivery_no_event_no_seq_bump(self, ledger, txn_a):
        ledger.upsert(TENANT_A, [txn_a])
        cursor = ledger.sync(TENANT_A).next_cursor
        assert cursor == CURSOR_1

        assert ledger.upsert(TENANT_A, [txn_a]) == []
        assert ledger.get(TENANT_A, ID_A).last_modified_seq == 1
        page = ledger.sync(TENANT_A, cursor=cursor)
        assert page.events == ()
        assert page.next_cursor == CURSOR_1
        assert page.has_more is False

    def test_incoming_bookkeeping_never_adopted(self, ledger, txn_a, make_txn):
        ledger.upsert(TENANT_A, [txn_a])
        redelivered = make_txn(
            id=ID_A, tx_hash="0xaaa", last_modified_seq=500, removed=True
        )
        assert ledger.upsert(TENANT_A, [redelivered]) == []
        stored = ledger.get(TENANT_A, ID_A)
        assert stored.last_modified_seq == 1
        assert stored.removed is False

    def test_changed_payload_emits_added_and_bumps(self, ledger, txn_a, make_txn):
        ledger.upsert(TENANT_A, [txn_a])
        changed = make_txn(id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000)
        events = ledger.upsert(TENANT_A, [changed])
        assert len(events) == 1
        assert events[0].kind is SyncEventKind.ADDED
        stored = ledger.get(TENANT_A, ID_A)
        assert stored.last_modified_seq == 2
        assert stored.confirmed_at == MS + 24_000

    def test_mixed_batch_only_new_and_changed_emit(
        self, ledger, txn_a, txn_b, make_txn
    ):
        ledger.upsert(TENANT_A, [txn_a, txn_b])  # seqs 1, 2
        changed_a = make_txn(id=ID_A, tx_hash="0xaaa", block_number=FORK + 9)
        new_c = make_txn(id=ID_C, tx_hash="0xccc")
        events = ledger.upsert(TENANT_A, [new_c, changed_a, txn_b])
        assert {e.transaction.id for e in events} == {ID_A, ID_C}
        seqs = [e.transaction.last_modified_seq for e in events]
        assert seqs == [3, 4]
        assert ledger.get(TENANT_A, ID_B).last_modified_seq == 2  # untouched

    def test_duplicate_ids_in_batch_raise_and_write_nothing(
        self, ledger, txn_a, make_txn
    ):
        twin = make_txn(id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000)
        with pytest.raises(ValidationError):
            ledger.upsert(TENANT_A, [txn_a, twin])
        with pytest.raises(NotFoundError):
            ledger.get(TENANT_A, ID_A)
        page = ledger.sync(TENANT_A)
        assert page.events == ()
        assert page.next_cursor == CURSOR_0

    def test_empty_batch_is_a_noop(self, ledger):
        assert ledger.upsert(TENANT_A, []) == []
        assert ledger.sync(TENANT_A).next_cursor == CURSOR_0

    def test_identical_redelivery_onto_removed_row_resurrects(
        self, ledger, txn_a
    ):
        # SPEC §6.4 re-add semantics: identical payload + stored
        # removed=True is the ONE case where redelivery emits an event.
        ledger.upsert(TENANT_A, [txn_a])  # seq 1
        ledger.mark_removed(TENANT_A, [ID_A])  # seq 2
        events = ledger.upsert(TENANT_A, [txn_a])
        assert len(events) == 1
        assert events[0].kind is SyncEventKind.ADDED
        assert events[0].transaction.id == ID_A
        stored = ledger.get(TENANT_A, ID_A)
        assert stored.removed is False
        assert stored.last_modified_seq == 3  # strictly bumped past 2

    def test_identical_redelivery_after_resurrection_is_a_noop(
        self, ledger, txn_a
    ):
        ledger.upsert(TENANT_A, [txn_a])  # seq 1
        ledger.mark_removed(TENANT_A, [ID_A])  # seq 2
        ledger.upsert(TENANT_A, [txn_a])  # resurrected at seq 3
        assert ledger.upsert(TENANT_A, [txn_a]) == []
        assert ledger.get(TENANT_A, ID_A).last_modified_seq == 3
        page = ledger.sync(TENANT_A, cursor=CURSOR_3)
        assert page.events == ()
        assert page.next_cursor == CURSOR_3

    def test_changed_payload_onto_removed_row_re_adds(
        self, ledger, txn_a, make_txn
    ):
        ledger.upsert(TENANT_A, [txn_a])  # seq 1
        ledger.mark_removed(TENANT_A, [ID_A])  # seq 2
        changed = make_txn(id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000)
        events = ledger.upsert(TENANT_A, [changed])
        assert len(events) == 1
        assert events[0].kind is SyncEventKind.ADDED
        stored = ledger.get(TENANT_A, ID_A)
        assert stored.removed is False
        assert stored.last_modified_seq == 3
        assert stored.confirmed_at == MS + 24_000


class TestGet:
    def test_returns_the_stored_transaction(self, ledger, txn_a):
        ledger.upsert(TENANT_A, [txn_a])
        stored = ledger.get(TENANT_A, ID_A)
        assert stored.id == ID_A
        assert stored.tx_hash == "0xaaa"
        assert payload_equal(stored, txn_a) is True

    def test_unknown_id_raises_not_found(self, ledger):
        with pytest.raises(NotFoundError):
            ledger.get(TENANT_A, ID_A)


class TestMarkRemoved:
    def test_removed_event_state_and_seq_bump(self, ledger, txn_a):
        ledger.upsert(TENANT_A, [txn_a])  # seq 1
        events = ledger.mark_removed(TENANT_A, [ID_A])
        assert len(events) == 1
        assert events[0].kind is SyncEventKind.REMOVED
        assert events[0].transaction.id == ID_A
        stored = ledger.get(TENANT_A, ID_A)
        assert stored.removed is True
        assert stored.last_modified_seq == 2

    def test_sync_shows_one_removed_event_at_the_new_seq(self, ledger, txn_a):
        ledger.upsert(TENANT_A, [txn_a])
        ledger.mark_removed(TENANT_A, [ID_A])
        page = ledger.sync(TENANT_A)
        # State-based: ONE event per row, at its current seq — the old
        # ADDED at seq 1 no longer exists.
        assert len(page.events) == 1
        assert page.events[0].kind is SyncEventKind.REMOVED
        assert page.next_cursor == CURSOR_2

    def test_unknown_id_raises_not_found(self, ledger):
        with pytest.raises(NotFoundError):
            ledger.mark_removed(TENANT_A, [ID_A])

    def test_already_removed_is_a_silent_noop(self, ledger, txn_a):
        ledger.upsert(TENANT_A, [txn_a])
        ledger.mark_removed(TENANT_A, [ID_A])  # seq 2
        assert ledger.mark_removed(TENANT_A, [ID_A]) == []
        assert ledger.get(TENANT_A, ID_A).last_modified_seq == 2
        page = ledger.sync(TENANT_A, cursor=CURSOR_2)
        assert page.events == ()
        assert page.next_cursor == CURSOR_2

    def test_batch_removal_events_ascend(self, ledger, txn_a, txn_b):
        ledger.upsert(TENANT_A, [txn_a, txn_b])  # seqs 1, 2
        events = ledger.mark_removed(TENANT_A, [ID_A, ID_B])
        assert [e.kind for e in events] == [SyncEventKind.REMOVED] * 2
        assert [e.transaction.last_modified_seq for e in events] == [3, 4]


class TestSync:
    def test_empty_ledger_page(self, ledger):
        page = ledger.sync(TENANT_A)
        assert isinstance(page, SyncPage)
        assert page.events == ()
        assert page.next_cursor == CURSOR_0
        assert page.has_more is False

    @pytest.mark.parametrize(
        "bad", ["bogus", "42", "0000000000000000000", "-0000000000000000001"]
    )
    def test_malformed_cursor_raises_cursor_error(self, ledger, bad):
        with pytest.raises(CursorError):
            ledger.sync(TENANT_A, cursor=bad)

    def test_filter_is_strictly_greater_than_cursor(self, ledger, txn_a):
        ledger.upsert(TENANT_A, [txn_a])  # seq 1
        assert len(ledger.sync(TENANT_A, cursor=CURSOR_0).events) == 1
        assert ledger.sync(TENANT_A, cursor=CURSOR_1).events == ()

    def test_cursor_beyond_head_echoes_input(self, ledger, txn_a):
        ledger.upsert(TENANT_A, [txn_a])
        page = ledger.sync(TENANT_A, cursor=CURSOR_5)
        assert page.events == ()
        assert page.next_cursor == CURSOR_5
        assert page.has_more is False

    def test_modified_row_reappears_once_at_its_new_seq(
        self, ledger, txn_a, txn_b, make_txn
    ):
        # SPEC §6.4: last-modified order, NOT transaction date.
        ledger.upsert(TENANT_A, [txn_a, txn_b])  # A=1, B=2
        changed = make_txn(id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000)
        ledger.upsert(TENANT_A, [changed])  # A=3
        page = ledger.sync(TENANT_A)
        assert [e.transaction.id for e in page.events] == [ID_B, ID_A]
        assert [e.transaction.last_modified_seq for e in page.events] == [2, 3]
        assert page.next_cursor == CURSOR_3

    def test_pagination_5_events_limit_2_pages_2_2_1(self, ledger, five_txns):
        # Acceptance: pages of 2/2/1, has_more True/True/False, cursors
        # strictly increasing, no event lost or duplicated.
        ledger.upsert(TENANT_A, five_txns)  # seqs 1..5

        p1 = ledger.sync(TENANT_A, limit=2)
        assert len(p1.events) == 2
        assert p1.has_more is True
        assert p1.next_cursor == CURSOR_2

        p2 = ledger.sync(TENANT_A, cursor=p1.next_cursor, limit=2)
        assert len(p2.events) == 2
        assert p2.has_more is True
        assert p2.next_cursor == CURSOR_4

        p3 = ledger.sync(TENANT_A, cursor=p2.next_cursor, limit=2)
        assert len(p3.events) == 1
        assert p3.has_more is False
        assert p3.next_cursor == CURSOR_5

        assert CURSOR_0 < p1.next_cursor < p2.next_cursor < p3.next_cursor
        seen = [
            e.transaction.id for page in (p1, p2, p3) for e in page.events
        ]
        assert len(seen) == 5
        assert set(seen) == {ID_A, ID_B, ID_C, ID_D, ID_E}
        seqs = [
            e.transaction.last_modified_seq
            for page in (p1, p2, p3)
            for e in page.events
        ]
        assert seqs == [1, 2, 3, 4, 5]

    def test_has_more_false_when_limit_exactly_drains(self, ledger, txn_a, txn_b):
        ledger.upsert(TENANT_A, [txn_a, txn_b])
        page = ledger.sync(TENANT_A, limit=2)
        assert len(page.events) == 2
        assert page.has_more is False

    @pytest.mark.parametrize("limit", [0, -1])
    def test_limit_below_one_raises_validation_error(self, ledger, limit):
        # Reference-backend semantics: a page that can hold nothing can
        # never drain, so paging with it would loop forever.
        with pytest.raises(ValidationError):
            ledger.sync(TENANT_A, limit=limit)


class TestApplyReorg:
    @pytest.fixture
    def three(self, make_txn):
        """Three txns: orphaned (A), replaced-old (B), survivor (C).

        Seeding happens INSIDE each test body (never in a fixture) so a
        stub backend fails the test with NotImplementedError instead of
        erroring during setup.
        """
        return [
            make_txn(id=ID_A, tx_hash="0xaaa", block_number=FORK),
            make_txn(id=ID_B, tx_hash="0xbbb", block_number=FORK + 1),
            make_txn(id=ID_C, tx_hash="0xccc", block_number=FORK + 2),
        ]

    @pytest.fixture
    def replacement(self, make_txn):
        return make_txn(id=ID_BB2, tx_hash="0xbb2", block_number=FORK + 1)

    def test_composes_removed_then_added(self, ledger, three, replacement):
        ledger.upsert(TENANT_A, three)  # seqs 1, 2, 3
        plan = ReorgPlan(remove_ids=(ID_A, ID_B), add=(replacement,))
        events = ledger.apply_reorg(TENANT_A, plan)
        assert [e.kind for e in events] == [
            SyncEventKind.REMOVED,
            SyncEventKind.REMOVED,
            SyncEventKind.ADDED,
        ]
        assert [e.transaction.id for e in events] == [ID_A, ID_B, ID_BB2]
        assert [e.transaction.last_modified_seq for e in events] == [4, 5, 6]

    def test_survivor_is_untouched(self, ledger, three, replacement):
        ledger.upsert(TENANT_A, three)  # seqs 1, 2, 3
        ledger.apply_reorg(
            TENANT_A, ReorgPlan(remove_ids=(ID_A, ID_B), add=(replacement,))
        )
        survivor = ledger.get(TENANT_A, ID_C)
        assert survivor.removed is False
        assert survivor.last_modified_seq == 3

    def test_identical_add_is_a_noop(self, ledger, three):
        ledger.upsert(TENANT_A, three)  # seqs 1, 2, 3
        survivor_payload = three[2]
        events = ledger.apply_reorg(
            TENANT_A, ReorgPlan(remove_ids=(), add=(survivor_payload,))
        )
        assert events == []
        assert ledger.get(TENANT_A, ID_C).last_modified_seq == 3

    def test_spec_reorg_fixture_through_paged_sync(
        self, ledger, three, replacement
    ):
        # SPEC §13 contract: REMOVED for orphaned + replaced-old, ADDED
        # for the replacement, via sync() with a STRICTLY MONOTONIC
        # cursor across pages.
        ledger.upsert(TENANT_A, three)  # seqs 1, 2, 3
        pre_cursor = ledger.sync(TENANT_A).next_cursor
        assert pre_cursor == CURSOR_3

        canonical = [replacement, three[2]]  # survivor untouched
        plan = plan_reorg(three, canonical, from_block=FORK)
        ledger.apply_reorg(TENANT_A, plan)

        p1 = ledger.sync(TENANT_A, cursor=pre_cursor, limit=2)
        assert [e.kind for e in p1.events] == [SyncEventKind.REMOVED] * 2
        assert {e.transaction.id for e in p1.events} == {ID_A, ID_B}
        assert p1.has_more is True
        assert p1.next_cursor == CURSOR_5
        assert pre_cursor < p1.next_cursor

        p2 = ledger.sync(TENANT_A, cursor=p1.next_cursor, limit=2)
        assert [e.kind for e in p2.events] == [SyncEventKind.ADDED]
        assert p2.events[0].transaction.id == ID_BB2
        assert p2.has_more is False
        assert p2.next_cursor == CURSOR_6
        assert p1.next_cursor < p2.next_cursor

        # The removed rows stay visible as removed state, not resurrected.
        assert ledger.get(TENANT_A, ID_A).removed is True
        assert ledger.get(TENANT_A, ID_B).removed is True
        assert ledger.get(TENANT_A, ID_BB2).removed is False

    def test_duplicate_add_ids_raise_and_persist_nothing(
        self, ledger, three, make_txn
    ):
        # Atomicity: the duplicate check runs BEFORE mark_removed, so a
        # bad plan never leaves the tenant half-reorged.
        ledger.upsert(TENANT_A, three)  # seqs 1, 2, 3
        twin_a = make_txn(id=ID_BB2, tx_hash="0xbb2", block_number=FORK + 1)
        twin_b = make_txn(id=ID_BB2, tx_hash="0xbb2", block_number=FORK + 2)
        plan = ReorgPlan(remove_ids=(ID_A,), add=(twin_a, twin_b))
        with pytest.raises(ValidationError):
            ledger.apply_reorg(TENANT_A, plan)
        untouched = ledger.get(TENANT_A, ID_A)
        assert untouched.removed is False  # the remove never landed
        assert untouched.last_modified_seq == 1
        with pytest.raises(NotFoundError):
            ledger.get(TENANT_A, ID_BB2)  # the add never landed either


class TestTenantIsolation:
    """SPEC §13 attempted-leak contract (rule #6)."""

    def test_tenant_b_sync_sees_zero_of_tenant_a_events(self, ledger, txn_a):
        ledger.upsert(TENANT_A, [txn_a])
        page = ledger.sync(TENANT_B)
        assert page.events == ()
        assert page.next_cursor == CURSOR_0
        assert page.has_more is False

    def test_tenant_b_get_on_a_id_raises_not_found(self, ledger, txn_a):
        ledger.upsert(TENANT_A, [txn_a])
        with pytest.raises(NotFoundError):
            ledger.get(TENANT_B, ID_A)

    def test_tenant_b_mark_removed_raises_and_a_is_untouched(
        self, ledger, txn_a
    ):
        ledger.upsert(TENANT_A, [txn_a])
        with pytest.raises(NotFoundError):
            ledger.mark_removed(TENANT_B, [ID_A])
        stored = ledger.get(TENANT_A, ID_A)
        assert stored.removed is False
        assert stored.last_modified_seq == 1
        page = ledger.sync(TENANT_A)
        assert len(page.events) == 1
        assert page.events[0].kind is SyncEventKind.ADDED

    def test_seq_counters_are_per_tenant(self, ledger, txn_a, txn_b, make_txn):
        ledger.upsert(TENANT_A, [txn_a, txn_b])  # A at seqs 1, 2
        ledger.upsert(TENANT_B, [make_txn(id=ID_C, tx_hash="0xccc")])
        assert ledger.get(TENANT_B, ID_C).last_modified_seq == 1
        assert ledger.sync(TENANT_B).next_cursor == CURSOR_1

    def test_same_id_in_two_tenants_are_independent_rows(
        self, ledger, txn_a, make_txn
    ):
        ledger.upsert(TENANT_A, [txn_a])
        ledger.upsert(TENANT_B, [txn_a])
        changed = make_txn(id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000)
        ledger.upsert(TENANT_A, [changed])
        assert ledger.get(TENANT_A, ID_A).confirmed_at == MS + 24_000
        assert ledger.get(TENANT_B, ID_A).confirmed_at == MS + 12_000
        assert ledger.get(TENANT_B, ID_A).last_modified_seq == 1
