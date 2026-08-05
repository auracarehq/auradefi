"""The published schema must be the schema the code writes.

MOTIVATING DEFECT. `metadata` mapped every Python `int` to SQLAlchemy
`Integer`, which is **int4** on PostgreSQL. Two of those columns hold
millisecond epochs — `initiated_at` was 1_754_000_000_000, over the int4
ceiling by 816x — so the first insert against Postgres would have failed with
"integer out of range". 3,348 tests passed anyway, because the suite runs on
SQLite, whose INTEGER affinity is already 8 bytes. A green suite coexisted
with a backend the README said "should work" on Postgres and could not.

Nothing catches that class of bug except asserting on the DDL, because the
DDL is where the type actually appears. So:

1. **The committed `.sql` files match `metadata`.** They are what a host
   applies through Alembic, Flyway or a reviewed migration; a column added to
   `ledger/backends/models.py` and missing here is a migration that silently
   omits a column, which is worse than no migration at all.
2. **No column that holds a millisecond epoch, a sequence or a block number
   is 32-bit.** Stated per column rather than "everything is BIGINT", so the
   reasoning survives someone adding a genuinely small column later.

Both are cheap, and the alternative is finding out from a host's production
insert.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

REPO = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO / "docs" / "schema"

#: Columns whose values do not fit in 32 bits, and why.
WIDE_COLUMNS = {
    "auradefi_ledger_transactions": {
        "initiated_at": "millisecond epoch — 1.75e12, int4 ceiling is 2.1e9",
        "confirmed_at": "millisecond epoch",
        "last_modified_seq": "monotonic per-tenant counter, unbounded",
        "block_number": "block heights grow without a ceiling",
    },
    "auradefi_ledger_seqs": {
        "seq": "monotonic per-tenant counter, unbounded",
    },
}

INT4_MAX = 2_147_483_647


def test_the_schema_files_exist() -> None:
    """A host applying a migration needs the DDL as TEXT, not as a REPL call."""
    for dialect in ("postgresql", "sqlite"):
        path = SCHEMA_DIR / f"ledger_{dialect}.sql"
        assert path.is_file(), f"{path.relative_to(REPO)} is missing"
        assert "CREATE TABLE auradefi_ledger_transactions" in path.read_text(
            encoding="utf-8"
        )


def test_the_committed_ddl_matches_the_models() -> None:
    """`scripts/emit_schema.py --check` regenerates and diffs."""
    finished = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "emit_schema.py"), "--check"],
        capture_output=True, text=True, cwd=REPO,
    )
    assert finished.returncode == 0, (
        "the committed schema no longer matches ledger/backends/models.py — "
        "run `python scripts/emit_schema.py` and commit the result:\n"
        + finished.stdout + finished.stderr
    )


@pytest.mark.parametrize(
    ("table_name", "column_name", "reason"),
    [
        (table, column, reason)
        for table, columns in WIDE_COLUMNS.items()
        for column, reason in columns.items()
    ],
)
def test_wide_columns_are_not_32_bit(table_name: str, column_name: str,
                                     reason: str) -> None:
    from auradefi.ledger.backends.models import metadata

    table = metadata.tables[table_name]
    rendered = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    line = next(
        (row.strip() for row in rendered.splitlines()
         if row.strip().startswith(f"{column_name} ")),
        None,
    )
    assert line is not None, f"{table_name}.{column_name} is not in the DDL"
    assert "BIGINT" in line, (
        f"{table_name}.{column_name} compiles to `{line}` on PostgreSQL, but "
        f"holds values that do not fit in 32 bits: {reason}. SQLite will "
        "accept it and Postgres will not, so the suite cannot see this."
    )


def test_a_millisecond_epoch_really_does_overflow_int4() -> None:
    """The premise, asserted, so the parametrised test above is not folklore."""
    from auradefi.clock import SystemClock

    now_ms = SystemClock().now_ms()
    assert now_ms > INT4_MAX * 100, (
        "a millisecond epoch is expected to dwarf the int4 ceiling; if this "
        "ever fails, the clock is not returning milliseconds"
    )


def test_only_the_ledger_claims_tables() -> None:
    """The schema page promises exactly two auradefi tables; keep that true.

    Tenancy, keys, quota, audit, webhooks and embed sync-state are in-memory
    in this release. A third `auradefi_` table appearing means the docs and
    the migration story both need editing — which is the point of failing.

    Filtered by the `auradefi_` prefix rather than compared to the whole
    registry, because `metadata` IS `SQLModel.metadata` — a global that also
    holds every table the HOST defines. That sharing is why the prefix exists
    and why the `.sql` files matter: `create_all()` on this object creates
    the host's SQLModel tables too, and the emitted DDL creates exactly two.
    """
    from sqlmodel import SQLModel

    from auradefi.ledger.backends.models import metadata

    assert metadata is SQLModel.metadata, (
        "the registry stopped being shared, which is good news — update the "
        "schema documentation, which warns hosts that it is"
    )
    ours = {name for name in metadata.tables if name.startswith("auradefi_")}
    assert ours == {
        "auradefi_ledger_transactions",
        "auradefi_ledger_seqs",
    }, sorted(ours)
