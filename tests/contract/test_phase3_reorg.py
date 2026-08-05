"""THE PHASE-3 GATE (SPEC §11 Phase 3; §6.4): a reorg fixture produces
``removed`` + re-``added`` — at the RICH level.

Composes the full pipeline end to end:

    decode_account -> ledger.bridge.to_ledger_transaction ->
    MemoryLedger.upsert -> sync -> ledger.reorg.plan_reorg ->
    apply_reorg -> sync

Golden transaction ids were derived INDEPENDENTLY via ``python3 -c`` over
the algorithm pinned in docs/internal/DECISIONS.md
(``"txn_" + sha256(f"{chain_id}|{tx_hash}|{account_id}").hexdigest()[:16]``):

    eip155:1 | 0x + 'bb'*32 | acct_1  ->  txn_e5e727672fb4ada6   (B)
    eip155:1 | 0x + 'cc'*32 | acct_1  ->  txn_557113c18fb02870   (C)

Cursor literals are ``f"{seq:020d}"`` (DECISIONS.md "Cursor token"),
hardcoded — never computed via ``encode_cursor``.

This file lives under tests/contract/ (mirror-exempt): each contract test
exists because the failure it guards has burned somebody (SPEC §13).
"""

from __future__ import annotations

from auradefi.decode.pipeline import decode_account
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.ledger.bridge import to_ledger_transaction
from auradefi.ledger.models import Direction, Entry, SyncEventKind, payload_equal
from auradefi.ledger.reorg import plan_reorg
from auradefi.money.quantity import Quantity
from auradefi.sources.evm.txlist import NormalTxRecord, TokenTxRecord

TENANT = "tenant-a"
CHAIN = "eip155:1"
ACCT_ID = "acct_1"
ACCOUNT = "0x" + "11" * 20
FROM_99 = "0x" + "99" * 20
TO_33 = "0x" + "33" * 20
CP_44 = "0x" + "44" * 20
TO_55 = "0x" + "55" * 20

USDC_CONTRACT = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
ETH = "eip155:1/slip44:60"
USDC = f"eip155:1/erc20:{USDC_CONTRACT}"

HASH_A = "0x" + "aa" * 32
HASH_B = "0x" + "bb" * 32
HASH_C = "0x" + "cc" * 32
HASH_D = "0x" + "dd" * 32

# Derived independently (see module docstring); NEVER regenerate.
TXN_A = "txn_f7e3f7aba9d6775a"
TXN_B = "txn_e5e727672fb4ada6"
TXN_C = "txn_557113c18fb02870"
TXN_D = "txn_a30f49051566e03d"
ALL_IDS = (TXN_A, TXN_B, TXN_C, TXN_D)

# f"{seq:020d}" — hardcoded per DECISIONS.md "Cursor token".
CURSOR_4 = "00000000000000000004"
CURSOR_6 = "00000000000000000006"
CURSOR_7 = "00000000000000000007"

GAS_PRICE = 10**10


def _normal(tx_hash, block, ts, sender, to, value, gas_used, is_error=False):
    return NormalTxRecord(
        tx_hash=tx_hash, block_number=block, time_stamp=ts,
        from_address=sender, to_address=to, value_wei=value,
        gas_used=gas_used, gas_price_wei=GAS_PRICE, is_error=is_error,
    )


def _token(tx_hash, block, ts, sender, to, value_raw, gas_used):
    return TokenTxRecord(
        tx_hash=tx_hash, block_number=block, time_stamp=ts,
        from_address=sender, to_address=to, contract_address=USDC_CONTRACT,
        value_raw=value_raw, token_decimal=6, token_symbol="USDC",
        gas_used=gas_used, gas_price_wei=GAS_PRICE,
    )


def _normal_b(block=101, ts=1_700_000_100):
    return _normal(HASH_B, block, ts, ACCOUNT, USDC_CONTRACT, 0, 50_000)


