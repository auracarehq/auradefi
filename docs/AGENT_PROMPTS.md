# AGENT_PROMPTS — the loop that builds auradefi

This repo is built by a five-role agent graph. The roles are checked in as
agent definitions under [`.claude/agents/`](../.claude/agents), the
orchestration as a workflow under
[`.claude/workflows/phase-build.js`](../.claude/workflows/phase-build.js).
This file is the copy-paste runbook: a newcomer with Claude Code and this
document can run the next phase unaided.

## The graph

```
docs/SPEC.md §11 (phase N)
      │
      ▼
spec-interpreter ──► work orders (disjoint file ownership, dependency waves)
      │
      ▼  per wave, orders in parallel; per order, strictly in sequence:
test-author ──► red tests + stubs (fail with NotImplementedError, never ImportError)
      │
implementer ──► green, never touching a test
      │
harsh-reviewer ──► approve | fix_required ──► implementer fix ──► re-review (≤3 rounds)
      │
      ▼  once the whole phase integrates green:
devops-docs ──► PyBooks, README, CHANGELOG, Docker, release_check.sh, this file
      │
      ▼
orchestrator: full pytest + style gates → commit + push → next phase
```

Unresolved findings after 3 rounds escalate to `STATUS.md` — never dropped.

## Running a phase with the workflow (recommended)

In Claude Code, from the repo root:

> Run the `phase-build` workflow with args `{"phase": N}`. When it returns,
> run the full suite (`.venv/bin/pytest`), fix integration breakage, run
> the devops-docs agent for the release gate, update STATUS.md, then
> commit and push the milestone.

Anything the interpreter lists in `shared_files_needed` (new deps, new
error classes, new ALLOWED_IMPORTS domains, new cassettes) is
**orchestrator work — do it before launching the waves.**

## Copy-paste prompts (for running roles by hand)

### 1 — spec-interpreter
```text
You are the spec-interpreter for auradefi (read-only). Decompose SPEC
phase N into work orders. Read docs/SPEC.md §11 for the gate, the phase's
sections, docs/DECISIONS.md for pinned algorithms, and
tests/style/test_layering.py for the ALLOWED_IMPORTS matrix. Output ONLY
the JSON described in .claude/agents/spec-interpreter.md: waves of orders
with disjoint src_files/test_files (tests mirror source exactly),
contracts quoting SPEC §s and pinned algorithms verbatim, checkable
acceptance criteria, shared_files_needed for anything only the
orchestrator may touch.
```

### 2 — test-author (one per work order)
```text
You are the test-author for the auradefi work order below. Create ONLY its
test_files plus its src_files as stubs (full typed signatures, docstring
contracts, bodies = raise NotImplementedError). Golden vectors: derive
values yourself from docs/DECISIONS.md pinned algorithms via python3 -c,
hardcode the literals. Cover happy path, every documented error with
pytest.raises on the specific auradefi.errors class, boundaries (zero,
negative, 10^77), immutability. Done when:
.venv/bin/pytest <your dirs> --collect-only -q has zero errors;
failures are NotImplementedError only; tests/style is green.
Never touch pyproject.toml, conftest.py, tests/style/, errors.py, git.

<WORK ORDER JSON HERE>
```

### 3 — implementer (one per work order)
```text
You are the implementer for the auradefi work order below. Fill in the
stubbed src_files until the order's tests are green. You may not modify
any test, conftest, gate, errors.py, or pyproject.toml, and you never run
git. If a test is wrong, implement the rest and report it under
disputed_tests — do not game it. Done when: order tests green, tests/style
green, FULL .venv/bin/pytest green, and git diff --stat shows only your
src_files.

<WORK ORDER JSON HERE>
```

### 4 — harsh-reviewer (one per work order, after green)
```text
You are the harsh reviewer for the auradefi work order below. The suite is
green; find what green hides. Six lenses: spec fidelity (cite SPEC §s),
numeric truth (recompute ≥2 golden vectors independently via python3 -c),
adversarial inputs, test quality (would these tests catch a WRONG NUMBER,
not just a crash?), style beyond the gates, API honesty. Output ONLY the
verdict JSON from .claude/agents/harsh-reviewer.md. Every finding needs
evidence. Any blocker/major ⇒ fix_required.

<WORK ORDER JSON HERE>
```

### 5 — devops-docs (once per phase, after integration)
```text
You are the devops-docs agent for auradefi phase N. Make the phase
shippable and documented: extend docs/books/ with an executed notebook for
the new capability (offline, asserted outputs, built with nbformat,
executed with nbclient); extend docs/examples/quickstart.py; update
README capability table and CHANGELOG; keep docs/AGENT_PROMPTS.md current.
Then RUN the release gate and report literal output: bash
scripts/release_check.sh; docker build --target test + docker run
--network none (suite green containerised); runtime image quickstart
green. Never touch src/ or run git.
```

## Orchestrator duties (whoever runs the loop)

- Author `shared_files_needed` before waves launch; agents never touch
  shared files.
- Adjudicate `disputed_tests` and `blocked_on` reports.
- Run the full suite between waves; fix cross-order integration only.
- Update STATUS.md per phase (gates table + unresolved findings).
- Commit + push only on green milestones; agents never run git.
