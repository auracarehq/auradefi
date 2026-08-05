# Database schema

**Two tables.** Here they are as plain SQL, ready to paste into your own
migration:

- **[`ledger_postgresql.sql`](https://github.com/auracarehq/auradefi/blob/main/docs/schema/ledger_postgresql.sql)**
- **[`ledger_sqlite.sql`](https://github.com/auracarehq/auradefi/blob/main/docs/schema/ledger_sqlite.sql)**

Both are generated from the same `metadata` the library uses, and a style gate
regenerates and diffs them, so they cannot drift from the code. Regenerate
locally with `python scripts/emit_schema.py`.

auradefi **never emits DDL** and never opens a connection you did not hand it.
The schema is yours: apply it with Alembic, Flyway, Liquibase, Rails, Prisma,
`psql -f`, or whatever reviews your migrations. If you would rather not, one
call does it for you:

```python
from auradefi.ledger.backends.models import metadata
metadata.create_all(engine)          # fine for a script; read the warning below
```

## `auradefi_ledger_transactions`

One row per transaction, per tenant.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `tenant_id` | `VARCHAR` | no | **PK part 1.** The `usr_…` id. Every query is scoped by it. |
| `id` | `VARCHAR` | no | **PK part 2.** The derived `txn_…` id — stable across runs and backends. |
| `chain_id` | `VARCHAR` | no | CAIP-2, e.g. `eip155:1`. |
| `tx_hash` | `VARCHAR` | no | On-chain hash. Not unique on its own: one hash can touch several accounts. |
| `account_id` | `VARCHAR` | no | Which connection this row was ingested for. |
| `block_number` | `BIGINT` | yes | `NULL` while pending. |
| `initiated_at` | `BIGINT` | no | **Millisecond** epoch. |
| `confirmed_at` | `BIGINT` | yes | Millisecond epoch; `NULL` until confirmed. |
| `entries_json` | `VARCHAR` | no | The movements, as canonical JSON — see below. |
| `removed` | `BOOLEAN` | no | Reorg tombstone. A removed row is kept, never deleted. |
| `last_modified_seq` | `BIGINT` | no | Cursor ordering. Indexed with `tenant_id`. |

Index: `ix_auradefi_ledger_transactions_tenant_seq (tenant_id, last_modified_seq)`
— `sync()` filters and orders on exactly that pair, so this index is what
makes the cursor feed cheap. Keep it.

### `entries_json`

Canonical JSON, sorted keys, no whitespace. Each entry is:

```json
[{"asset_id":"eip155:1/slip44:60","decimals":18,"direction":"in","raw":"1000000000000000000"}]
```

`raw` is a **decimal-int string**, never a JSON number, and this is load-bearing
rather than stylistic: `json.loads("1e77")` yields a float that is wrong by
about `10^60`. Because these rows live in *your* database, something other than
auradefi may write them, so a numeric `raw` is **rejected** on read rather than
coerced — a plausible-looking wrong amount is worse than an error.
`direction` is `in`, `out` or `self`.

## `auradefi_ledger_seqs`

| Column | Type | Null | Meaning |
|---|---|---|---|
| `tenant_id` | `VARCHAR` | no | **PK.** |
| `seq` | `BIGINT` | no | Monotonic counter for that tenant; first value is 1. |

The cursor counter lives in the database, not in the process, so a restart
cannot hand out a sequence number twice. Allocation is documented
**single-writer**: run one ingest worker per tenant, or hold a lock, until
Postgres hardening lands.

## Two things that will bite you

**Every numeric column is `BIGINT`, and it has to be.** Python `int` maps to
SQLAlchemy `Integer`, which is `int4` on PostgreSQL, and a millisecond epoch
(`1_754_000_000_000`) overflows `int4` by 816×. Until 0.1.2 these columns were
`INTEGER`, which meant the SQL ledger **could not work on PostgreSQL at all** —
the first insert would fail with `integer out of range`. SQLite never noticed,
because its `INTEGER` affinity is already 8 bytes, which is exactly why a fully
green test suite hid it. If you hand-write this schema, use 64-bit integers.

**`metadata` is the global `SQLModel.metadata`.** If your application also uses
SQLModel, its tables are in the same registry, so:

```python
metadata.create_all(engine)     # creates OUR two tables AND all of yours
```

That is rarely what you want against a production database, and it is the best
reason to apply the `.sql` files instead — they create exactly two tables and
touch nothing else. The `auradefi_` prefix on every table name exists for the
same reason: these objects land in your database, beside your own.

## What is *not* here

Only the ledger persists. **Tenancy, API keys, quota counters, the audit log,
webhook endpoints and deliveries, and embed sync-state are all in-memory** in
this release — there are no tables for them, and a restart forgets them.

That is a real limitation, not an omission from this page. If you need any of
it durable, the ports are there: bind your own `sync_state`
([Bring your own](bring-your-own.html)), and keep tenancy in your own schema
until a SQL backend for it ships. The audit log in particular is
security-relevant and in-memory; treat it as such.
