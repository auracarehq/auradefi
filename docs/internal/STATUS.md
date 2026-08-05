# STATUS — build log and honest state

Built by a five-role agent loop (spec-interpreter → test-author →
implementer → harsh-reviewer → devops-docs, one pass per SPEC §11 phase);
see [`docs/internal/AGENT_PROMPTS.md`](AGENT_PROMPTS.md) to re-run it.

## Currently

**All ten phases DONE and 0.1.1 is released.** `.venv/bin/pytest` →
**3,247 tests, 3,247 passed, 0 failed** (count from `--junitxml`), offline,
on a fresh clone with no API keys. All twelve PyBooks execute clean
(`bash scripts/run_books.sh`) and all eleven examples run green
(`bash scripts/run_examples.sh`). `pyproject.toml` and
`__init__.__version__` both read **0.1.1**, which is the version on PyPI.

The release gate — version agreement → build → `twine check` → wheel
contents → fresh-venv install → quickstart and every example against the
installed wheel → Docker test image → Docker runtime image → all twelve
notebooks executed — is `bash scripts/release_check.sh`. It executes the
PyBooks as its last step because `pytest` never opens a notebook, so
without that the loop's own gates could not see a published book gone
stale (that happened: `docs/books/09_embedding.ipynb` once asserted a
connection id 0.1.1 had stopped minting, with the suite green).

The wave table below records what each 0.1.1 wave fixed and is kept for
history; the findings that were open while the release was in progress are
resolved unless this file says otherwise.

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

## 0.1.1 — released

`docs/internal/RELEASE_0.1.1.md` is the spec: nineteen verified defects in the
published 0.1.0, **none of which failed a test**. `CHANGELOG.md` `[0.1.1]`
carries the host-facing account, including the *Upgrading* note — 0.1.0
**library**-ingested data is not portable, because the embed connection id
(and every `transaction_id` hashed over it) re-derives. Data written through
the HTTP API is unaffected.

| Wave | Issues | State on `main` |
|---|---|---|
| security | #20, #25, #30, #33, #34, #35, #36 | **landed** (`c826eab` and the `fix/tenancy-keys` / `fix/api-auth` / `fix/api-deps` merges): `scopes: []` no longer mints a full-privilege token, `rotate()` no longer revives a revoked key, `/auth/revoke` is no longer a cross-tenant oracle, an audit row's IP is the socket peer unless a proxy hop is explicitly trusted, a refused mint is metered, and two unauthenticated 500s are gone — per-issue detail in `CHANGELOG.md` |
| A — identity/persistence | #19, #26 | **landed** — one derivation site, chain-scoped connection ids, and both chains' rows carrying their own `account_id` through ingest |
| B — sync correctness | #18, #21, #22, #24 | **landed** — resumable backfill window, connections enumerated from the state port, `removed` set on reorg, one connection's failure contained to its own row |
| C/D/E | #27, #28, #23, #29, #31, #32, #17 | **landed** — including #17, which is now the mechanical gate `tests/style/test_spec_layout_matches_tree.py` rather than prose |

Gate files, all green: `tests/contract/test_release_0_1_1_wave2.py` and
`tests/contract/seams/test_wave2_*.py`.

### Open note carried forward from the wave-2 audit

One finding from that audit is real, unfixed, and deliberately recorded
rather than closed:

**`derive_connection_id`'s two `str` parameters are silently swappable.**
`derive_connection_id(tenant_id, address, chain_id)` matches the
DECISIONS-pinned segment order, and a caller who passes
`(tenant_id, chain_id, address)` gets a plausible id and no error — the
audit's own seam test did exactly that. The seam test was corrected; the
signature was not. It should be keyword-only past `tenant_id`, which is a
source change with no behaviour change and no migration, and is the right
first commit of 0.1.2.

## Known caveats (deliberate, documented, not bugs)

1. **ACB reports unrealized from the pool, `TaxLot.cost_basis` from the
   lots.** Under `method="acb"`, `PnLReport.unrealized` subtracts the ACB
   **pool's** cost, while each open lot still reports its own remaining
   basis. The two will not agree — the pool is what ACB actually costs
   with, and the lots remain ground truth for lot-level reporting
   (`docs/internal/DECISIONS.md`, "ACB pooling"; demonstrated in
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

One, recorded above in full: **`derive_connection_id`'s two `str`
parameters are silently swappable** and the signature should be
keyword-only past `tenant_id` (0.1.1 wave-2 seam audit). Harsh-reviewer
findings that survive 3 fix rounds land here, never silently dropped.
