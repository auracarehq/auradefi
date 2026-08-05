# STATUS — build log and honest state

Built by a five-role agent loop (spec-interpreter → test-author →
implementer → harsh-reviewer → devops-docs, one pass per SPEC §11 phase);
see [`docs/AGENT_PROMPTS.md`](docs/AGENT_PROMPTS.md) to re-run it.

## Currently

**All ten phases DONE; the 0.1.1 fix release is IN PROGRESS and this branch
is RED.** `.venv/bin/pytest` → **3,127 tests, 3,105 passed, 22 failed**
(count from `--junitxml`; the count is above the 3,033 this branch started
at, and no test was deleted or weakened — but the suite is not green).
Do not read anything below as shipped.

The 0.1.0 release gate (build → `twine check` → wheel contents →
fresh-venv install → quickstart against the wheel → Docker test image →
Docker runtime image → all twelve notebooks executed) was green at 0.1.0.
On this branch `bash scripts/release_check.sh` **FAILS**: it now executes
the PyBooks as its last step — `pytest` never opens a notebook, so without
that the loop's own gates could not see a published book gone stale — and
`docs/books/09_embedding.ipynb` does not execute clean. Everything before
that step (version agreement, build, `twine check`, wheel contents,
fresh-venv install, quickstart against the installed wheel) passes, and
`docker compose run --rm demo` runs the quickstart green in a network-less
container. `pyproject.toml` and `__init__.__version__` both still read
**0.1.0**: the version bump is the orchestrator's call, not this stage's.
See *0.1.1 — release in progress* below.

## Phase gates

Each gate is a real test file, run by the normal suite — not a claim.

| Phase | Gate | State |
|---|---|---|
| 0 | pytest green on fresh clone, no API keys | **DONE** — `tests/style` + the foundation suites (money 120, chains 123, assets 192, ledger 318, style 15); 14 harsh-review findings fixed and re-pinned (mutation-proof resurrection/reorg-atomicity tests, strict wire grammar, bool-poisoning guards, asset-id dedup) |
| 1 | known-rich address → sane USD total (cassettes) | **DONE** — `tests/golden/test_phase1_holdings.py` (7): 18,988,784.99999872437900871726 USD exact, 0.059% from the incumbent reference; 220 tests across portfolio/prices/sources.evm |
| 2 | cross-tenant isolation test that tries to leak | **DONE** — `tests/contract/test_tenant_isolation.py` (9): identical `external_user_id` and descriptor on both sides; cryptographic, id-smuggling, enumeration, audit and quota leak attempts all refused; 183 tenancy tests |
| 3 | reorg fixture → removed + re-added | **DONE** — `tests/contract/test_phase3_reorg.py` (8): decode → bridge → upsert → sync → reorg → sync, cursors strictly increasing, resurrection re-adds under the same id; 92 decode/bridge tests |
| 4 | projection invariant holds | **DONE** — `tests/contract/test_projection_invariant.py` (5): synthetic Holdings sum to net worth by exact `Decimal` equality, and again after repricing; 265 positions tests with block-20450000 goldens |
| 5 | host binds own session, syncs on own tick | **DONE** — `tests/contract/test_embedding.py` (8): host-owned engine/DDL/session, budgeted two-phase sync, no-op proven by counting transport calls; 288 tests across embed/project/sqlmodel |
| 6 | one xpub → full derived balance set | **DONE** — `tests/golden/test_phase6_xpub.py` (9): pure-Python BIP32 against published vectors, 44-interaction cassette enforcing the gap-20 stop, extended key mechanically unable to reach HTTP; 245 tests |
| 7 | SPL balances offline | **DONE** — `tests/golden/test_phase7_solana.py` (9): Token-2022 ScaledUiAmount carried as both raw `Quantity` and `ui_amount_string` with the identity break asserted; 5-POST wire order pinned; 219 tests |
| 8 | signed/durable/replayable webhooks | **DONE** — `tests/contract/test_phase8_http_api.py`: one tenant's whole journey in order — token mint, connection, 409 with `existing_connection_id`, paged Plaid envelope, signed delivery verified with the shipped verifier, the pinned retry schedule into dead letter, replay, redelivery; 428 api/webhooks tests |
| 9 | arbitrary-date PnL on large fixture | **DONE** — `tests/golden/test_phase9_pnl.py` (32) over a generated 50,000-event stream: three arbitrary cutoffs, FIFO and LIFO disagreeing where they must and agreeing where they must, plus `test_phase9_perf.py`; 309 accounting tests |