def _token_b(block=101, ts=1_700_000_100):
    return _token(HASH_B, block, ts, ACCOUNT, TO_33, 25_000_000, 50_000)


def _normal_c(block=102, ts=1_700_000_200):
    return _normal(HASH_C, block, ts, ACCOUNT, CP_44, 10**18, 120_000)


def _token_c(block=102, ts=1_700_000_200):
    return _token(HASH_C, block, ts, CP_44, ACCOUNT, 3_000_000_000, 120_000)


def _bridged_fixture():
    """decode [A, B, C, D] -> bridge each to a LedgerTransaction."""
    normal = [
        _normal(HASH_A, 100, 1_700_000_000, FROM_99, ACCOUNT, 10**18, 21_000),
        _normal_b(),
        _normal_c(),
        _normal(
            HASH_D, 103, 1_700_000_300, ACCOUNT, TO_55, 5 * 10**17, 21_000,
            is_error=True,
        ),
    ]
    tokens = [_token_b(), _token_c()]
    rich = decode_account(CHAIN, ACCT_ID, ACCOUNT, normal, tokens)
    return [to_ledger_transaction(txn) for txn in rich]


def _bridge_one(normal, tokens):
    (rich,) = decode_account(CHAIN, ACCT_ID, ACCOUNT, normal, tokens)
    return to_ledger_transaction(rich)


def _b_prime():
    """B re-decoded after being re-mined at block 105 (ts 1700000500)."""
    return _bridge_one([_normal_b(105, 1_700_000_500)], [_token_b(105, 1_700_000_500)])


def _c_unchanged():
    """C re-decoded after resurfacing at its ORIGINAL block 102.

    Byte-identical to the stored C payload on purpose: the resurrection
    branch is only reached when NOTHING about the payload changed. A
    fixture that moved the block would route through the payload-changed
    path and never test resurrection at all (RELEASE_0.1.1 §5 #22).
    """
    return _bridge_one([_normal_c()], [_token_c()])


def _seeded():
    """Fresh ledger with the decoded fixture upserted under TENANT."""
    ledger = MemoryLedger()
    events = ledger.upsert(TENANT, _bridged_fixture())
    return ledger, events


def _reorged():
    """Seeded ledger after the reorg: C orphaned, B re-mined at 105."""
    ledger, _ = _seeded()
    stored = [ledger.get(TENANT, txn_id) for txn_id in ALL_IDS]
    bridged_d = _bridged_fixture()[3]
    plan = plan_reorg(stored, [_b_prime(), bridged_d], from_block=101)
    events = ledger.apply_reorg(TENANT, plan)
    return ledger, plan, events


def test_gate_upsert_emits_four_added_with_seqs_1_to_4():
    _, events = _seeded()
    assert [e.kind for e in events] == [SyncEventKind.ADDED] * 4
    assert [e.transaction.id for e in events] == list(ALL_IDS)
    assert [e.transaction.last_modified_seq for e in events] == [1, 2, 3, 4]


def test_gate_initial_sync_drains_to_cursor_4():
    ledger, _ = _seeded()
    page = ledger.sync(TENANT, None)
    assert [e.transaction.id for e in page.events] == list(ALL_IDS)
    assert all(e.kind is SyncEventKind.ADDED for e in page.events)
    assert page.next_cursor == CURSOR_4
    assert page.has_more is False


def test_gate_bridged_entries_carry_movements_never_fees():
    ledger, _ = _seeded()
    txn_a = ledger.get(TENANT, TXN_A)
    assert txn_a.entries == (
        Entry(asset_id=ETH, quantity=Quantity(10**18, 18), direction=Direction.IN),
    )
    txn_c = ledger.get(TENANT, TXN_C)
    assert [(e.direction, e.asset_id) for e in txn_c.entries] == [
        (Direction.OUT, ETH),
        (Direction.IN, USDC),
    ]
    # D failed: fee survives at the rich level but NEVER becomes an entry.
    assert ledger.get(TENANT, TXN_D).entries == ()


