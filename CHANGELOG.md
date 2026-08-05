# Changelog

All notable changes to auradefi. Format follows Keep a Changelog; versions
follow SemVer once past 1.0.

## [Unreleased]

## [0.1.1]

A correctness release. 0.1.0 is published and **should not be used**: an
independent adversarial review found nineteen verified defects — #18 to #36 —
and **none of them failed a test** (`docs/RELEASE_0.1.1.md` is the full
accounting). All nineteen are fixed here, each with a regression test that
fails against the unfixed code.

Five were security, four lost transactions or returned empty results while
reporting success, and the rest produced wrong numbers, unhandled 500s, or
broke any host-supplied implementation of a declared interface.

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
  silent. The fix is not a boundary tweak: a `min(block)` cursor is
  *coarser* than the row-level unit being paginated, so neither boundary
  works — exclusive drops the split block, and inclusive re-reads page 1
  every tick and never advances at all once one block holds more rows than
  `page_size`. `SyncState` now carries the window end and the drained page
  (`backfill_end`, `backfill_page`), so the walk is monotonic over a stable
  ordered list and every row arrives exactly once.
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

### Fixed — security

- **#20 — `scopes: []` minted a full-privilege token.** `body.scopes or
  key.scopes` treated an explicitly-empty list as "omitted", so a request
  for a **zero**-privilege token returned one carrying every scope the API
  key held. An empty list now means empty, and such a token is refused by
  every scoped route.
- **#33 — `POST /auth/revoke` was a cross-tenant authenticity oracle.** The
  signing secret was selected from the token's own *unverified* `project_id`
  claim, so a foreign token verified under its real owner's secret — and the
  response then separated genuine-and-live (404), bad-signature (401
  `AuthError`) and genuine-but-expired (401 `TokenExpiredError`). An attacker
  with any free project and their own `users:admin` key could POST captured
  JWTs and learn which were authentic and still live **for projects they hold
  no credential for**, then replay only those; they cannot compute this
  offline. Every non-owned outcome is now one status, one error type, one
  body.
- **#34 — an unauthenticated `RecursionError` became an unhandled 500.**
  `_peek_project_id` guarded only `(ValueError, UnicodeDecodeError)`, so a
  ~26 KB token of ~10,000 nested arrays raised `RecursionError` — a
  `RuntimeError` — and escaped as an unformatted 500 instead of the pinned
  401. Reachable with **no credentials at all** via `GET /users/me`, burning
  a worker on a 10,000-frame unwind and leaking a stack trace per request.
  Tokens are bounded before any decode (`MAX_TOKEN_CHARS = 1024`) and
  `RecursionError` is caught — in the core verifier too, where the identical
  guard had the identical hole.
- **#25 — `rotate()` revived a revoked key.** No revoked/expired guard, so
  rotating a revoked key id minted a **live** key with the revoked key's
  project, environment and full scope set: any bulk rotation job silently
  re-privileged a key an operator had revoked. Rotation now refuses a revoked
  or expired key, only ever **shortens** `expires_at`, and both `revoke` and
  `rotate` are tenant-gated — they previously looked the key up in a global
  dict with no project filter, so any project could revoke any other's key.
- **#30 — the audit log recorded a caller-controlled IP.** `_client_ip`
  returned the first `X-Forwarded-For` hop verbatim, so any caller chose the
  IP its own audit row would record — permanently, in a log with no mutation
  surface. The hop count now comes from `Deps.trusted_proxy_hops` (default
  **0**: socket peer only), the chain is read across *repeated* field lines
  per RFC 9110 §5.3, and `AuditRecord` carries `ip_source` so a
  header-derived value is never mistaken for a verified one.

### Fixed — declared interfaces that lied

- **#27 / #28 — `WebhookSink` promised less than the routes require.**
  `create_replay` was undeclared (the replay route reaches it *indirectly*
  through `webhooks.replay.replay`, so grepping for `webhooks.create_replay`
  found nothing) and `register_endpoint` was typed as returning one object
  while the route unpacks a 2-tuple. Every host-supplied sink got an
  unhandled 500 there; the shipped store worked by accident. The seam is now
  stated in full in the new `api/sinks.py`, with structural row Protocols
  naming every attribute the wire projections read — a bare `object` return
  is unimplementable from the declaration alone, which is what made the
  whole class possible. `get_delivery` is deliberately *not* declared: no
  route calls it, and an unused promise makes every host write dead code.
- **#35 — `POST /auth/token` 500'd on wire-string scopes.** Line 113 read
  `{scope.value for scope in key.scopes}` while the *very next line* already
  used the tolerant `str(scope)`. `ApiKeyStore.issue` stored
  `frozenset(scopes)` with no coercion and `has_scope` compares with `in`,
  which succeeds for plain strings because `Scope` is a `StrEnum` — so a key
  rehydrated from JSON or SQL authenticated everywhere and then
  `AttributeError`ed here. Scopes are coerced at the store boundary and the
  route is tolerant on both lines.

### Fixed — wrong numbers and unmetered work

- **#23 — a non-USD price was relabelled USD.** `portfolio/holdings.py`
  stamped `Money(..., "USD")` without reading `price.currency`, so a EUR
  oracle price produced a total wrong by the FX rate, labelled as dollars,
  absent from `unpriced`, with nothing raised. `Inquirer` now enforces the
  oracle contract its own docstring states (`CurrencyMismatchError`), and
  holdings carries the price's currency through — a host may bind a bare
  oracle rather than an `Inquirer`, so both halves are needed.
- **#29 — the `rounded_basis` flag was discarded.** Every
  `fraction_to_money` call site indexed `[0]` and dropped the `is_exact`
  bit, and none of `AssetPnL`, `PnLReport` or `TaxLot` had a field to hold
  it — so a figure accurate to 28 significant digits was indistinguishable
  from one exactly right, and DECISIONS' "always flagged" promise was kept
  by nothing. All three carry `flags` now, and the report flags if its own
  total rounded *or* if anything underneath it did.
- **#31 — one stale descriptor dropped the whole staking slice.**
  `ReceiptTokenAdapter.resolve` indexed unguarded, so a `KeyError` on the
  first unknown descriptor removed **every** Lido/Rocket Pool position from
  `net_worth`. Now `.get(...)` + `continue`, matching the Aave adapter, and
  the skip costs no RPC.
- **#36 — a refused mint cost the caller nothing.** The three sibling
  handlers consume quota immediately after authentication; `POST /auth/token`
  deferred it and `raise ScopeError` returned first, so a tenant could drive
  unlimited authenticated mints that always request a scope the key lacks —
  each walking and HMAC-comparing every stored key — and decrement nothing.
  Quota is consumed after authentication, before the privilege check, and a
  refused mint still writes **no** audit row.
- **#32 — a 422 burned quota.** `GET /crypto/sync` consumed quota before
  `_resolved_limit` and the cursor decode, so a client with a hard-coded bad
  limit drained the **project's** per-day window on requests it could never
  succeed at, then 429'd every other user of that project. It now validates
  first, the rule `POST /batch/holdings` already stated in `_checked_size`.

### Fixed — honesty

- **#17 — README understated its own gaps.** *What is not there* omitted the
  whole `jobs/` package, five of the nine declared `api/routes/` modules,
  `project/plaid.py` + `native.py`, and `prices/historian.py` + `store.py`.
  All are now stated, and
  `tests/style/test_spec_layout_matches_tree.py` diffs the shipped tree
  against `docs/SPEC.md` §3.2 in **both** directions against a committed
  inventory, so neither a module arriving nor one leaving can happen without
  the prose being revisited.

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
