"""SQLModel table rows and pure converters for the SQL backend (SPEC §8).

Table definitions live ONLY in a ``models.py`` (placement gate), and ORM
imports are legal only under ``ledger/backends/`` (layering gate). Table
names carry the ``auradefi_`` prefix because they land in the HOST's
database. The host binds its own session factory and owns its migration
story (rules #6/#12); the library never emits DDL. The host runs schema
creation itself against :data:`metadata`.

Wire format (rule #2): entries persist as canonical JSON where ``raw`` is
a decimal-int JSON STRING: floats never touch money, and a 78-digit raw
round-trips exactly.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import BigInteger, Index
from sqlmodel import Field, SQLModel

from auradefi.errors import ValidationError
from auradefi.ledger.models import Direction, Entry, LedgerTransaction
from auradefi.money.quantity import Quantity

#: The HOST runs schema creation / migrations against this metadata
#: itself (SPEC §8: storage is a port, we never emit DDL).
metadata = SQLModel.metadata


class LedgerTransactionRow(SQLModel, table=True):
    """One persisted ledger transaction, tenant-scoped by composite PK.

    Mirrors ``auradefi.ledger.models.LedgerTransaction`` plus the owning
    ``tenant_id``. ``entries_json`` is the canonical wire string from
    :func:`encode_entries`. ``(tenant_id, last_modified_seq)`` is indexed
    because sync pages filter and order on it (SPEC §6.4).
    """

    __tablename__ = "auradefi_ledger_transactions"
    __table_args__ = (
        Index(
            "ix_auradefi_ledger_transactions_tenant_seq",
            "tenant_id",
            "last_modified_seq",
        ),
    )

    # BIGINT, not INTEGER, on every numeric column. Python `int` maps to
    # SQLAlchemy `Integer`, which is int4 on Postgres, and a millisecond
    # epoch (1_754_000_000_000) overflows int4 by 816x, so the FIRST insert
    # would fail with "integer out of range". sqlite never noticed because
    # its INTEGER affinity is already 8 bytes, which is why a green suite
    # coexisted with a backend that could not run on Postgres at all.
    tenant_id: str = Field(primary_key=True)
    id: str = Field(primary_key=True)
    chain_id: str
    tx_hash: str
    account_id: str
    block_number: int | None = Field(default=None, sa_type=BigInteger)
    initiated_at: int = Field(sa_type=BigInteger)
    confirmed_at: int | None = Field(default=None, sa_type=BigInteger)
    entries_json: str
    removed: bool = False
    last_modified_seq: int = Field(sa_type=BigInteger)


class TenantSeqRow(SQLModel, table=True):
    """Per-tenant monotonic seq counter. The counter lives in the DB.

    First allocated value is 1 (SPEC §6.4). Allocation is documented
    single-writer; Postgres hardening is Phase 8.
    """

    __tablename__ = "auradefi_ledger_seqs"

    tenant_id: str = Field(primary_key=True)
    seq: int = Field(sa_type=BigInteger)   # BIGINT: see the row above


def encode_entries(entries: Sequence[Entry]) -> str:
    """Canonical JSON string for ``entries`` (rule #2), order preserved.

    ``json.dumps`` of a list of ``{"asset_id", "raw", "decimals",
    "direction"}`` objects with ``sort_keys=True`` and
    ``separators=(",", ":")``. ``raw`` is the decimal-int STRING of
    ``quantity.raw`` (a JSON string, never a JSON number); ``decimals``
    is an int; ``direction`` is the enum value (``"in"``/``"out"``/
    ``"self"``). Deterministic byte-for-byte.
    """
    return json.dumps(
        [
            {
                "asset_id": entry.asset_id,
                "raw": str(entry.quantity.raw),
                "decimals": entry.quantity.decimals,
                "direction": Direction(entry.direction).value,
            }
            for entry in entries
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_raw(raw: object) -> int:
    """The base-unit int behind a wire ``raw``, or ``ValidationError``.

    ``raw`` must be a decimal-int JSON STRING (rule #2): a JSON number is
    rejected, never coerced. These tables live in the HOST's database, so
    a row may have been written by something that is not auradefi, and
    ``json`` parses ``1e77`` to a float that is off by ~10**60: a
    plausible-looking wrong amount is worse than an error. Only an
    optional ``-`` followed by ASCII digits parses, so neither
    underscores (``"1_0"``) nor padding survive as a silent rescale.
    """
    if not isinstance(raw, str):
        raise ValidationError(
            f"entry raw must be a decimal-int string (rule #2), got "
            f"{type(raw).__name__}"
        )
    digits = raw[1:] if raw.startswith("-") else raw
    if not digits or not all(char in "0123456789" for char in digits):
        raise ValidationError(f"entry raw is not a decimal int: {raw!r}")
    return int(raw)


def decode_entries(payload: str) -> tuple[Entry, ...]:
    """Exact inverse of :func:`encode_entries`.

    Each object becomes ``Entry(asset_id, Quantity(int(raw), decimals),
    Direction(direction))``: exact at any magnitude, 78-digit raws
    included. ``decode_entries(encode_entries(entries)) == entries``.

    ``raw`` must be a decimal-int string; a JSON number (or any other
    non-string) raises ``auradefi.errors.ValidationError`` rather than
    decoding to a rounded value.
    """
    return tuple(
        Entry(
            asset_id=item["asset_id"],
            quantity=Quantity(_decode_raw(item["raw"]), item["decimals"]),
            direction=Direction(item["direction"]),
        )
        for item in json.loads(payload)
    )


def transaction_to_row(
    tenant_id: str, txn: LedgerTransaction
) -> LedgerTransactionRow:
    """Pure conversion of ``txn`` to its row under ``tenant_id``.

    Copies every field, encodes ``entries`` via :func:`encode_entries`,
    and carries the bookkeeping fields (``removed``,
    ``last_modified_seq``) verbatim. No I/O.
    """
    return LedgerTransactionRow(
        tenant_id=tenant_id,
        id=txn.id,
        chain_id=txn.chain_id,
        tx_hash=txn.tx_hash,
        account_id=txn.account_id,
        block_number=txn.block_number,
        initiated_at=txn.initiated_at,
        confirmed_at=txn.confirmed_at,
        entries_json=encode_entries(txn.entries),
        removed=txn.removed,
        last_modified_seq=txn.last_modified_seq,
    )


def row_to_transaction(row: LedgerTransactionRow) -> LedgerTransaction:
    """Pure inverse of :func:`transaction_to_row` (drops ``tenant_id``).

    ``row_to_transaction(transaction_to_row(t, txn))`` equals ``txn``
    field-for-field, bookkeeping included. No I/O.
    """
    return LedgerTransaction(
        id=row.id,
        chain_id=row.chain_id,
        tx_hash=row.tx_hash,
        account_id=row.account_id,
        block_number=row.block_number,
        initiated_at=row.initiated_at,
        confirmed_at=row.confirmed_at,
        entries=decode_entries(row.entries_json),
        removed=row.removed,
        last_modified_seq=row.last_modified_seq,
    )
