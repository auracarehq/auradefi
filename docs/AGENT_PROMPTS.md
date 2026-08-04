# AGENT_PROMPTS — the loop that builds auradefi

This repo was built by a five-role agent graph, phase by phase, and the
graph is checked in: the roles as agent definitions under
[`.claude/agents/`](../.claude/agents), the orchestration as a workflow at
[`.claude/workflows/phase-build.js`](../.claude/workflows/phase-build.js).

This file is the copy-paste runbook. A newcomer with Claude Code and this
document can run the next phase — or re-run an existing one — unaided.

All ten SPEC phases are done (see [`STATUS.md`](../STATUS.md)); the loop
remains the way to add a phase 10, a new chain, or a new adapter family.

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
harsh-reviewer ──► approve | fix_required
      │                  │
      │                  ├─ category contains "test" ──► test-author (adds pinning tests)
      │                  └─ everything else ──────────► implementer (fixes code)
      │                            └──► re-review (≤ 3 rounds)
      ▼  once the whole phase integrates green:
devops-docs ──► PyBooks, README, CHANGELOG, STATUS, Docker, release gate
      │
      ▼
orchestrator: full pytest + style gates → commit + push → next phase
```

Two things this diagram encodes that are easy to get wrong:

* **Review findings are routed by category, not all dumped on the
  implementer.** The implementer is ownership-blocked on test files, so a
  finding like *"these tests would not catch a wrong number"* must go to a
  **test-author**, who adds pinning tests. Both fixers can run in the same
  round.
* **Waves are a barrier.** Orders inside a wave run in parallel over
  disjoint file sets; the next wave does not start until the previous one
  finishes, because waves encode dependency ordering.

Unresolved blockers/majors after 3 rounds escalate to
[`STATUS.md`](../STATUS.md) — never silently dropped.

## Running a phase with the workflow (recommended)

From the repo root, in Claude Code:

> Run the `phase-build` workflow with args `{"phase": N}`. When it returns,
> run the full suite (`.venv/bin/pytest`), fix integration breakage, run
> the devops-docs agent for the release gate, update STATUS.md, then
> commit and push the milestone.

The workflow returns a summary: per-order verdicts, unresolved findings,
`shared_files_needed`, and the interpreter's notes.

**Before launching the waves**, the orchestrator must author everything the
interpreter listed under `shared_files_needed` — new dependencies in
`pyproject.toml`, new error classes, new `ALLOWED_IMPORTS` entries in
`tests/style/test_layering.py`, new cassettes. Agents never touch shared
files, so a missing one blocks a whole wave.

### If the session dies mid-phase

It happened here (a usage-limit window killed three concurrent phases at
~04:00). **Re-invoke the same workflow with the same args.** Completed
agents are served from the workflow cache, so the run resumes at the first
incomplete agent with zero loss and zero duplicated work. Do not hand-patch
half-built orders; let the loop finish them.

If you already have a validated plan, pass it to skip re-interpretation:
`{"phase": N, "plan": { … }}`.

### Running phases concurrently

Phases with disjoint file sets can run at once — 4, 5, 6, 7 and 9 were
built concurrently here. The constraint is file ownership, not phase
number: check that no two in-flight phases claim the same `src_files`,
`test_files` or shared files. Run the full suite between merges.

## Copy-paste prompts (for running roles by hand)

Each role's full contract is in `.claude/agents/<role>.md`; these prompts
are the short form that loads it.

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
evidence. Any blocker/major ⇒ fix_required. Tag test-quality findings with
a category containing "test" so they route to a test-author, not to the
implementer.

<WORK ORDER JSON HERE>
```

### 4b — test-author, closing test-quality findings
```text
You are the test-author, closing test-quality review findings on your work
order below. Add ONLY the missing pinning tests described. Never weaken or
delete an existing test. Verify each pinned behaviour against the CURRENT
source first; if the source is actually wrong, REPORT it instead of
pinning the bug.

<WORK ORDER JSON + FINDINGS JSON HERE>
```

### 5 — devops-docs (once per phase, after integration)
```text
You are the devops-docs agent for auradefi phase N. Make the phase
shippable and documented. Your surface: docs/books/*.ipynb,
docs/examples/quickstart.py, README.md, CHANGELOG.md, STATUS.md,
docs/AGENT_PROMPTS.md, docs/RELEASING.md, Dockerfile, docker-compose.yml,
.github/workflows/ci.yml, scripts/*. Never src/, never tests/, never git.

1. Add a numbered executable notebook under docs/books/ for the new
   capability: build it with nbformat from a throwaway script in /tmp,
   every cell offline (cassettes or in-memory fixtures), markdown cells
   citing SPEC §numbers, code cells asserting REAL values. Then execute it
   headlessly (.venv/bin/jupyter execute --inplace <nb>) and confirm exit 0
   — an unexecuted notebook is undelivered work.
2. Extend docs/examples/quickstart.py; it must stay green against the
   installed wheel with only core dependencies (guard optional extras).
3. Update the README capability table (true TODAY, and say what is NOT
   there), CHANGELOG (capability, not activity) and STATUS.md.
4. RUN the release gate and report literal output:
   bash scripts/release_check.sh
   docker build --target test -t auradefi:test . && docker run --rm --network none auradefi:test
   docker build -t auradefi:0.1.0 . && docker run --rm --network none auradefi:0.1.0
   Report anything you could not make pass, with a diagnosis. Never report
   a gate you did not run. If you find a source bug, REPORT it; do not fix it.
```

## Orchestrator duties (whoever runs the loop)

- Author `shared_files_needed` **before** waves launch; agents never touch
  shared files.
- Adjudicate `disputed_tests` and `blocked_on` reports from implementers.
- Run the full suite between waves and between concurrent phases; fix only
  cross-order integration breakage.
- Re-invoke a killed workflow rather than repairing it by hand — the cache
  resumes it.
- Update `STATUS.md` per phase (gate table + unresolved findings + any new
  deliberate caveat).
- Commit and push only on green milestones; agents never run git.