def test_gate_plan_reorg_orphans_c_and_readds_b():
    ledger, _ = _seeded()
    stored = [ledger.get(TENANT, txn_id) for txn_id in ALL_IDS]
    plan = plan_reorg(stored, [_b_prime(), _bridged_fixture()[3]], from_block=101)
    assert plan.remove_ids == (TXN_C,)
    assert plan.add == (_b_prime(),)


def test_gate_apply_reorg_emits_removed_then_readded():
    _, _, events = _reorged()
    assert [(e.kind, e.transaction.id) for e in events] == [
        (SyncEventKind.REMOVED, TXN_C),
        (SyncEventKind.ADDED, TXN_B),
    ]
    assert events[0].transaction.removed is True
    assert events[1].transaction.removed is False
    assert events[1].transaction.block_number == 105
    assert events[1].transaction.initiated_at == 1_700_000_500_000
    assert [e.transaction.last_modified_seq for e in events] == [5, 6]


def test_gate_sync_after_reorg_returns_exactly_removed_plus_added():
    ledger, _, _ = _reorged()
    page = ledger.sync(TENANT, CURSOR_4)
    assert [(e.kind, e.transaction.id) for e in page.events] == [
        (SyncEventKind.REMOVED, TXN_C),
        (SyncEventKind.ADDED, TXN_B),
    ]
    assert page.events[1].transaction.block_number == 105
    assert page.next_cursor == CURSOR_6
    assert page.has_more is False


def test_gate_resurrection_re_add_of_c():
    # pins: C, orphaned by the reorg and now canonical again with a
    #       BYTE-IDENTICAL payload, is planned as a re-add and reaches the
    #       client as ADDED — never stranded at removed=True forever.
    ledger, _, _ = _reorged()
    assert ledger.get(TENANT, TXN_C).removed is True

    c_back = _c_unchanged()
    stored = [ledger.get(TENANT, txn_id) for txn_id in ALL_IDS]
    # Guard the fixture: if this were False the row would route through the
    # payload-changed bucket and the resurrection branch would go untested.
    assert payload_equal(c_back, ledger.get(TENANT, TXN_C))

    plan = plan_reorg(
        stored, [_b_prime(), c_back, _bridged_fixture()[3]], from_block=101
    )
    assert plan.remove_ids == ()
    assert plan.add == (c_back,)

    events = ledger.apply_reorg(TENANT, plan)
    assert [(e.kind, e.transaction.id) for e in events] == [
        (SyncEventKind.ADDED, TXN_C),
    ]
    assert events[0].transaction.removed is False
    assert events[0].transaction.block_number == 102

    page = ledger.sync(TENANT, CURSOR_6)
    assert [(e.kind, e.transaction.id) for e in page.events] == [
        (SyncEventKind.ADDED, TXN_C),
    ]
    assert page.events[0].transaction.block_number == 102
    assert page.next_cursor == CURSOR_7
    assert page.has_more is False


def test_gate_cursors_strictly_increase_across_the_whole_story():
    ledger, _ = _seeded()
    first = ledger.sync(TENANT, None).next_cursor  # drains the initial 4
    stored = [ledger.get(TENANT, txn_id) for txn_id in ALL_IDS]
    plan = plan_reorg(stored, [_b_prime(), _bridged_fixture()[3]], from_block=101)
    ledger.apply_reorg(TENANT, plan)
    second = ledger.sync(TENANT, first).next_cursor  # drains the reorg pair
    ledger.upsert(TENANT, [_c_unchanged()])
    third = ledger.sync(TENANT, second).next_cursor  # drains the resurrection
    assert (first, second, third) == (CURSOR_4, CURSOR_6, CURSOR_7)
    assert first < second < third  # lexicographic == numeric (pinned token)
