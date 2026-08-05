# loop.md — a self-verifying build loop for spec-driven projects

Drop a specification in, get a built, tested, documented project out. The
loop is language- and domain-agnostic: everything project-specific lives in
one profile file.

This is version 2. Version 1 built a ten-phase library — 3,027 tests green,
every acceptance gate passing, release artifacts clean — and an independent
adversarial pass afterwards found **15 verified defects**, three of them
silent data loss and three security. Every stage added in v2 exists because
of a specific defect v1 could not see. The evidence is in
[Why each stage exists](#why-each-stage-exists); read it before deleting a
stage to save tokens.

---

## The portable unit

Copy these into any repository:

```
loop.md                          this file
.claude/loop.profile.yml         the ONLY project-specific file — you write this
.claude/agents/*.md              nine role definitions
.claude/workflows/phase-build.js the orchestration
```

Then write your spec (anywhere; the profile points at it) and run:

```
Workflow({ name: 'phase-build', args: { phase: 1 } })
```

Nothing else in the loop names your language, your test runner, your
directory layout or your domain. If you find a hardcoded assumption, it is a
bug in the loop, not a thing to work around.

---

## Quick start

**1. Write the profile.** `.claude/loop.profile.yml` binds the loop to your
project. Every field is used by at least one agent; there are no optional
decorations. See [The profile](#the-profile).

**2. Write the spec.** Any format the agents can read. It must contain:

- a **phase list** — the increments to build, in order, each with a
  *done-when* gate stated as an observable outcome, not a task list
- **non-negotiable rules** — the invariants the whole system must hold
- enough **interface detail** that two independent readers would build the
  same public surface

If your spec pins algorithms (hash formulas, wire encodings, rounding
rules), put them in a separate `decisions` file and point the profile at it.
Golden-vector tests derive from that file, so it becomes the arbiter when
tests and implementation disagree.

**3. Run one phase at a time.** Read the returned report before starting the
next. The loop is designed to be supervised at phase boundaries — that is
where a human catches a spec misreading before it compounds.

---

## The pipeline

```
Audit ──▶ Interpret ──▶ Gate ──▶ Build ──▶ Prove ──┐
  (0)        (1)         (2)       (3)      (4)    │
                                    ▲              ▼
                                    └── fix ◀── Seam ──▶ Sweep ──▶ Ship ──▶ Integrate
                                                (5)       (6)      (7)        (8)
```

| # | Phase | Agent | Barrier? | Deliverable |
|---|-------|-------|----------|-------------|
| 0 | Audit | `spec-auditor` | — | the spec's own claims checked against the enforced gates |
| 1 | Interpret | `spec-interpreter` | — | work orders with disjoint file ownership |
| 2 | Gate | `gate-author` | — | the phase acceptance test, written **blind** |
| 3 | Build | `test-author` → `implementer` → `harsh-reviewer` (≤3 rounds) | per order | green tests + implementation |
| 4 | Prove | `mutation-gate` | per order | proof each test discriminates |
| 5 | Seam | `seam-auditor` | **per wave** | third-party-binding tests across boundaries |
| 6 | Sweep | `pattern-sweeper` | per finding | the finding's *class* eliminated tree-wide |
| 7 | Ship | `devops-docs` | end of phase | docs, packaging, release gate |
| 8 | Integrate | `integrator` | end of phase | the real dependency graph, one branch per order |

Stages 3–4 **pipeline** per work order: order B can be mutating while order
C is still implementing. Stage 5 is a genuine barrier — it exists precisely
to look between orders, so it needs them all finished. Stage 6 pipelines per
confirmed finding.

Stages 0 and 8 bracket the rest and were added in v3. Both exist because a
build can be internally perfect and still be wrong at its edges: **0** checks
the instructions before anyone follows them, **8** checks that work which was
decomposed cleanly can also be *delivered* separately.

---

## Why each stage exists

The root cause of v1's misses, in one sentence: **the unit of review was
inherited from the unit of parallelism.** Work was decomposed by disjoint
file ownership so many orders could build concurrently, and the reviewer was
then handed that same decomposition as its scope. It caught everything
*inside* a work order. It could not see anything *between* two orders that
were each internally consistent and green.

### Stage 2 — blind gates

*v1 failure:* the phase-3 acceptance gate was written by the same lineage
that built phase 3, and it was written to pass. The resurrection test used a
fixture with a changed block number, so it exercised a different code path
than the one it claimed to pin, and a real bug shipped underneath it.

`gate-author` reads the spec and is **forbidden from reading the source
tree** (enforced by ownership, and by running before any implementation
exists). Its gate must fail against a mutated build in stage 4.

### Stage 4 — mutation gate

*v1 failure:* "red for the right reason" governed how tests were *written* —
fail with the unimplemented sentinel, never with an import error. There was
no equivalent discipline for how they went **green**. A test can be red
correctly and green vacuously.

Every test carries a `pins:` declaration written **before** the
implementation exists:

```python
def test_an_orphaned_transaction_returning_unchanged_is_readded():
    # pins: a transaction removed by an earlier reorg, reappearing with an
    #       identical payload, is re-added rather than left removed
```

`mutation-gate` then attempts, for each pin, to construct a mutant that
violates the stated behaviour and proves the test goes red. Two outcomes are
findings:

- the mutant is applied and the test **stays green** → `vacuous-test`
- no mutant can be constructed, because nothing in the tree implements the
  pinned behaviour → `unimplemented-pin`

This is the highest-value stage in the loop. It converts "the suite is green"
from a proxy into evidence.

### Stage 5 — seam auditor

*v1 failures (four of the fifteen):* two modules derived the same logical
identifier by different formulas, so the library wrote rows one place and
the API read another — both green, composition silently empty. Two routes
called methods their declared interface never promised, so every
host-supplied implementation got a 500 while the shipped one worked.

`seam-auditor` never looks inside a module. Its input is an inventory of
boundaries, and its deliverable is a test that **binds a minimal
implementation written only from the declared interface** — the thing no
in-repo test ever does, because in-repo tests use the in-repo class.

### Stage 6 — pattern sweeper

*v1 failure:* a falsy-vs-absent bug (`if not x` where `x is None` was meant)
was found in one file, fixed in that one file, and the **identical** bug two
files away shipped.

A confirmed finding is a sample, not an incident. `pattern-sweeper` searches
the tree for the *class*, and where the class is mechanically detectable,
writes a permanent check into the style-gate directory. It is the only agent
that may write there.

### Stage 3 — report-honesty lens

*v1 failures (three of the fifteen):* `backfill_complete=True` while
transactions were missing; `no_op=True` while connections existed;
`unpriced=()` while a value had been silently mislabelled. All the same
shape — a success-shaped report that is not true.

`harsh-reviewer` gained a seventh lens: for every field that means *nothing
is wrong*, try to construct a state where it lies. Success-shaped failure is
the worst outcome for a library, and nothing in v1 was pointed at it.

---

## What was kept, deliberately

- **Disjoint file ownership.** It is what made many-order concurrency work
  with zero merge conflicts. The fix for its blind spot is an added pass
  (stage 5), not a weaker constraint.
- **Test-first.** v1's failures were not missing tests; they were tests that
  did not discriminate. Stage 4 attacks that directly.
- **The adversarial reviewer.** It earned its keep — in v1 it found a real
  authentication bypass. Its scope was the problem, not its existence.
- **A bounded fix loop (≤3 rounds).** Unbounded fixing converges on
  agreeable nonsense. What survives three rounds is escalated to the
  `status` file, never silently dropped.

---

## The profile

`.claude/loop.profile.yml` is the only file you edit per project.

```yaml
project:
  name: my-project
  spec: docs/SPEC.md          # the specification (required)
  decisions: docs/DECISIONS.md # pinned algorithms; "" if you have none
  status: STATUS.md            # where unresolved findings land (required)

layout:
  source_root: src/my_project
  test_root: tests
  style_gates: tests/style     # pattern-sweeper's exclusive territory
  seam_tests: tests/seams      # seam-auditor's exclusive territory
  mirror: "{source_root}/a/b.py <-> {test_root}/a/test_b.py"
  errors_module: src/my_project/errors.py  # "" if not applicable

commands:                      # {path} is substituted; omit what you lack
  test: ".venv/bin/pytest"
  test_path: ".venv/bin/pytest {path}"
  collect: ".venv/bin/pytest {path} --collect-only -q"
  style: ".venv/bin/pytest tests/style"
  release: "bash scripts/release_check.sh"

language:
  # a stub body, so a test fails for the right reason before implementation
  unimplemented: "raise NotImplementedError"
  # how mutation-gate reverts a behaviour without breaking compilation
  mutation_style: "edit the smallest expression that implements the pin"

seam_patterns:                 # how to inventory boundaries in your language
  interfaces: "grep -rn 'Protocol\\|ABC\\|@abstractmethod' {source_root}"
  derivations: "grep -rnE 'def (derive_|make_|.*_id)' {source_root}"
  wire_formats: "grep -rn 'to_wire\\|from_wire\\|serialize' {source_root}"

rules:                         # your project's non-negotiables, verbatim
  - "Money uses exact decimals; a float equality on money is a defect."
  - "Timestamps are millisecond-epoch integers."
  - "Only exception classes declared in errors_module may be raised."
```

Everything the agents need is here. If an agent needs something that is not,
it stops and reports `blocked_on` rather than guessing — a guess in one agent
becomes a spec divergence in twelve.

**A command the loop cannot see does not exist.** `commands` is the whole of
what the loop runs, so an artefact executed by anything else is outside every
gate. auradefi's 0.1.1 wave 2 paid for this: the twelve notebooks under
`docs/books/` are executed only by `scripts/run_books.sh`, wired into a CI
job and into `docker compose run books` — neither `commands.test`,
`commands.style` nor `commands.release` opens a notebook. A published book
therefore went on asserting a connection id the code had stopped minting, with
the suite green. If your project has an executable-documentation artefact,
either add it to `commands` (a `docs:` entry the release stage must run) or
add a style gate that checks it at the text level; `tests/style/`
`test_docs_pin_live_values.py` in this repo is the second option.

---

## Invariants

These are what make the loop safe to run at concurrency. Each is enforced in
an agent's role definition, and violating one is how a build corrupts itself.

1. **No file has two owners.** Ever, at any concurrency, across any stage.
   `spec-interpreter` guarantees it for work orders; `pattern-sweeper` and
   `seam-auditor` have exclusive directories precisely so they can run
   alongside everything else.
2. **Implementers never edit tests.** A wrong test is *escalated*
   (`disputed_tests`), never edited into agreement. This is the single rule
   that keeps green meaningful.
3. **No agent runs `git`** except read-only inspection (`diff --stat`,
   `ls-files`). The orchestrator owns history.
4. **`mutation-gate` restores what it mutates**, and proves it with an empty
   `git diff` over its own files before reporting.
5. **Every finding carries evidence** — something the agent ran or read,
   quoted. A claim without evidence is reported at low confidence and says
   so.
6. **Unresolved findings are written down.** Three fix rounds and still
   disputed means it lands in the `status` file with its evidence. A loop
   that quietly drops what it cannot fix is worse than no loop.

---

## What this loop still does not catch

Stated plainly, so you do not over-trust it.

- **Wrong specs.** Every stage measures fidelity to the spec. If the spec is
  wrong, the loop will faithfully build the wrong thing and prove it works.
  Phase boundaries are the human review point for exactly this reason.
- **Live integration.** Everything is verified offline against recorded
  fixtures. Provider drift, real network semantics, real clock skew and real
  concurrency are outside its reach. Budget a separate, credentialed
  reconciliation job.
- **Emergent performance.** Stage 4 proves behaviour, not cost. Add explicit
  budget tests to your gates if latency matters.
- **Cross-phase design drift.** `seam-auditor` audits boundaries as built,
  not whether the architecture still makes sense eight phases later. That
  judgement is yours.

---

## Cost

The added stages cost roughly 15–25% on top of a v1 build. Run stages 4–6 on
every phase — they are the only stages that can tell you the other stages
lied, so they are the worst possible place to economise.

Economise here instead. These numbers are measured, not estimated: the
adversarial pass that found v1's fifteen defects ran 55 agents and 4,240,334
tokens in 45 minutes.

| phase | agents | tokens | share |
|---|---|---|---|
| Verify | 46 | 2,264,757 | **53.4%** |
| Find | 6 | 1,513,598 | 35.7% |
| Sweep | 1 | 338,244 | 8.0% |
| Synthesize | 1 | 77,436 | 1.8% |
| Scope | 1 | 46,299 | 1.1% |

**Verification dominates, and roughly a third of it was duplicated work.**
That run verified 43 findings which collapsed to ~30 distinct defects — the
merge happened at synthesis, *after* paying for it. A further ~15 verified
findings were then discarded by the report cap.

The four economies that follow, all of which preserve detection exactly:

1. **Cluster by root cause before verifying, not after.** Verify one
   representative per cause and propagate the verdict. Same merge, ahead of
   the money.
2. **Carry the finder's reproduction into the verifier.** A verifier averaged
   49k tokens and 11 tool calls rebuilding a scenario the finder had already
   reproduced. Hand it the repro and ask it to *refute*, not rediscover.
3. **Rank before verifying** so a report cap never discards work already paid
   for — and log what went unverified, because a silent cap reads as "we
   looked at everything".
4. **Give every finder a shared module map.** Six finders each independently
   read the same 262 files; the scope stage had already walked the tree.

`.claude/workflows/code-review-lean.js` implements all four against the
built-in review workflow. In this loop, the equivalents are: delta reviews in
rounds ≥2 (`reviewMode: delta` — adjudicate the fixes, not the module again),
`effort: 'medium'` on the stages where the judgement was spent upstream
(`pattern-sweeper`, `devops-docs`), and the rule that the work order's
`contract` is authoritative so `test-author` and `implementer` never re-read
the spec.

**Never tier down `mutation-gate` or `seam-auditor`.** Choosing a mutant that
violates exactly one pin, and telling a real interface lie from a
coincidence, are judgement. That is where v1's defects lived.

## Recovering a killed run

A workflow that dies — usage limits, a killed session — does not have to be
rebuilt. Every invocation persists its script and returns a `runId`:

```
Workflow({ scriptPath: "<returned path>", resumeFromRunId: "<returned runId>" })
```

Agents whose `(prompt, opts)` are unchanged replay from cache at **zero
token cost**; the first changed call and everything after it runs live. Same
script, same args → 100% cache hit. This also makes iterating on the script
cheap: edit it, resume, and only the edited stage re-runs.

Before diagnosing an empty or surprising result, read
`<transcriptDir>/journal.jsonl` — one line per agent with its actual return
value. Do not assume a cached result was non-empty.

During the v1 build I hit roughly a dozen usage-limit windows and re-ran
phases from scratch rather than resuming. That was avoidable and expensive.
