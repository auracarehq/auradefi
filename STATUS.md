# STATUS — build log and honest state

Built by a five-role agent loop (spec-interpreter → test-author →
implementer → harsh-reviewer → devops-docs, one pass per SPEC §11 phase);
see [`docs/AGENT_PROMPTS.md`](docs/AGENT_PROMPTS.md) to re-run it.

## Currently

**All ten phases DONE.** `.venv/bin/pytest` → **3,027 passed**, offline,
with no API keys, on a fresh clone and inside a network-less container.
The 0.1.0 release gate (build → `twine check` → wheel contents →
fresh-venv install → quickstart against the wheel → Docker test image →
Docker runtime image → all twelve notebooks executed) is green.

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

## Known caveats (deliberate, documented, not bugs)

1. **ACB reports unrealized from the pool, `TaxLot.cost_basis` from the
   lots.** Under `method="acb"`, `PnLReport.unrealized` subtracts the ACB
   **pool's** cost, while each open lot still reports its own remaining
   basis. The two will not agree — the pool is what ACB actually costs
   with, and the lots remain ground truth for lot-level reporting
   (`docs/DECISIONS.md`, "ACB pooling"; demonstrated in
   `docs/books/11_accounting.ipynb`).

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
