"""Contract tests for auradefi.ledger.reorg (SPEC §6.4, §13).

A chain reorg is removed + re-added, first-class. ``plan_reorg`` is the
pure half: stored txns at/above the fork point that vanished from the
canonical view are removed; canonical txns that are new or payload-
changed are (re-)added; identical survivors are untouched.

Transaction ids derived independently via ``python3 -c`` over the
DECISIONS algorithm (chain eip155:1, acct_1) — never from the code
under test.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from auradefi.ledger.reorg import ReorgPlan, plan_reorg

# Derived independently; NEVER regenerate from the implementation.
ID_ORPHANED = "txn_fb618872cdc184c0"  # tx_hash 0xaaa
ID_REPLACED = "txn_9d8e7888ce01e8a5"  # tx_hash 0xbbb
ID_REPLACEMENT = "txn_6b20cedf697f79fb"  # tx_hash 0xbb2
ID_SURVIVOR = "txn_07c85f8766037afc"  # tx_hash 0xccc

FORK = 19_000_000
MS = 1_754_000_000_000


@pytest.fixture
def orphaned(make_txn):
    """Stored at the fork block; simply gone from the canonical chain."""
    return make_txn(id=ID_ORPHANED, tx_hash="0xaaa", block_number=FORK)


@pytest.fixture
def replaced_old(make_txn):
    """Stored above the fork; its slot went to a different transaction."""
    return make_txn(id=ID_REPLACED, tx_hash="0xbbb", block_number=FORK + 1)


@pytest.fixture
def replacement(make_txn):
    """The canonical transaction that took the replaced slot (new id)."""
    return make_txn(id=ID_REPLACEMENT, tx_hash="0xbb2", block_number=FORK + 1)


@pytest.fixture
def survivor(make_txn):
    """Stored above the fork and identical in the canonical view."""
    return make_txn(id=ID_SURVIVOR, tx_hash="0xccc", block_number=FORK + 2)


class TestReorgPlanShape:
    def test_frozen(self):
        plan = ReorgPlan(remove_ids=(), add=())
        with pytest.raises(FrozenInstanceError):
            plan.remove_ids = (ID_ORPHANED,)

    def test_fields(self, replacement):
        plan = ReorgPlan(remove_ids=(ID_ORPHANED,), add=(replacement,))
        assert plan.remove_ids == (ID_ORPHANED,)
        assert plan.add == (replacement,)


class TestSpecFixture:
    """SPEC §13: one orphaned, one replaced, one surviving."""

    def test_orphaned_and_replaced_old_removed_replacement_added(
        self, orphaned, replaced_old, replacement, survivor
    ):
        plan = plan_reorg(
            existing_for_chain=[orphaned, replaced_old, survivor],
            canonical=[replacement, survivor],
            from_block=FORK,
        )
        assert set(plan.remove_ids) == {ID_ORPHANED, ID_REPLACED}
        assert len(plan.remove_ids) == 2  # no duplicates
        assert plan.add == (replacement,)

    def test_identical_survivor_appears_in_neither_bucket(
        self, orphaned, survivor
    ):
        plan = plan_reorg([orphaned, survivor], [survivor], FORK)
        assert plan.remove_ids == (ID_ORPHANED,)
        assert ID_SURVIVOR not in plan.remove_ids
        assert plan.add == ()

    def test_no_divergence_yields_the_empty_plan(self, survivor, replaced_old):
        plan = plan_reorg(
            [replaced_old, survivor], [replaced_old, survivor], FORK
        )
        assert plan == ReorgPlan(remove_ids=(), add=())


class TestRemoveSide:
    def test_stored_below_fork_is_never_removed(self, make_txn, survivor):
        below = make_txn(
            id=ID_ORPHANED, tx_hash="0xaaa", block_number=FORK - 1
        )
        plan = plan_reorg([below, survivor], [survivor], FORK)
        assert plan.remove_ids == ()

    def test_stored_exactly_at_fork_is_removed(self, orphaned):
        # block_number >= from_block: the boundary is inclusive.
        plan = plan_reorg([orphaned], [], FORK)
        assert plan.remove_ids == (ID_ORPHANED,)

    def test_pending_stored_txn_is_never_removed(self, make_txn):
        pending = make_txn(
            id=ID_ORPHANED,
            tx_hash="0xaaa",
            block_number=None,
            confirmed_at=None,
        )
        plan = plan_reorg([pending], [], FORK)
        assert plan.remove_ids == ()

    def test_empty_canonical_removes_everything_at_or_above_fork(
        self, orphaned, replaced_old, survivor
    ):
        plan = plan_reorg([orphaned, replaced_old, survivor], [], FORK)
        assert set(plan.remove_ids) == {ID_ORPHANED, ID_REPLACED, ID_SURVIVOR}
        assert plan.add == ()


class TestAddSide:
    def test_brand_new_canonical_txn_is_added(self, replacement):
        plan = plan_reorg([], [replacement], FORK)
        assert plan == ReorgPlan(remove_ids=(), add=(replacement,))

    def test_same_id_different_payload_is_readded_not_removed(
        self, replaced_old, make_txn
    ):
        # The same tx re-mined one block higher: id unchanged (id hashes
        # chain|tx_hash|account, not the block), payload differs.
        remined = make_txn(
            id=ID_REPLACED, tx_hash="0xbbb", block_number=FORK + 2
        )
        plan = plan_reorg([replaced_old], [remined], FORK)
        assert plan.remove_ids == ()
        assert plan.add == (remined,)

    def test_bookkeeping_only_difference_is_not_readded(
        self, replaced_old, make_txn
    ):
        # payload_equal ignores last_modified_seq: a canonical twin
        # differing only in bookkeeping is an identical survivor.
        twin = make_txn(
            id=ID_REPLACED,
            tx_hash="0xbbb",
            block_number=FORK + 1,
            last_modified_seq=7,
        )
        plan = plan_reorg([replaced_old], [twin], FORK)
        assert plan.add == ()
        assert plan.remove_ids == ()

    def test_confirmed_at_shift_is_readded(self, survivor, make_txn):
        shifted = make_txn(
            id=ID_SURVIVOR,
            tx_hash="0xccc",
            block_number=FORK + 2,
            confirmed_at=MS + 24_000,
        )
        plan = plan_reorg([survivor], [shifted], FORK)
        assert plan.add == (shifted,)
        assert plan.remove_ids == ()


class TestPurity:
    def test_inputs_are_not_mutated(self, orphaned, replacement, survivor):
        existing = [orphaned, survivor]
        canonical = [replacement, survivor]
        plan_reorg(existing, canonical, FORK)
        assert existing == [orphaned, survivor]
        assert canonical == [replacement, survivor]

    def test_pure_same_inputs_same_plan(
        self, orphaned, replaced_old, replacement, survivor
    ):
        existing = [orphaned, replaced_old, survivor]
        canonical = [replacement, survivor]
        assert plan_reorg(existing, canonical, FORK) == plan_reorg(
            existing, canonical, FORK
        )

    def test_result_buckets_are_tuples(self, orphaned, replacement):
        plan = plan_reorg([orphaned], [replacement], FORK)
        assert isinstance(plan.remove_ids, tuple)
        assert isinstance(plan.add, tuple)
