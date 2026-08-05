# Bring your own

Yes: your own API, your own database, your own prices, your own clock. Every
collaborator is a port, and a port is a plain object with one or two methods.
There is no base class to inherit and no registration step, because the
protocols are structural. Satisfying the shape *is* implementing them.

The defaults exist so you do not have to start here. Replace one port and keep
the rest:

```python
aura = Auradefi.from_env()                          # all defaults
aura = Auradefi.from_env(ledger=MyLedger())         # your database
aura = Auradefi.from_env(source=MySource())         # your chain data
aura = Auradefi.from_env(prices=MyPrices())         # your price feed
aura = Auradefi(ledger=…, source=…, prices=…)       # nothing of ours
```

| Port | Methods | Default | Replace it when |
|---|---|---|---|
| `source` | 2 | `EtherscanSource` | you have your own node, vendor or archive |
| `prices` | 1 | DefiLlama via `Inquirer` | you need BTC/SOL, or your own marks |
| `ledger` | 4 | `MemoryLedger` | always, in production: the default is not durable |
| `sync_state` | 5 | `MemorySyncState` | you want cursors to survive a restart |
| `clock` | 1 | `SystemClock` | you are testing, or replaying history |

## Your own database

`ledger` is where transactions live. The shipped SQL backend takes a session
factory instead of a URL, so that your application keeps ownership of the
engine, the connection pool and the migrations. auradefi never opens a
connection you did not hand it and never emits DDL, which is also why there is
no `AURADEFI_DATABASE_URL` to set.

```python
from sqlalchemy import create_engine
from sqlmodel import Session

from auradefi import Auradefi
from auradefi.ledger.backends.models import metadata
from auradefi.ledger.backends.sqlmodel import SqlModelLedger

engine = create_engine("postgresql+psycopg://user@host/db")
metadata.create_all(engine)          # your migration, run once, by you

aura = Auradefi.from_env(
    ledger=SqlModelLedger(session_factory=lambda: Session(engine)),
)
```

Install it with `pip install 'auradefi[sql]'`. Postgres and sqlite both go
through the same port; only sqlite is exercised in CI.

If you would rather own the DDL, [Database schema](schema.html) has both
tables as plain SQL for Postgres and SQLite, ready for Alembic, Flyway or a
reviewed migration. It also covers two hazards worth knowing about before you
hand-write the schema.

### Or write the port yourself

Four methods, all of them tenant-scoped. `tenant_id` is the first argument
everywhere, and no call may read or write across tenants:

```python
class MyLedger:
    def upsert(self, tenant_id, txns) -> list[SyncEvent]: ...
    def sync(self, tenant_id, cursor=None, limit=100) -> SyncPage: ...
    def get(self, tenant_id, txn_id) -> LedgerTransaction: ...
    def mark_removed(self, tenant_id, txn_ids) -> list[SyncEvent]: ...
```

Callers depend on three behaviours, so a replacement has to copy them:

1. `upsert` is idempotent. Re-ingesting an unchanged transaction emits no
   event, which is what makes a whole tick safe to retry.
2. A removed row that comes back is re-added rather than mutated: stored with
   `removed=False`, a bumped sequence, and an `ADDED` event. That is how a
   reorg stays expressible.
3. `sync` pages by last-modified order rather than by transaction date, so an
   old row that changes reappears at the end of the feed. Clients page until
   `has_more` is `False` before persisting the cursor.

`get` for another tenant's id must raise `NotFoundError`, which is
indistinguishable from a row that never existed. An id therefore cannot be
used as an existence oracle across tenants.

See [guide 04](examples/04_persist_to_your_database.html).

## Your own chain data

`source` is one object with two methods. You may not have to write it, since
`EtherscanSource` ships and `from_env()` binds it.

```python
class MySource:
    def balances(self, chain_id: str, address: str) -> list[BalanceRecord]:
        """What the address holds NOW. Feeds holdings and pricing."""

    def fetch_txlist(self, chain_id, address, *, start_block, end_block,
                     page, offset, sort) -> list[dict]:
        """ONE page of raw history rows for exactly that window."""
```

The engine owns the window. It chooses the blocks, the page number and the
sort order, and it learns that a window has drained by receiving a page
shorter than `offset`. Answer the window you were asked for: do not widen it,
do not page internally, and do not retry silently. Returning everything at
once defeats the budget, and returning an empty page early advances a cursor
over data you never read.

Rows come back raw, as `list[dict]`, because parsing belongs to the decoder
seam. You can replace that too, via `decoder=`.

To signal failure, raise `auradefi.errors.SourceError`, or any
`AuradefiError`, and `sync()` will contain it to that one connection's report
row. Anything else propagates, since a `KeyError` in your adapter is a bug and
a loud tick is the better outcome.

See [guide 03](examples/03_write_a_source_adapter.html).

## Your own prices

One method. Returning nothing for an asset is allowed and is not an error:

```python
class MyPrices:
    def usd_prices(self, caip19s) -> dict[str, Money]:
        return {asset_id: Money(Decimal("2500"), "USD"), …}
```

An asset you omit comes back held but unpriced: listed, named in
`report.unpriced`, and never valued at zero. Bind this port if you need
Bitcoin or Solana prices, which the default cannot provide at all.

Use `Money` with exact `Decimal` amounts. A float reintroduces the drift this
arithmetic exists to avoid.

## Your own cursor store

`sync_state` holds connections and their sync cursors. The default is
in-process, so a restart forgets every connection. The SQL-backed
implementation is not written yet, and this is the port to bind if you want
durable cursors before it lands.

```python
class MyState:
    def get_state(self, tenant_id, connection_id) -> SyncState: ...
    def put_state(self, tenant_id, connection_id, state) -> None: ...
    def connections(self, tenant_id) -> tuple[ConnectionRecord, ...]: ...
    def add_connection(self, tenant_id, record) -> None: ...
    def tenants(self) -> tuple[str, ...]: ...
```

`tenants()` is the one method with no `tenant_id`, and it carries real weight:
`sync()` enumerates its work from the store. A worker that read its tenant
list from process memory would restart, find nothing, and report a cheerful
`no_op` forever. That was a real defect (0.1.1 #21).

## Your own clock

```python
class MyClock:
    def now_ms(self) -> int: ...
```

`FrozenClock(ms)` ships for tests and replays, and `SystemClock` is the
default. Because time is a port, quota windows, sync throttling and `as_of_ms`
are all testable without sleeping, and Sandbox can hand you reproducible
answers.

## What is not pluggable

Three edges, stated plainly.

The chain registry is per-instance and mutable, so `register()` a chain and
`connect_address` will accept it. The seeded set is five chains, and the
decoder needs an entry to exist before a connection can be made.

The decoder is replaceable via `decoder=`, but the shipped one handles EVM
native txlist rows only.

Position adapters need a `ContractReader` that you supply. No `eth_call`
transport and no multicall ship in this package, which is the largest gap
between working and working against mainnet.
