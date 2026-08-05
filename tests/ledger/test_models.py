"""Golden vectors and invariants for auradefi.ledger.models (SPEC §4.4).

The transaction-id literals below were derived INDEPENDENTLY of the code
under test, via ``python3 -c`` over the algorithm pinned in
docs/internal/DECISIONS.md:

    "txn_" + sha256(f"{chain_id}|{tx_hash}|{account_id}".encode()).hexdigest()[:16]

A stability contract is a hardcoded string, not a call to the function
under test.
"""

from __future__ import annotations

import dataclasses
from dataclasses import FrozenInstanceError

import pytest

from auradefi.ledger.models import (
    Direction,
    Entry,
    LedgerTransaction,
    SyncEvent,
    SyncEventKind,
    SyncPage,
    payload_equal,
    transaction_id,
)
from auradefi.money.quantity import Quantity

# Derived independently (see module docstring); NEVER regenerate from the
# implementation.
GOLDEN_ID_ACCT_1 = "txn_8960436486a11960"  # eip155:1 | 0xabc | acct_1
GOLDEN_ID_ACCT_2 = "txn_96e39b11221dd121"  # eip155:1 | 0xabc | acct_2
GOLDEN_ID_CHAIN_137 = "txn_29df63af5ae2a213"  # eip155:137 | 0xabc | acct_1
GOLDEN_ID_HASH_DEF = "txn_728c1582b3e16304"  # eip155:1 | 0xdef | acct_1

MS = 1_754_000_000_000  # ms-epoch, matches the repo's frozen clock era


def make_entry(**overrides) -> Entry:
    fields = {
        "asset_id": "eip155:1/slip44:60",
        "quantity": Quantity(15 * 10**17, 18),
        "direction": Direction.IN,
    }
    fields.update(overrides)
    return Entry(**fields)


def make_txn(**overrides) -> LedgerTransaction:
    """Local factory (duplicated per test module: tests/ledger/conftest.py
    is outside this order's ownership — see the work-order report)."""
    fields = {
        "id": GOLDEN_ID_ACCT_1,
        "chain_id": "eip155:1",
        "tx_hash": "0xabc",
        "account_id": "acct_1",
        "block_number": 19_000_000,
        "initiated_at": MS,
        "confirmed_at": MS + 12_000,
        "entries": (make_entry(),),
    }
    fields.update(overrides)
    return LedgerTransaction(**fields)


class TestTransactionId:
    def test_pinned_golden_vector(self):
        assert transaction_id("eip155:1", "0xabc", "acct_1") == GOLDEN_ID_ACCT_1

    def test_deterministic_across_calls(self):
        first = transaction_id("eip155:1", "0xabc", "acct_1")
        second = transaction_id("eip155:1", "0xabc", "acct_1")
        assert first == second == GOLDEN_ID_ACCT_1

    def test_different_account_id_gives_different_id(self):
        other = transaction_id("eip155:1", "0xabc", "acct_2")
        assert other == GOLDEN_ID_ACCT_2
        assert other != GOLDEN_ID_ACCT_1

    def test_every_component_is_identity_bearing(self):
        assert transaction_id("eip155:137", "0xabc", "acct_1") == GOLDEN_ID_CHAIN_137
        assert transaction_id("eip155:1", "0xdef", "acct_1") == GOLDEN_ID_HASH_DEF
        ids = {
            GOLDEN_ID_ACCT_1,
            GOLDEN_ID_ACCT_2,
            GOLDEN_ID_CHAIN_137,
            GOLDEN_ID_HASH_DEF,
        }
        assert len(ids) == 4

    def test_shape_is_txn_plus_16_hex_chars(self):
        txn_id = transaction_id("eip155:1", "0xabc", "acct_1")
        assert txn_id.startswith("txn_")
        suffix = txn_id.removeprefix("txn_")
        assert len(suffix) == 16
        assert set(suffix) <= set("0123456789abcdef")


class TestPayloadEqual:
    def test_identical_payloads_are_equal(self):
        assert payload_equal(make_txn(), make_txn()) is True

    def test_ignores_last_modified_seq(self):
        assert payload_equal(make_txn(), make_txn(last_modified_seq=42)) is True

    def test_ignores_removed(self):
        assert payload_equal(make_txn(), make_txn(removed=True)) is True

    def test_ignores_both_bookkeeping_fields_together(self):
        a = make_txn(removed=False, last_modified_seq=1)
        b = make_txn(removed=True, last_modified_seq=999)
        assert payload_equal(a, b) is True

    def test_catches_confirmed_at_change(self):
        assert payload_equal(make_txn(), make_txn(confirmed_at=MS + 13_000)) is False
        assert payload_equal(make_txn(), make_txn(confirmed_at=None)) is False

    def test_catches_entries_change(self):
        changed = make_txn(
            entries=(make_entry(quantity=Quantity(16 * 10**17, 18)),)
        )
        assert payload_equal(make_txn(), changed) is False

    def test_catches_entry_direction_change(self):
        changed = make_txn(entries=(make_entry(direction=Direction.OUT),))
        assert payload_equal(make_txn(), changed) is False

    def test_catches_block_number_change(self):
        assert payload_equal(make_txn(), make_txn(block_number=19_000_001)) is False


class TestEnums:
    def test_direction_members_and_wire_values(self):
        assert Direction.IN.value == "in"
        assert Direction.OUT.value == "out"
        assert Direction.SELF.value == "self"
        assert len(Direction) == 3

    def test_direction_is_str(self):
        assert Direction.IN == "in"
        assert isinstance(Direction.OUT, str)

    def test_sync_event_kind_members_and_wire_values(self):
        assert SyncEventKind.ADDED.value == "added"
        assert SyncEventKind.REMOVED.value == "removed"
        assert len(SyncEventKind) == 2
        assert SyncEventKind.REMOVED == "removed"


class TestImmutability:
    def test_entry_is_frozen(self):
        entry = make_entry()
        with pytest.raises(FrozenInstanceError):
            entry.asset_id = "eip155:1/erc20:0xdead"

    def test_ledger_transaction_is_frozen(self):
        txn = make_txn()
        with pytest.raises(FrozenInstanceError):
            txn.removed = True

    def test_sync_event_is_frozen(self):
        event = SyncEvent(kind=SyncEventKind.ADDED, transaction=make_txn())
        with pytest.raises(FrozenInstanceError):
            event.kind = SyncEventKind.REMOVED

    def test_sync_page_is_frozen(self):
        page = SyncPage(events=(), next_cursor="0" * 20, has_more=False)
        with pytest.raises(FrozenInstanceError):
            page.has_more = True


class TestDefaultsAndShape:
    def test_bookkeeping_defaults(self):
        txn = make_txn()
        assert txn.removed is False
        assert txn.last_modified_seq == 0

    def test_timestamps_are_ms_epoch_ints(self):
        txn = make_txn()
        assert isinstance(txn.initiated_at, int)
        assert isinstance(txn.confirmed_at, int)
        assert txn.initiated_at == MS

    def test_entries_field_holds_a_tuple(self):
        assert isinstance(make_txn().entries, tuple)

    def test_sync_event_fields(self):
        txn = make_txn()
        event = SyncEvent(kind=SyncEventKind.REMOVED, transaction=txn)
        assert event.kind is SyncEventKind.REMOVED
        assert event.transaction is txn

    def test_all_model_classes_are_dataclasses(self):
        for cls in (Entry, LedgerTransaction, SyncEvent, SyncPage):
            assert dataclasses.is_dataclass(cls)
