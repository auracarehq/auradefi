# STATUS — overnight autonomous build

Live log of the phase loop (spec-interpreter → test-author → implementer →
harsh-reviewer → devops-docs, per SPEC §11 phase). Newest first.

## Currently

- **In flight, concurrently:** Phase 1 (EVM balances→holdings), Phase 2
  (tenancy), Phase 3 (transaction decode + reorg gate), Phase 4
  (positions: UniV2/V3, Aave, liquid staking) — four phase-build
  workflows over disjoint file sets. Phase 0 PyBooks delivered
  (docs/books/01–04, executed in CI).

## Phase gates

| Phase | Gate | State |
|---|---|---|
| 0 | pytest green on fresh clone, no API keys | **DONE** — 664 tests green offline; 6 work orders built test-first by the agent loop (36 agents), all 14 harsh-review findings fixed and re-pinned (incl. mutation-proof resurrection/reorg-atomicity tests, strict wire grammar, bool-poisoning guards, asset-id dedup) |
| 1 | known-rich address → sane USD total (cassettes) | in flight |
| 2 | cross-tenant isolation test that tries to leak | pending |
| 3 | reorg fixture → removed + re-added | pending |
| 4 | projection invariant holds | pending |
| 5 | host binds own session, syncs on own tick | pending |
| 6 | one xpub → full derived balance set | pending |
| 7 | SPL balances offline | pending |
| 8 | signed/durable/replayable webhooks | pending |
| 9 | arbitrary-date PnL on large fixture | pending |

## Unresolved review findings

(none yet — harsh-reviewer findings that survive 3 fix rounds land here,
never silently dropped)
