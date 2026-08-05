# Changelog

All notable changes to auradefi. Format follows Keep a Changelog; versions
follow SemVer once past 1.0.

## [Unreleased]

## [0.1.1]

A correctness release. 0.1.0 is published and **should not be used**: an
independent adversarial review found nineteen verified defects, none of
which failed a test (`docs/RELEASE_0.1.1.md` is the full accounting). This
section covers the identity, persistence and sync-correctness half — waves A
and B of that document's §5. Four of the six were **silent**: they lost
transactions or returned empty results while reporting success.

### Upgrading — 0.1.0 embed data is NOT portable

Read this before upgrading a host that ingested through the **library**
(`from auradefi import Auradefi`); hosts that only used the HTTP API are
unaffected.

- The embed **connection id** is now chain-scoped
  (`sha256("embed|{tenant_id}|address|{chain_id}|{address}")`), so every
  0.1.0 embed connection id re-derives on the next connect.
- That id **is** the ledger `account_id` that `transaction_id` hashes
  (`chain_id|tx_hash|account_id`), so **every transaction id of a
  library-ingested row re-derives with it**. Rows written by 0.1.0 keep the
  old chainless `account_id`, so they stop matching into holdings and
  metrics, and re-ingesting the same history arrives as fresh rows rather
  than as idempotent redeliveries.
- **Tenant** ids are unchanged under the default project id (`"embed"`), so
  0.1.0 rows remain addressable *by tenant* — just not by `account_id`.
- A host with library-ingested data in place either re-derives those account
  and transaction ids in a migration, or accepts the old rows as orphaned
  history. There is no in-package migration.

### Changed — breaking

- **`SyncStatePort` grew a fifth method, `tenants()`.** The published,
  `runtime_checkable` Protocol a host may implement now requires it, and
  `Auradefi.__init__` checks for it at **bind** time: a store written
  against the 0.1.0 four-method shape raises `ValidationError` naming the
  missing method instead of binding cleanly and reporting `no_op=True`
  forever. `connections(tenant_id)` needs the tenant already known, so with
  only four methods nothing in a restarted process could ask the store what
  work it held.
- **`ConnectionSyncReport` gained `failed: bool = False`**, and
  `SyncReport` a derived `failed_connections` property. `failed` and `no_op`
  are mutually exclusive — "I could not do it" is not "nothing needed
  doing", and a row claiming both raises `ValidationError`.
- `embed.models.derive_connection_id` takes `chain_id`;
  `derive_tenant_id` takes an optional `project_id`.

### Fixed

- **#19 — the library and the HTTP API addressed different tenants.** The
  facade keyed rows by `sha256("embed|{external_user_id}")` while
  `GET /crypto/sync` keyed them by `sha256("{project_id}|…")`: ingest with
  the library, read over HTTP, and the client saw an account with **zero
  transactions, forever**, with no error on either side. There is now one
  derivation, threaded from the new `Settings.project_id` (default
  `"embed"`, the 0.1.0 value).
- **#26 — a connection id dropped `chain_id`.** The same address could be
  watched on exactly one chain — the second connect returned a
  `ConflictError` naming an id the caller already owned — and two chains
  would have shared one sync cursor. One address on two chains is now two
  connections with independent cursors.
- **#18 — backfill dropped transactions and reported success.** The window
  restarted strictly *below* the lowest block ingested, so when a page
  ended inside a block the rest of that block was never fetched, and
  `backfill_complete` still flipped to `True`. Permanent, unrecoverable,
  silent. The boundary block is now inclusive and de-duplication is by
  transaction id rather than by treating `block_number` as a unique cursor.
- **#21 — `sync()` no-opped after a restart.** Connections were enumerated
  from an in-process list, so a restarted worker returned
  `SyncReport(no_op=True)` — success-shaped — while ingesting nothing,
  forever. They are enumerated from the injected `SyncStatePort`.