## 0.1.1 — release in progress (RED)

`docs/RELEASE_0.1.1.md` is the spec: nineteen verified defects in the
published 0.1.0, **none of which failed a test**. `CHANGELOG.md` `[0.1.1]`
carries the host-facing account, including the *Upgrading* note — 0.1.0
**library**-ingested data is not portable, because the embed connection id
(and every `transaction_id` hashed over it) re-derives.

| Wave | Issues | State on this branch |
|---|---|---|
| security | #20, #25, #30, #33, #34, #35, #36 | **NOT in this tree.** Committed on the unmerged branches `fix/tenancy-keys`, `fix/api-auth`, `fix/api-deps`; `HEAD` still has `body.scopes or key.scopes` (#20). Nothing in this file's numbers reflects them. |
| A — identity/persistence | #19, #26 | code landed in `embed/`; #19's single derivation and the chain-scoped id are in place, **#26 is only half-fixed** — see finding 1 below |
| B — sync correctness | #18, #21, #22, #24 | code landed in `embed/`, `ledger/reorg.py`; #21 and #22 hold, **#18 + #24 interact badly** — see finding 2 below |
| C/D/E | #27, #28, #23, #29, #31, #32, #17 | not started |

Gate file: `tests/contract/test_release_0_1_1_wave2.py` (10 tests, **1
failing**). Seam audit: `tests/contract/seams/test_wave2_*.py` (**10
failing**).

### Unresolved findings — 0.1.1 wave 2

Recorded here because the phase was handed to the release stage as
"mutation-proven and seam-audited" with no findings, and it is not.

1. **#26 is half-fixed: both chains' rows land under ONE `account_id`.**
   `tests/contract/test_release_0_1_1_wave2.py::test_26_same_address_on_two_chains_is_two_connections`
   — connect derives two distinct ids (verified: `conn_d0327e21d9b0ea55` on
   `eip155:1`, `conn_acb7e927076b309e` on `eip155:137` for the book's
   tenant), but after `sync()` all six ledger rows across both chains carry
   the **mainnet** connection id. The ingest path stamps `account_id` from
   something other than the connection it is syncing, so cross-chain
   activity is still merged — silently, with a success-shaped report. This
   is the defect #26 exists to remove, one layer further down.

2. **#18 and #24 together recreate silent loss against the recorded
   fixture.** The new backfill window asks for
   `startblock=0&endblock=105&page=1&offset=2&sort=desc`, which
   `tests/cassettes/embed_gate.json` never recorded. The miss is a
   `CassetteMissError` — an `AuradefiError` — so #24's per-connection
   containment turns it into `ConnectionSyncReport(failed=True)` with every
   count zero, and `Auradefi.sync()` returns `no_op=False` and raises
   nothing while **2 of 7 transactions** land. Five
   `test_wave2_backfill_ledger_boundary.py` seam tests, four
   `tests/contract/test_embedding.py` tests and
   `docs/books/09_embedding.ipynb` all fail on this. The fixture must be
   re-recorded (or generated) for the new windows, and the containment
   introduced by #24 should not be able to swallow a fixture miss in the
   offline suite.

