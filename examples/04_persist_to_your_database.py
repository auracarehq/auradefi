"""How do I store this in MY database, and stream changes to my clients?

    pip install 'auradefi[sql]'
    python examples/04_persist_to_your_database.py

The ledger is a port with four methods. This file uses the shipped
SQLModel backend against a sqlite file you can open with any client
afterwards, and shows the four properties a downstream consumer actually
depends on:

* **the host owns the schema.** The library emits no DDL and opens no
  connection: you create the tables and hand over a session factory;
* **upsert is idempotent.** Re-ingesting the same transaction produces no
  second row and no second event;
* **`sync(cursor)` is a Plaid-shaped cursor feed.** `added` / `removed`
  with a `next_cursor` and `has_more`, so a client can resume exactly
  where it stopped and never has to re-read history;
* **a reorg is expressible.** A transaction that leaves the canonical
  chain is emitted as `removed`, and if it comes back it is re-`added`
  under the same id, never mutated in place, never quietly deleted.

Every id here is derived, so two independent workers ingesting the same
transaction write the same row.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlmodel import Session

from auradefi.errors import NotFoundError
from auradefi.ledger.backends.models import metadata
from auradefi.ledger.backends.sqlmodel import SqlModelLedger
from auradefi.ledger.models import (
    Direction,
    Entry,
    LedgerTransaction,
    SyncEventKind,
    transaction_id,
)
from auradefi.ledger.reorg import plan_reorg
from auradefi.money.quantity import Quantity

CHAIN = "eip155:1"
ETH = "eip155:1/slip44:60"
ACCOUNT = "acct_main"
ALICE, BOB = "usr_alice", "usr_bob"     # two tenants, one database


def transaction(index: int, block: int) -> LedgerTransaction:
    """One inbound 0.1 ETH transfer. The id is DERIVED, never assigned."""
    tx_hash = "0x" + f"{index:02x}" * 32
    return LedgerTransaction(
        id=transaction_id(CHAIN, tx_hash, ACCOUNT),
        chain_id=CHAIN, tx_hash=tx_hash, account_id=ACCOUNT,
        block_number=block,
        initiated_at=1_753_000_000_000 + index * 1_000,
        confirmed_at=1_753_000_000_500 + index * 1_000,
        entries=(Entry(asset_id=ETH, quantity=Quantity(10**17, 18),
                       direction=Direction.IN),),
    )


with tempfile.TemporaryDirectory() as tmp:
    database = Path(tmp) / "host.db"

    # ------------------------------------------------ 1. your schema, your engine
    # `metadata.create_all` is the HOST calling it. In production this is
    # your Alembic migration; the library never runs DDL behind your back.
    engine = create_engine(f"sqlite:///{database}")
    metadata.create_all(engine)
    ledger = SqlModelLedger(session_factory=lambda: Session(engine))
    print(f"tables created by the host: {', '.join(sorted(metadata.tables))}")

    # ------------------------------------------------------- 2. idempotent write
    batch = [transaction(index, 18_000_000 + index) for index in range(1, 4)]
    events = ledger.upsert(ALICE, batch)
    again = ledger.upsert(ALICE, batch)          # the same tick runs twice
    assert [event.kind for event in events] == [SyncEventKind.ADDED] * 3
    assert again == [], "a re-ingest is a no-op, not a duplicate"
    print(f"\nupsert: {len(events)} added, re-upsert: {len(again)} events "
          "(safe to retry a whole tick)")

    # -------------------------------------------------------- 3. the cursor feed
    seen, cursor, pages = [], None, 0
    while True:
        page = ledger.sync(ALICE, cursor, limit=2)
        pages += 1
        seen.extend((event.kind.value, event.transaction.id) for event in page.events)
        print(f"  page {pages}: {len(page.events)} event(s), "
              f"next_cursor={page.next_cursor} has_more={page.has_more}")
        cursor = page.next_cursor
        if not page.has_more:
            break

    assert pages == 2 and len(seen) == 3
    assert ledger.sync(ALICE, cursor).events == (), "a drained cursor returns nothing"
    print(f"  drained in {pages} pages; the cursor is where a client resumes")

    # --------------------------------------------------------- 4. tenant isolation
    # Bob's ledger is empty. There is no filter to forget: the tenant id is
    # a parameter of every call, and asking for someone else's row raises.
    # Bob asking for Alice's transaction id gets the same answer as Bob
    # asking for a transaction that never existed: not found. The id is not
    # an existence oracle across tenants.
    assert ledger.sync(BOB).events == ()
    try:
        ledger.get(BOB, batch[0].id)
    except NotFoundError as exc:
        print(f"\nBob asking for Alice's transaction: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------- 5. the reorg
    # Block 18,000,003 is re-mined and transaction 3 lands in a later block.
    # `plan_reorg` compares what we stored against what the chain now says,
    # from a block number down.
    canonical = [transaction(3, 18_000_009)]
    stored = [ledger.get(ALICE, txn.id) for txn in batch]
    plan = plan_reorg(stored, canonical, from_block=18_000_003)
    reorg_events = ledger.apply_reorg(ALICE, plan)

    delta = ledger.sync(ALICE, cursor)
    print("\nreorg from block 18,000,003:")
    for event in delta.events:
        print(f"  {event.kind.value:<8} {event.transaction.id}  "
              f"block={event.transaction.block_number} removed={event.transaction.removed}")

    # Same id, new block, and the row is NOT removed: it was re-added, which
    # is exactly what a client replaying the feed needs to see.
    assert [event.kind for event in reorg_events] == [SyncEventKind.ADDED]
    resurrected = ledger.get(ALICE, batch[2].id)
    assert resurrected.block_number == 18_000_009 and resurrected.removed is False
    print(f"  transaction 3 kept its id {resurrected.id}: history stays "
          "addressable across a reorg")

    # A transaction that does NOT come back is emitted as removed, with the
    # row retained so the feed can carry the retraction.
    dropped = plan_reorg([ledger.get(ALICE, batch[1].id)], [], from_block=18_000_002)
    (removal,) = ledger.apply_reorg(ALICE, dropped)
    assert removal.kind is SyncEventKind.REMOVED
    assert ledger.get(ALICE, batch[1].id).removed is True
    print(f"  transaction 2 orphaned -> {removal.kind.value}, row kept with removed=True")

    print(f"\nsqlite file: {database.name} "
          f"({database.stat().st_size} bytes, openable with any client)")

# The same code against Postgres is one URL away:
#     engine = create_engine("postgresql+psycopg://user@host/db")
# The port is exercised against sqlite in CI; Postgres should work through
# it unchanged, and is not yet covered by a gate (README, *What is not there*).
print("\nOK: your schema, your session, derived ids, a resumable feed.")
