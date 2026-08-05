"""Contract tests for auradefi.ledger.backends.models (SPEC §8, rule #2).

Table shape: ``auradefi_``-prefixed names (they land in the HOST's
database), composite PK (tenant_id, id), the (tenant_id,
last_modified_seq) sync index, and an exposed ``metadata`` the HOST runs
create_all/migrations against. The library never emits DDL.

Wire goldens: entries encode to canonical JSON with ``raw`` as a
decimal-int JSON STRING (rule #2). Every literal below was derived
independently via ``python3 -c`` (json.dumps with sort_keys=True,
separators=(',',':')), never from the code under test.
"""

from __future__ import annotations

import pytest

from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from auradefi.ledger.backends.models import (
    LedgerTransactionRow,
    TenantSeqRow,
    decode_entries,
    encode_entries,
    metadata,
    row_to_transaction,
    transaction_to_row,
)
from auradefi.ledger.models import Direction, Entry
from auradefi.money.quantity import Quantity

MS = 1_754_000_000_000

# eip155:1 | 0xabc | acct_1 through the pinned id algorithm: derived
# independently via python3 -c; NEVER regenerate from the implementation.
ID_DEFAULT = "txn_8960436486a11960"

TXN_TABLE = "auradefi_ledger_transactions"
SEQ_TABLE = "auradefi_ledger_seqs"

# json.dumps(list-of-objects, sort_keys=True, separators=(",", ":")), 
# raw is a JSON STRING (rule #2). Derived independently via python3 -c.
GOLDEN_ONE = (
    '[{"asset_id":"eip155:1/slip44:60","decimals":18,'
    '"direction":"in","raw":"1500000000000000000"}]'
)
GOLDEN_TWO = (
    '[{"asset_id":"eip155:1/slip44:60","decimals":18,'
    '"direction":"in","raw":"1500000000000000000"},'
    '{"asset_id":"eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",'
    '"decimals":6,"direction":"out","raw":"2500000"}]'
)
# 78-digit raw (10**77): rule #2's whole point: no float ever survives this.
GOLDEN_HUGE = (
    '[{"asset_id":"eip155:1/slip44:60","decimals":18,"direction":"in",'
    '"raw":"1000000000000000000000000000000000000000000'
    '00000000000000000000000000000000000"}]'
)
GOLDEN_NEGATIVE = (
    '[{"asset_id":"eip155:1/slip44:60","decimals":0,'
    '"direction":"out","raw":"-7"}]'
)

USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def _memory_engine():
    """Fresh in-memory sqlite engine; one shared connection (StaticPool)."""
    return create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


class TestLedgerTransactionRowTable:
    def test_tablename_is_host_prefixed(self):
        assert LedgerTransactionRow.__tablename__ == TXN_TABLE

    def test_registered_on_the_exposed_metadata(self):
        assert TXN_TABLE in metadata.tables

    def test_composite_primary_key_is_tenant_id_and_id(self):
        table = metadata.tables[TXN_TABLE]
        assert {c.name for c in table.primary_key.columns} == {
            "tenant_id",
            "id",
        }
        assert table.c["tenant_id"].primary_key is True
        assert table.c["id"].primary_key is True

    def test_columns_are_exactly_the_contract_set(self):
        table = metadata.tables[TXN_TABLE]
        assert set(table.c.keys()) == {
            "tenant_id",
            "id",
            "chain_id",
            "tx_hash",
            "account_id",
            "block_number",
            "initiated_at",
            "confirmed_at",
            "entries_json",
            "removed",
            "last_modified_seq",
        }

    @pytest.mark.parametrize("column", ["block_number", "confirmed_at"])
    def test_optional_columns_are_nullable(self, column):
        assert metadata.tables[TXN_TABLE].c[column].nullable is True

    @pytest.mark.parametrize(
        "column",
        [
            "tenant_id",
            "id",
            "chain_id",
            "tx_hash",
            "account_id",
            "initiated_at",
            "entries_json",
            "removed",
            "last_modified_seq",
        ],
    )
    def test_required_columns_are_not_nullable(self, column):
        assert metadata.tables[TXN_TABLE].c[column].nullable is False

    def test_tenant_seq_sync_index_exists(self):
        # sync filters and orders on (tenant_id, last_modified_seq).
        table = metadata.tables[TXN_TABLE]
        index_column_sets = [
            tuple(column.name for column in index.columns)
            for index in table.indexes
        ]
        assert ("tenant_id", "last_modified_seq") in index_column_sets