- **#24 — an unseeded chain connected, then broke the whole sync loop.**
  Only the CAIP-2 *shape* was validated, so connecting an address on a chain
  absent from `ChainRegistry` succeeded and every later `sync()` raised
  `UnknownChainError` — starving **every other connection** as the
  exception escaped the loop. `connect_address` now checks registry
  membership, and one connection's `AuradefiError` is filed as
  `ConnectionSyncReport.failure` (costing one budget unit) instead of
  aborting the tick. A non-`AuradefiError` still propagates: a bug is not
  an API contract.
- **#22 — an orphaned transaction could never be resurrected.**
  `plan_reorg` decided re-add by `payload_equal`, which ignores the
  `removed` flag, so a transaction orphaned by an earlier reorg and back
  on-chain **unchanged** landed in neither bucket and stayed `removed=True`
  forever. The stored `removed` flag is now part of the re-add decision.

### Documentation

- `docs/DECISIONS.md` pins the three 0.1.1 decisions (embed id derivation,
  the sync loop's containment rules, the `SyncStatePort` version break).
- Three new style gates keep the docs from drifting: a version pinned in
  DECISIONS must own a CHANGELOG section and announce a portability break
  (`tests/style/test_release_note_companions.py`), and an executable doc may
  not pin a retired derived value or a stale dataclass repr
  (`tests/style/test_docs_pin_live_values.py`).

## [0.1.0] — in progress

The first release implements all ten SPEC phases. 3,027 tests pass on a
fresh clone with no API keys and no network access. Entries below describe
**capability**, one per phase.

### Added

- **Scaffold and gates.** Spec (`docs/SPEC.md`), Apache-2.0 licence,
  packaging (hatchling, `py.typed`), CI, Docker (test + runtime stages),
  and a release gate that builds, `twine`-checks and installs the wheel into
  a clean venv. Style is enforced as tests, not convention: size caps
  (300 soft / 400 hard, **no allowlist**), structure, placement (tests
  mirror source; tables only in `models.py`), and layering (acyclic domain
  graph; no web framework outside `api/`; no ORM outside
  `ledger/backends/`; HTTP clients only in I/O domains).

- **Phase 0 — foundation.** Exact money and identity.
  - `money/` — `Quantity` (arbitrary-precision integer + decimals, exact
    string rendering, never scientific notation), `Money` (`Decimal` +
    currency), and the pinned four-field wire form with a strict ASCII
    grammar (rule #2: a raw amount is never a JSON number).
  - `chains/` — CAIP-2 registry (no vendor name zoo) with EVM/Bitcoin/Solana
    family helpers, seeded with Ethereum, Polygon, Base, Bitcoin and Solana.
  - `assets/` — CAIP-19 parse/canonicalize, chain-agnostic `Asset` with
    per-chain `Implementation`, deterministic permanently-stable asset ids
    (rule #3), both-ways registry lookup, groups with a decimals-equality
    law and a `single` fallback, and additive-never-destructive spam scoring
    that returns the numbers rather than a verdict (rule #9).
  - `ledger/` — `LedgerPort`, deterministic transaction ids, last-modified
    cursor tokens, upsert diffing, reorg planning (`removed` + re-`added`,
    SPEC §6.4) and a tenant-isolated `MemoryLedger` with resurrection
    semantics and pagination.
  - `testing/cassettes` — recorded-HTTP replay whose misses fail loudly, so
    the whole suite runs offline.

- **Phase 1 — balances to holdings.** `sources/evm/etherscan.py` (Etherscan
  V2: one key, many chains, paged token discovery that skips malformed rows
  additively and dedupes mixed-case contracts), `prices/` (a structural
  `PriceOracle` seam, a first-wins `Inquirer`, and a DefiLlama oracle), and
  `portfolio/` which assembles them into a `HoldingsReport` with an **exact**
  `Decimal` total — context-free multiplication, so a 78-digit raw survives
  intact. Unpriced assets are listed, never guessed at zero.

- **Phase 2 — tenancy.** `tenancy/` ships the org → project → end-user →
  connection graph with derived (`usr_`, `conn_`) ids, scoped `adk_live_` /
  `adk_test_` API keys with rotation and revocation, the Vezgo-style
  `authEndpoint` mint (short-lived HS256 user tokens signed per project),
  a three-window quota counter, and an append-only audit log. Every
  tenant-data method is project-scoped in its first argument.

- **Phase 3 — transactions.** `decode/` turns explorer rows into the rich
  model — `parts[]` for movements, `acts[]` for sub-operations, fees as
  **siblings** carrying `borne_by`, a derived `type`, and `data_quality`
  that names what is missing. `ledger/bridge.py` projects rich into the
  durable `LedgerTransaction`, dropping fees by design. Reorgs emit
  `removed` + re-`added` with a strictly monotonic cursor, and an orphaned
  transaction that resurfaces keeps its id.

- **Phase 4 — positions.** `positions/` ships the discover/resolve adapter
  protocol over a single `ContractReader` seam, adapters for Uniswap v2,
  Uniswap v3 (canonical TickMath), Aave v3 and receipt-token liquid staking
  (Lido, Rocket Pool), per-adapter golden vectors pinned to Ethereum block
  20,450,000, group totals with health factor and LTV, and the drill-down
  that projects positions into **signed synthetic Holdings** — a negative
  quantity for debt, so a Plaid-only client summing `institution_value`
  gets the right net worth (SPEC §6.3).

- **Phase 5 — embedding.** `from auradefi import Auradefi`: the host binds
  its own session factory, transport, prices and clock, and the library
  emits no DDL and opens no connection it was not handed. Adds the
  SQLModel ledger backend, a budgeted two-phase (live + backfill) resumable
  sync that self-throttles to a genuine zero-request no-op, and
  `project/scalar.py`'s 26-metric projection.

- **Phase 6 — Bitcoin.** Pure-Python BIP32 public derivation validated
  against the published test vectors, bech32 (`p2wpkh`) addresses, the
  BIP44 gap-20 scan over an Esplora source, and confirmed-only UTXO
  arithmetic that the mempool cannot move. The extended key is bound in a
  closure and mechanically cannot reach HTTP (SPEC §10).

- **Phase 7 — Solana.** JSON-RPC source for native and SPL balances,
  including Token-2022 under its own program id, with **ScaledUiAmount**
  carried as both the exact `Quantity` and the node's `uiAmountString` plus
  a `scaled_ui` flag — because `raw / 10**decimals` is not always the
  displayed amount. Signature history with pinned paging, and one
  `SourceError` for every RPC failure shape.

- **Phase 8 — HTTP API and webhooks.** `api/` is a thin FastAPI shell over
  injected ports: the `authEndpoint`, connections, Plaid's exact
  `/crypto/sync` envelope, `/coverage` generated from live capability
  bindings (never prose, rule #10), batch holdings mounted only when the
  capability is bound, nine quota headers on every response, and one error
  taxonomy mapped to one status table. `webhooks/` delivers HMAC-SHA256
  signed payloads over a pinned retry schedule into a dead letter queue,
  with replay that creates a new delivery and never mutates the original.

- **Phase 9 — accounting.** `accounting/` is pure and clock-free: a lot
  ledger with pluggable selectors (FIFO, LIFO, HIFO and ACB pooling),
  exact rational arithmetic that rounds only at the `Fraction → Money`
  boundary, realised and unrealised PnL with `None`-propagation rather than
  zero-guessing, Plaid-shaped `tax_lots`, and **arbitrary-date** PnL that
  replays instead of interpolating between pre-computed marks.

- **Documentation.** Twelve executable notebooks under `docs/books/` — one
  per capability, every cell offline and asserting real values, executed
  headlessly in CI so they cannot rot — plus a quickstart that exercises
  every phase against the installed wheel, `docs/DECISIONS.md` recording
  every pinned algorithm and id formula, and `docs/AGENT_PROMPTS.md`
  documenting the agent loop that built the repo.
