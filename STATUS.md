# STATUS — overnight autonomous build

Live log of the phase loop (spec-interpreter → test-author → implementer →
harsh-reviewer → devops-docs, per SPEC §11 phase). Newest first.

## Currently

- **In flight:** scaffold (wave 0) — foundation modules, style gates,
  cassette harness, packaging, Docker, CI.

## Phase gates

| Phase | Gate | State |
|---|---|---|
| 0 | pytest green on fresh clone, no API keys | in flight |
| 1 | known-rich address → sane USD total (cassettes) | pending |
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