class TestTenantSeqRowTable:
    def test_tablename_is_host_prefixed(self):
        assert TenantSeqRow.__tablename__ == SEQ_TABLE

    def test_registered_on_the_exposed_metadata(self):
        assert SEQ_TABLE in metadata.tables

    def test_primary_key_is_tenant_id_alone(self):
        table = metadata.tables[SEQ_TABLE]
        assert {c.name for c in table.primary_key.columns} == {"tenant_id"}

    def test_columns_are_tenant_id_and_seq(self):
        table = metadata.tables[SEQ_TABLE]
        assert set(table.c.keys()) == {"tenant_id", "seq"}
        assert table.c["seq"].nullable is False


class TestMetadataBelongsToTheHost:
    def test_host_runs_ddl_against_the_exposed_metadata(self):
        # SPEC §8: the HOST creates the schema; importing models never does.
        engine = _memory_engine()
        assert inspect(engine).get_table_names() == []
        metadata.create_all(engine)
        assert {TXN_TABLE, SEQ_TABLE} <= set(inspect(engine).get_table_names())


class TestEncodeEntries:
    def test_single_entry_golden_string(self, make_entry):
        # Acceptance golden: raw is a JSON STRING, keys sorted, compact.
        assert encode_entries((make_entry(),)) == GOLDEN_ONE

    def test_two_entries_preserve_order_golden_string(self, make_entry):
        entries = (
            make_entry(),
            make_entry(
                asset_id=USDC,
                quantity=Quantity(25 * 10**5, 6),
                direction=Direction.OUT,
            ),
        )
        assert encode_entries(entries) == GOLDEN_TWO

    def test_78_digit_raw_golden_string(self, make_entry):
        entry = make_entry(quantity=Quantity(10**77, 18))
        assert encode_entries((entry,)) == GOLDEN_HUGE

    def test_negative_raw_golden_string(self, make_entry):
        entry = make_entry(
            quantity=Quantity(-7, 0), direction=Direction.OUT
        )
        assert encode_entries((entry,)) == GOLDEN_NEGATIVE

    def test_empty_entries_encode_to_empty_json_list(self):
        assert encode_entries(()) == "[]"


class TestDecodeEntries:
    def test_golden_string_decodes_to_the_exact_entry(self):
        decoded = decode_entries(GOLDEN_ONE)
        assert isinstance(decoded, tuple)
        assert decoded == (
            Entry(
                asset_id="eip155:1/slip44:60",
                quantity=Quantity(15 * 10**17, 18),
                direction=Direction.IN,
            ),
        )
        # Real enum member, not a bare "in" string that == would forgive.
        assert decoded[0].direction is Direction.IN

    def test_round_trip_is_the_exact_inverse(self, make_entry):
        entries = (
            make_entry(),
            make_entry(
                asset_id=USDC,
                quantity=Quantity(25 * 10**5, 6),
                direction=Direction.OUT,
            ),
        )
        assert decode_entries(encode_entries(entries)) == entries

    def test_78_digit_raw_round_trips_exactly(self):
        decoded = decode_entries(GOLDEN_HUGE)
        assert decoded[0].quantity == Quantity(10**77, 18)
        assert decoded[0].quantity.raw == 10**77

    def test_negative_raw_round_trips_exactly(self):
        decoded = decode_entries(GOLDEN_NEGATIVE)
        assert decoded[0].quantity == Quantity(-7, 0)
        assert decoded[0].direction is Direction.OUT

    def test_empty_json_list_decodes_to_empty_tuple(self):
        assert decode_entries("[]") == ()


class TestRowConverters:
    def test_transaction_to_row_copies_every_field(self, make_txn):
        txn = make_txn(removed=True, last_modified_seq=7)
        row = transaction_to_row("tenant-a", txn)
        assert row.tenant_id == "tenant-a"
        assert row.id == ID_DEFAULT
        assert row.chain_id == "eip155:1"
        assert row.tx_hash == "0xabc"
        assert row.account_id == "acct_1"
        assert row.block_number == 19_000_000
        assert row.initiated_at == MS
        assert row.confirmed_at == MS + 12_000
        assert row.entries_json == GOLDEN_ONE
        assert row.removed is True
        assert row.last_modified_seq == 7

    def test_row_to_transaction_is_the_exact_inverse(self, make_txn):
        txn = make_txn(removed=True, last_modified_seq=9)
        assert row_to_transaction(transaction_to_row("tenant-a", txn)) == txn

    def test_round_trip_preserves_default_bookkeeping(self, make_txn):
        txn = make_txn()
        back = row_to_transaction(transaction_to_row("tenant-b", txn))
        assert back == txn
        assert back.removed is False
        assert back.last_modified_seq == 0

    def test_round_trip_preserves_none_fields(self, make_txn):
        # Pending txns: block_number and confirmed_at are None, not 0.
        txn = make_txn(block_number=None, confirmed_at=None)
        back = row_to_transaction(transaction_to_row("tenant-a", txn))
        assert back.block_number is None
        assert back.confirmed_at is None
        assert back == txn