3. **`derive_connection_id`'s two `str` parameters are silently
   swappable.** `derive_connection_id(tenant_id, address, chain_id)` matches
   the DECISIONS-pinned segment order, but
   `tests/contract/seams/test_wave2_id_derivations.py` calls it as
   `(tenant_id, chain_id, address)` and gets a plausible id
   (`conn_67fa5aaae890377a`) with no error — three seam failures. The code
   is right and the seam test is wrong, which is exactly why the signature
   should be keyword-only past `tenant_id`.

4. **The phase-5 gate still pins 0.1.0 vectors.**
   `tests/contract/test_embedding.py` hardcodes the chainless
   `conn_b116094c537a85e6` and the transaction ids derived from it.
   `docs/DECISIONS.md` already declares those golden vectors re-derive, so
   the gate's constants are stale rather than the code — but they must be
   updated deliberately, with the old values kept only as retired
   constants.

5. **`plan_reorg` is not chain-scoped.**
   `tests/contract/seams/test_wave2_reorg_chain_scope.py` — a reorg
   announced on `eip155:1` plans the removal of an `eip155:137`
   transaction. Pre-existing (the caller was assumed to scope), surfaced by
   the wave-2 audit, untouched by #22's `removed`-flag fix.

6. **Three style gates are red on source, not docs.**
   `SyncState` still carries no within-block position, so the #18 cursor
   cannot resume inside an oversized block
   (`test_bucket_cursor_has_intra_bucket_offset`); `_decode_page`'s
   docstring still promises that a malformed row's `SourceError`
   *propagates*, which #24 made false
   (`test_injected_callback_error_prose`); and `grp_` is minted by two
   different recipes in `assets/groups.py` and `positions/models.py`
   (`test_id_prefix_namespaces`, pre-existing, needs either agreement or
   registration plus a DECISIONS bullet).

7. **`docs/books/09_embedding.ipynb` is updated but UNEXECUTED.** Its
   prose, its connection-id assertion (`conn_d0327e21d9b0ea55`), the
   chain-scope derivation and a new restart-resume section are written, and
   the first cells pass, but the book cannot execute past the first
   `sync()` until finding 2 is fixed. `tests/style/test_docs_pin_live_values.py`
   is red for that reason and must stay red until the book is re-executed
   (`BOOKS_INPLACE=1 bash scripts/run_books.sh`). Wave 2's capabilities are
   demonstrated executably in `docs/examples/quickstart.py` in the
   meantime — that file runs green against the installed wheel.

## Known caveats (deliberate, documented, not bugs)

1. **ACB reports unrealized from the pool, `TaxLot.cost_basis` from the
   lots.** Under `method="acb"`, `PnLReport.unrealized` subtracts the ACB
   **pool's** cost, while each open lot still reports its own remaining
   basis. The two will not agree — the pool is what ACB actually costs
   with, and the lots remain ground truth for lot-level reporting
   (`docs/DECISIONS.md`, "ACB pooling"; demonstrated in
   `docs/books/11_accounting.ipynb`).

   The report now **says which one it used**: `basis_source` is `"pool"`
   under ACB and `"lots"` for every lot-tracking method, and
   `unrealized_basis` and `open_lots_basis` expose both figures. Summing
   `TaxLot.cost_basis` and comparing it against what `unrealized` implies
   is the wrong check, and it was previously the easiest one to reach for —
   the gap is now a value you can read rather than a discrepancy you
   reverse-engineer (#16).

2. **`positions/` is fixture-driven, pending a multicall reader.** The
   `ContractReader` seam ships and every adapter is pinned to
   block-20,450,000 golden vectors, but no concrete on-chain reader exists
   in the package: no `eth_call` transport and no multicall batcher. Until
   one lands, a host must bind its own reader to run the adapters against a
   live chain. This is the single largest gap between "works" in the README
   table and "works against mainnet".

Both are stated in the README's *What is not there* section as well, so
neither can be discovered only by hitting a discrepancy.

## Unresolved review findings

(none — harsh-reviewer findings that survive 3 fix rounds land here, never
silently dropped)
