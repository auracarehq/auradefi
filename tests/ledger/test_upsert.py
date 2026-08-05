"""Contract tests for auradefi.ledger.upsert (SPEC §6.4 sync semantics).

``classify`` is pure diffing over ``payload_equal``: bookkeeping fields
(``last_modified_seq``, ``removed``) must never make a re-delivered
transaction look changed. Transaction-id literals reuse the golden
vectors pinned in test_models.py / docs/internal/DECISIONS.md, derived
independently via ``python3 -c`` — never from the code under test.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from auradefi.errors import ValidationError
from auradefi.ledger.models import Direction
from auradefi.ledger.upsert import UpsertPlan, classify
from auradefi.money.quantity import Quantity

# Derived independently (python3 -c over the DECISIONS algorithm);
# NEVER regenerate from the implementation. chain eip155:1, acct_1.
ID_A = "txn_fb618872cdc184c0"  # tx_hash 0xaaa
ID_B = "txn_9d8e7888ce01e8a5"  # tx_hash 0xbbb
ID_C = "txn_07c85f8766037afc"  # tx_hash 0xccc

MS = 1_754_000_000_000


@pytest.fixture
def txn_a(make_txn):
    return make_txn(id=ID_A, tx_hash="0xaaa")


@pytest.fixture
def txn_b(make_txn):
    return make_txn(id=ID_B, tx_hash="0xbbb")


@pytest.fixture
def txn_c(make_txn):
    return make_txn(id=ID_C, tx_hash="0xccc")


class TestUpsertPlanShape:
    def test_frozen(self):
        plan = UpsertPlan(new=(), changed=(), unchanged=())
        with pytest.raises(FrozenInstanceError):
            plan.new = ()

    def test_fields(self, make_txn):
        txn = make_txn()
        plan = UpsertPlan(new=(txn,), changed=(), unchanged=(ID_A,))
        assert plan.new == (txn,)
        assert plan.changed == ()
        assert plan.unchanged == (ID_A,)


class TestClassifyBuckets:
    def test_empty_in_empty_out(self):
        plan = classify([], {})
        assert plan == UpsertPlan(new=(), changed=(), unchanged=())

    def test_all_new_when_store_is_empty(self, txn_a, txn_b):
        plan = classify([txn_a, txn_b], {})
        assert plan.new == (txn_a, txn_b)  # incoming order preserved
        assert plan.changed == ()
        assert plan.unchanged == ()

    def test_payload_identical_is_unchanged_by_id_only(self, txn_a, make_txn):
        stored = make_txn(id=ID_A, tx_hash="0xaaa")
        plan = classify([txn_a], {ID_A: stored})
        assert plan.unchanged == (ID_A,)  # the id string, not the txn
        assert plan.new == ()
        assert plan.changed == ()

    def test_payload_difference_is_changed(self, txn_a, make_txn):
        stored = make_txn(id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000)
        plan = classify([txn_a], {ID_A: stored})
        assert plan.changed == (txn_a,)
        assert plan.new == ()
        assert plan.unchanged == ()

    def test_entry_change_is_changed(self, txn_a, make_txn, make_entry):
        stored = make_txn(
            id=ID_A,
            tx_hash="0xaaa",
            entries=(make_entry(quantity=Quantity(16 * 10**17, 18)),),
        )
        plan = classify([txn_a], {ID_A: stored})
        assert plan.changed == (txn_a,)

    def test_bookkeeping_only_difference_is_unchanged(self, txn_a, make_txn):
        # payload_equal semantics: seq/removed never count as change.
        stored = make_txn(
            id=ID_A, tx_hash="0xaaa", removed=True, last_modified_seq=42
        )
        plan = classify([txn_a], {ID_A: stored})
        assert plan.unchanged == (ID_A,)
        assert plan.changed == ()

    def test_incoming_bookkeeping_is_ignored_too(self, make_txn):
        incoming = make_txn(id=ID_A, tx_hash="0xaaa", last_modified_seq=999)
        stored = make_txn(id=ID_A, tx_hash="0xaaa")
        plan = classify([incoming], {ID_A: stored})
        assert plan.unchanged == (ID_A,)

    def test_mixed_batch_lands_in_all_three_buckets(
        self, txn_a, txn_b, txn_c, make_txn
    ):
        existing = {
            ID_B: make_txn(id=ID_B, tx_hash="0xbbb", block_number=19_000_007),
            ID_C: make_txn(id=ID_C, tx_hash="0xccc"),
        }
        plan = classify([txn_a, txn_b, txn_c], existing)
        assert plan.new == (txn_a,)
        assert plan.changed == (txn_b,)
        assert plan.unchanged == (ID_C,)

    def test_order_preserved_within_buckets(self, make_txn):
        first = make_txn(id=ID_C, tx_hash="0xccc")
        second = make_txn(id=ID_A, tx_hash="0xaaa")
        third = make_txn(id=ID_B, tx_hash="0xbbb")
        plan = classify([first, second, third], {})
        assert plan.new == (first, second, third)

    def test_extra_stored_rows_are_not_reported(self, txn_a, txn_b):
        # classify diffs the INCOMING batch; stored-only rows are the
        # reorg planner's business, never upsert's.
        plan = classify([txn_a], {ID_A: txn_a, ID_B: txn_b})
        assert plan == UpsertPlan(new=(), changed=(), unchanged=(ID_A,))


class TestClassifyValidation:
    def test_duplicate_ids_within_incoming_raise(self, txn_a, make_txn):
        redelivered = make_txn(
            id=ID_A, tx_hash="0xaaa", confirmed_at=MS + 24_000
        )
        with pytest.raises(ValidationError):
            classify([txn_a, redelivered], {})

    def test_identical_duplicates_still_raise(self, txn_a, make_txn):
        with pytest.raises(ValidationError):
            classify([txn_a, make_txn(id=ID_A, tx_hash="0xaaa")], {})


class TestClassifyPurity:
    def test_inputs_are_not_mutated(self, txn_a, txn_b, make_txn):
        incoming = [txn_a, make_txn(id=ID_B, tx_hash="0xbbb", block_number=1)]
        existing = {ID_B: txn_b}
        classify(incoming, existing)
        assert incoming == [
            txn_a,
            make_txn(id=ID_B, tx_hash="0xbbb", block_number=1),
        ]
        assert existing == {ID_B: txn_b}

    def test_pure_same_inputs_same_plan(self, txn_a, txn_b, txn_c, make_txn):
        existing = {
            ID_B: make_txn(id=ID_B, tx_hash="0xbbb", block_number=1),
            ID_C: txn_c,
        }
        incoming = [txn_a, txn_b, txn_c]
        assert classify(incoming, existing) == classify(incoming, existing)

    def test_result_buckets_are_tuples(self, txn_a):
        plan = classify([txn_a], {})
        assert isinstance(plan.new, tuple)
        assert isinstance(plan.changed, tuple)
        assert isinstance(plan.unchanged, tuple)

    def test_direction_enum_roundtrips_through_classify(self, make_txn, make_entry):
        # Guard against normalisation: the txn objects come back as-is.
        txn = make_txn(entries=(make_entry(direction=Direction.OUT),))
        plan = classify([txn], {})
        assert plan.new[0] is txn
