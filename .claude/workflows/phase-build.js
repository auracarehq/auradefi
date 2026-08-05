export const meta = {
  name: 'phase-build',
  description: 'Build one spec phase: audit the spec -> interpret -> blind gate -> waves of (test -> implement -> review) -> mutation proof -> seam audit -> pattern sweep -> ship -> integrate',
  whenToUse: 'Invoke with args {phase: N} (optionally a pre-validated {plan}). Project bindings come from .claude/loop.profile.yml; see loop.md. The orchestrator prepares shared_files_needed, integrates, and commits after this returns.',
  phases: [
    { title: 'Audit', detail: "the spec's claims checked against what the project enforces" },
    { title: 'Interpret', detail: 'work orders with disjoint file ownership' },
    { title: 'Gate', detail: 'acceptance gate written blind, from the spec alone' },
    { title: 'Build', detail: 'per order: failing tests first, then implementation, then adversarial review' },
    { title: 'Prove', detail: 'mutation gate — every pin must be falsifiable' },
    { title: 'Seam', detail: 'audit the boundaries BETWEEN orders' },
    { title: 'Sweep', detail: 'generalise each finding to its class, tree-wide' },
    { title: 'Ship', detail: 'docs, packaging, release gate' },
    { title: 'Integrate', detail: 'one branch per order — the REAL dependency graph' },
  ],
}

const input = typeof args === 'string' ? JSON.parse(args) : args
const phaseNo = input?.phase
if (phaseNo === undefined || phaseNo === null) throw new Error('args.phase is required')
const MAX_FIX_ROUNDS = input?.maxFixRounds ?? 3

// ---------------------------------------------------------------- schemas

const ORDER_PROPS = {
  id: { type: 'string' },
  title: { type: 'string' },
  src_files: { type: 'array', items: { type: 'string' } },
  test_files: { type: 'array', items: { type: 'string' } },
  contract: { type: 'string' },
  acceptance: { type: 'array', items: { type: 'string' } },
  seams: { type: 'array', items: { type: 'string' } },
  depends_on: { type: 'array', items: { type: 'string' } },
  // Late guidance, appended without rewriting `contract`. Editing a
  // contract mid-run invalidates the cache key of every stage that
  // already consumed it, so one added instruction re-ran a whole order's
  // chain on a resumed run. `brief()` excludes these; `briefFull()`
  // includes them, and only stages that still need to run get the latter.
  addenda: { type: 'array', items: { type: 'string' } },
}

const PLAN_SCHEMA = {
  type: 'object',
  required: ['phase', 'gate', 'waves'],
  properties: {
    phase: { type: 'integer' },
    gate: { type: 'string' },
    waves: {
      type: 'array',
      items: {
        type: 'object',
        required: ['wave', 'orders'],
        properties: {
          wave: { type: 'integer' },
          orders: {
            type: 'array',
            items: {
              type: 'object',
              required: ['id', 'title', 'src_files', 'test_files', 'contract', 'acceptance', 'seams'],
              properties: ORDER_PROPS,
            },
          },
        },
      },
    },
    shared_files_needed: { type: 'array', items: { type: 'string' } },
    notes: { type: 'array', items: { type: 'string' } },
  },
}

const AUDIT_SCHEMA = {
  type: 'object',
  required: ['contradictions'],
  properties: {
    spec: { type: 'string' },
    claims_checked: { type: 'integer' },
    contradictions: {
      type: 'array',
      items: {
        type: 'object',
        required: ['kind', 'spec_ref', 'evidence'],
        properties: {
          severity: { enum: ['blocker', 'major', 'minor'] },
          kind: {
            enum: [
              'forbidden-by-gate',
              'contradicts-decisions',
              'claim-already-false',
              'count-wrong',
              'breaks-existing-test',
              'internally-inconsistent',
              'unsatisfiable-test',
            ],
          },
          spec_ref: { type: 'string' },
          evidence: { type: 'string' },
          consequence: { type: 'string' },
          suggestion: { type: ['string', 'null'] },
        },
      },
    },
    known_bug_pinned_tests: { type: 'array', items: { type: 'string' } },
    confirmed: { type: 'array', items: { type: 'string' } },
    blocked_on: { type: 'array', items: { type: 'string' } },
  },
}

const INTEGRATION_SCHEMA = {
  type: 'object',
  required: ['orders', 'merge_order'],
  properties: {
    base: { type: 'string' },
    baseline_failures: { type: 'array', items: { type: 'string' } },
    orders: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'standalone'],
        properties: {
          id: { type: 'string' },
          branch: { type: 'string' },
          standalone: { enum: ['green', 'red'] },
          result: { type: 'string' },
          needs: { type: 'array', items: { type: 'string' } },
          reason: { type: 'string' },
          own_defect: { type: ['string', 'null'] },
        },
      },
    },
    merge_order: { type: 'array', items: { type: 'string' } },
    cycles: { type: 'array' },
    assembled: { type: 'object' },
    release_preconditions: { type: 'array' },
    findings: { type: 'array' },
  },
}

const GATE_SCHEMA = {
  type: 'object',
  required: ['file', 'pins'],
  properties: {
    file: { type: 'string' },
    pins: { type: 'array', items: { type: 'string' } },
    golden_values: { type: 'array' },
    read_source_tree: { type: 'boolean' },
    blocked_on: { type: 'array', items: { type: 'string' } },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  required: ['verdict', 'findings'],
  properties: {
    verdict: { enum: ['approve', 'fix_required'] },
    confidence: { enum: ['high', 'medium', 'low'] },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'claim'],
        properties: {
          severity: { enum: ['blocker', 'major', 'minor'] },
          file: { type: 'string' },
          line: { type: 'integer' },
          category: { type: 'string' },
          claim: { type: 'string' },
          evidence: { type: 'string' },
          fix_hint: { type: 'string' },
          pattern: { type: 'string' },
        },
      },
    },
    recomputed_vectors: { type: 'array' },
    praise: { type: 'array', items: { type: 'string' } },
  },
}

const MUTATION_SCHEMA = {
  type: 'object',
  required: ['pins_checked', 'restored_clean', 'findings'],
  properties: {
    order: { type: 'string' },
    pins_checked: { type: 'integer' },
    pins_holding: { type: 'integer' },
    restored_clean: { type: 'boolean' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['kind', 'test', 'pin'],
        properties: {
          severity: { enum: ['blocker', 'major', 'minor'] },
          kind: {
            enum: [
              'vacuous-test',
              'unimplemented-pin',
              'unmutatable-pin',
              'bug-pinned-test',
            ],
          },
          test: { type: 'string' },
          pin: { type: 'string' },
          mutant: { type: 'string' },
          observed: { type: 'string' },
          diagnosis: { type: 'string' },
        },
      },
    },
  },
}

const SEAM_SCHEMA = {
  type: 'object',
  required: ['findings'],
  properties: {
    wave: { type: 'integer' },
    seams_audited: { type: 'array' },
    tests_written: { type: 'array', items: { type: 'string' } },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['kind', 'claim'],
        properties: {
          severity: { enum: ['blocker', 'major', 'minor'] },
          kind: { type: 'string' },
          sides: { type: 'array', items: { type: 'string' } },
          claim: { type: 'string' },
          evidence: { type: 'string' },
          owner_hint: { type: 'string' },
        },
      },
    },
  },
}

const SWEEP_SCHEMA = {
  type: 'object',
  required: ['class', 'instances'],
  properties: {
    source_finding: { type: 'string' },
    class: { type: 'string' },
    searches_run: { type: 'array', items: { type: 'string' } },
    instances: {
      type: 'array',
      items: {
        type: 'object',
        required: ['file', 'verdict'],
        properties: {
          file: { type: 'string' },
          line: { type: 'integer' },
          verdict: { enum: ['same-defect', 'shape-only', 'false-positive'] },
          reason: { type: 'string' },
          severity: { enum: ['blocker', 'major', 'minor'] },
        },
      },
    },
    gate_added: { type: ['string', 'null'] },
    gate_proof: { type: 'string' },
  },
}

// ------------------------------------------------------------------ roles
// Role definitions live in .claude/agents/*.md and are loaded by the agent
// itself as its first act: a mid-session registry snapshot does not include
// agent files written in the same session, so agentType is unreliable here.

const ROLE = (name) =>
  `First: Read \`.claude/agents/${name}.md\` in the current working directory and adopt its body as your role instructions (ignore the frontmatter). Then read \`.claude/loop.profile.yml\` as that role instructs. Obey the ownership limits exactly — other agents are editing OTHER files concurrently.`

// A transient API failure is not a result. On one fix-release wave 11 of 36
// agents died on `529 Overloaded` and 5 of 6 on the resumed run — including
// both mutation gates, the seam auditor and Ship. With no retry, that blip
// propagated straight into a half-built tree: implementations landed with no
// mutation proof, and the phase reported "zero unresolved findings" while 22
// tests were red, because the stages that would have found them never ran.
//
// Retry only what is plausibly transient. A schema-validation failure or a
// refusal is a real result and must not be re-rolled until it looks different.
const TRANSIENT = /\b(429|500|502|503|504|529)\b|overloaded|timed? ?out|econnreset|temporarily unavailable/i
const RETRIES = 3

async function run(prompt, opts) {
  let lastError
  for (let attempt = 1; attempt <= RETRIES; attempt++) {
    try {
      return await agent(prompt, opts)
    } catch (error) {
      lastError = error
      const message = String(error?.message ?? error)
      if (!TRANSIENT.test(message) || attempt === RETRIES) throw error
      const backoff = 5000 * 2 ** (attempt - 1)
      log(`${opts?.label ?? 'agent'}: transient failure (attempt ${attempt}/${RETRIES}), retrying in ${backoff / 1000}s — ${message.slice(0, 120)}`)
      await new Promise((resolve) => setTimeout(resolve, backoff))
    }
  }
  throw lastError
}

const CONTEXT = [
  'The repository is the current working directory.',
  `You are building phase ${phaseNo} of the specification named in .claude/loop.profile.yml.`,
  'All paths, commands and house rules come from .claude/loop.profile.yml. Never hardcode a path or a runner it does not declare.',
  'Never run git except read-only inspection.',
].join('\n')

// `brief` omits addenda so an order's cache key is stable when late guidance
// is appended; `briefFull` includes them, for stages that still have to run.
const brief = (order) => {
  const { addenda: _addenda, ...stable } = order ?? {}
  return JSON.stringify(stable, null, 2)
}
const briefFull = (order) => JSON.stringify(order, null, 2)
const isMustFix = (f) => f.severity === 'blocker' || f.severity === 'major'

// ------------------------------------------------------------- spec audit
// Stage 0. Reads the instructions before anyone follows them: a spec that
// contradicts a style gate, a pinned decision or an existing golden vector
// sends an agent to write code the project will reject.

phase('Audit')
const audit = await run(
  `${ROLE('spec-auditor')}\n\n${CONTEXT}\n\nAudit phase ${phaseNo} of the specification against what this project already enforces, per your output contract.`,
  { label: `audit:phase-${phaseNo}`, phase: 'Audit', schema: AUDIT_SCHEMA },
)
const blockingContradictions = (audit?.contradictions ?? []).filter(isMustFix)
if (blockingContradictions.length) {
  log(`SPEC AUDIT: ${blockingContradictions.length} blocking contradiction(s) — the spec disagrees with the repo`)
  for (const c of blockingContradictions) log(`  [${c.kind}] ${c.spec_ref}`)
}
if (audit?.known_bug_pinned_tests?.length) {
  log(`tests that assert the behaviour being removed: ${audit.known_bug_pinned_tests.join(', ')}`)
}

// -------------------------------------------------------------- interpret

phase('Interpret')
let plan = input?.plan
if (!plan) {
  plan = await run(
    `${ROLE('spec-interpreter')}\n\n${CONTEXT}\n\nDecompose phase ${phaseNo} into work orders per your output contract.${
      blockingContradictions.length
        ? `\n\nThe spec audit found contradictions between the spec and what this project enforces. Do NOT cut a work order that instructs an agent to do any of these — carry the auditor's suggestion, or record the conflict in notes:\n${brief(blockingContradictions)}`
        : ''
    }`,
    { label: `interpret:phase-${phaseNo}`, phase: 'Interpret', schema: PLAN_SCHEMA },
  )
  if (!plan) throw new Error('spec-interpreter returned nothing')
}
// Baseline the phase inherits. Every stage that reads a test result needs it:
// without it, "already broken" and "I broke it" are the same observation, and
// a mutation gate ends up reconstructing the distinction in prose.
const baselineFailures = input?.baselineFailures ?? plan.baseline_failures ?? []
const BASELINE = baselineFailures.length
  ? `\n\nBASELINE — these tests were ALREADY RED before this phase began. You did not cause them, and none is a mutation result:\n${baselineFailures.map((t) => `  - ${t}`).join('\n')}`
  : '\n\nBASELINE: the suite was fully green before this phase began, so ANY red test is caused by this phase.'
if (baselineFailures.length) {
  log(`baseline: ${baselineFailures.length} test(s) already red before this phase`)
}

const orderCount = plan.waves.reduce((n, w) => n + w.orders.length, 0)
log(`phase ${phaseNo}: ${plan.waves.length} wave(s), ${orderCount} order(s) — gate: ${plan.gate}`)
if (plan.shared_files_needed?.length) {
  log(`shared files the orchestrator must provide: ${plan.shared_files_needed.join(', ')}`)
}

// ------------------------------------------------------------- blind gate
// Runs BEFORE any implementation exists. That ordering is half the
// blindness guarantee; the role definition carries the other half.

phase('Gate')
const gate = await run(
  `${ROLE('gate-author')}\n\n${CONTEXT}\n\nWrite the acceptance gate for phase ${phaseNo}.\n\nDone-when, quoted from the spec:\n${plan.gate}\n\nThe work-order plan (for the public surface only — you may NOT read the source tree):\n${brief(plan.waves)}`,
  { label: `gate:phase-${phaseNo}`, phase: 'Gate', schema: GATE_SCHEMA },
)
if (gate?.read_source_tree) log(`WARNING: gate-author reports it read the source tree — the gate is not blind`)
if (gate?.blocked_on?.length) log(`gate-author blocked_on: ${gate.blocked_on.join('; ')}`)

// ------------------------------------------------------------------ build

async function buildOrder(order) {
  const spec = briefFull(order)

  const testReport = await run(
    `${ROLE('test-author')}\n\n${CONTEXT}${BASELINE}\n\nYour work order:\n${spec}`,
    { label: `tests:${order.id}`, phase: 'Build' },
  )
  if (testReport === null) return { order: order.id, skipped: 'test-author died' }

  let implReport = await run(
    `${ROLE('implementer')}\n\n${CONTEXT}${BASELINE}\n\nRed tests are in place (and in greenfield mode, stubs). Your work order:\n${spec}\n\nTest-author report:\n${testReport}`,
    { label: `impl:${order.id}`, phase: 'Build' },
  )

  let verdict = null
  const allFindings = []
  let priorMustFix = null
  let fixerReport = null
  for (let round = 1; round <= MAX_FIX_ROUNDS; round++) {
    // Round 1 is a full seven-lens review. Later rounds adjudicate only what
    // changed — re-running every lens over a module whose unreviewed parts did
    // not move is the largest avoidable cost in this loop. The delta reviewer
    // still owns regressions: a fix that breaks something is its finding.
    const prompt = round === 1
      ? `${ROLE('harsh-reviewer')}\n\n${CONTEXT}\n\nreviewMode: full — run all seven lenses.\n\nWork order:\n${spec}\n\nImplementer report:\n${implReport}`
      : `${ROLE('harsh-reviewer')}\n\n${CONTEXT}\n\nreviewMode: delta — round ${round}.\n\nA previous round of this review raised the findings below and they have since been fixed. Do NOT re-run all seven lenses over the whole work order; adjudicate the delta:\n\n1. For EACH prior finding: is it genuinely closed? Read the current code, not the fixer's claim. A finding "closed" by weakening a test, narrowing a docstring, or moving the problem is NOT closed.\n2. Did the fixes introduce anything new — a regression, a broken invariant elsewhere in the files they touched, a contradiction with the contract?\n3. Anything you deliberately deferred in an earlier round.\n\nPrior findings:\n${brief(priorMustFix)}\n\nWork order:\n${spec}\n\nFixer report:\n${fixerReport ?? '(none)'}`
    verdict = await run(prompt, { label: `review:${order.id}#${round}`, phase: 'Build', schema: VERDICT_SCHEMA })
    if (!verdict) break
    allFindings.push(...verdict.findings)
    if (verdict.verdict === 'approve') break
    const mustFix = verdict.findings.filter(isMustFix)
    if (!mustFix.length || round === MAX_FIX_ROUNDS) break
    priorMustFix = mustFix

    // Route by category. Test-quality findings go to a test-author (the
    // implementer is ownership-blocked on test files); everything else to
    // the implementer. Both may run in the same round.
    const testFix = mustFix.filter(f => (f.category || '').includes('test'))
    const codeFix = mustFix.filter(f => !(f.category || '').includes('test'))
    let testFixReport = null
    if (testFix.length) {
      testFixReport = await run(
        `${ROLE('test-author')}\n\n${CONTEXT}\n\nClose these test-quality findings on your work order:\n${spec}\n\nAdd ONLY the missing pinning tests, each with its \`pins:\` line. Never weaken or delete an existing test. Verify each behaviour against the CURRENT source first — if the source is actually wrong, report that instead of pinning the bug:\n${brief(testFix)}`,
        { label: `testfix:${order.id}#${round}`, phase: 'Build' },
      )
    }
    if (codeFix.length) {
      implReport = await run(
        `${ROLE('implementer')}\n\n${CONTEXT}\n\nFix these findings on your work order:\n${spec}\n\nAddress every blocker and major. If you dispute one, say so with evidence rather than complying:\n${brief(codeFix)}`,
        { label: `fix:${order.id}#${round}`, phase: 'Build' },
      )
    }
    fixerReport = [
      testFixReport ? `test-author:\n${testFixReport}` : null,
      codeFix.length ? `implementer:\n${implReport}` : null,
    ].filter(Boolean).join('\n\n') || null
  }
  return { order: order.id, verdict, findings: allFindings, implReport, spec }
}

// ------------------------------------------------------------------ prove

async function proveOrder(built) {
  if (!built || built.skipped) return built
  const mutation = await run(
    `${ROLE('mutation-gate')}\n\n${CONTEXT}${BASELINE}\n\nProve every \`pins:\` declaration in this work order's tests is falsifiable:\n${built.spec}\n\nRestore every mutation. Prove restoration with a read-only \`git diff --stat\` before you report.`,
    { label: `mutate:${built.order}`, phase: 'Prove', schema: MUTATION_SCHEMA },
  )
  if (!mutation) return { ...built, mutation: null }
  if (mutation.restored_clean === false) {
    log(`EMERGENCY: mutation-gate left ${built.order} dirty — inspect before continuing`)
  }
  const vacuous = mutation.findings.filter(f => f.kind === 'vacuous-test')
  const absent = mutation.findings.filter(f => f.kind !== 'vacuous-test')

  // A vacuous test is the test-author's to fix; an unimplemented pin is the
  // implementer's. Neither may fix the other's files.
  if (vacuous.length) {
    await run(
      `${ROLE('test-author')}\n\n${CONTEXT}\n\nThese tests are VACUOUS — a mutation that breaks the pinned behaviour left them green:\n${brief(vacuous)}\n\nWork order:\n${built.spec}\n\nFor each: the usual cause is a fixture that never reaches the branch the pin names. Fix the fixture (or the assertion) so the test discriminates. Do not change the pin to match what the test happens to do.`,
      { label: `unvacuum:${built.order}`, phase: 'Prove' },
    )
  }
  if (absent.length) {
    await run(
      `${ROLE('implementer')}\n\n${CONTEXT}\n\nThese pinned behaviours have no implementation to mutate — the tests pass for some other reason:\n${brief(absent)}\n\nWork order:\n${built.spec}\n\nImplement the pinned behaviour, or report with evidence that the pin is wrong.`,
      { label: `pinfix:${built.order}`, phase: 'Prove' },
    )
  }
  return { ...built, mutation }
}

// --------------------------------------------------- waves: build + prove
// pipeline(): an order can be mutating while a sibling is still under
// review. The wave boundary is the barrier, because the seam audit needs
// every order in the wave finished before it can look between them.

const results = []
const seamReports = []
for (const wave of plan.waves) {
  log(`wave ${wave.wave}: ${wave.orders.map(o => o.id).join(', ')}`)
  const waveResults = await pipeline(wave.orders, buildOrder, proveOrder)
  const done = waveResults.filter(Boolean)
  results.push(...done)

  phase('Seam')
  const declared = wave.orders.flatMap(o => (o.seams || []).map(s => `${o.id}: ${s}`))
  const seam = await run(
    `${ROLE('seam-auditor')}\n\n${CONTEXT}\n\nEvery order in wave ${wave.wave} is green. Audit the boundaries BETWEEN them, and between them and everything built in earlier phases.\n\nSeams declared by this wave's orders:\n${declared.length ? declared.join('\n') : '(none declared — build the inventory from profile.seam_patterns, and report the under-declaration as a finding)'}\n\nOrders:\n${brief(wave.orders)}`,
    { label: `seam:wave-${wave.wave}`, phase: 'Seam', schema: SEAM_SCHEMA },
  )
  if (seam) {
    seamReports.push(seam)
    const mustFix = (seam.findings || []).filter(isMustFix)
    if (mustFix.length) {
      log(`wave ${wave.wave}: ${mustFix.length} seam defect(s) — routing to owning orders`)
      // Group by owner so no two fixers touch the same files.
      const byOwner = new Map()
      for (const f of mustFix) {
        const owner = f.owner_hint || wave.orders[0].id
        if (!byOwner.has(owner)) byOwner.set(owner, [])
        byOwner.get(owner).push(f)
      }
      await parallel([...byOwner].map(([owner, findings]) => () => {
        const order = wave.orders.find(o => o.id === owner) || wave.orders[0]
        return agent(
          `${ROLE('implementer')}\n\n${CONTEXT}\n\nThe seam audit found defects at boundaries your order owns:\n${brief(findings)}\n\nWork order:\n${brief(order)}\n\nThese are contradictions between your module and another. Fix YOUR side only — if you believe the other side is wrong, report that with evidence instead of changing files you do not own.`,
          { label: `seamfix:${owner}`, phase: 'Seam' },
        )
      }))
    }
  }
}

// ---------------------------------------------- prove the gate itself too

phase('Prove')
let gateMutation = null
if (gate?.file) {
  gateMutation = await run(
    `${ROLE('mutation-gate')}\n\n${CONTEXT}${BASELINE}\n\nThe phase is green. Prove the PHASE ACCEPTANCE GATE can fail.\n\nGate file: ${gate.file}\nPins:\n${(gate.pins || []).join('\n')}\n\nFor each pin, construct a mutant in the implementation that violates it and require the gate to go red. A vacuous finding here is a BLOCKER: it means the phase's own definition of done cannot fail. Restore everything and prove it.`,
    { label: `mutate:gate`, phase: 'Prove', schema: MUTATION_SCHEMA },
  )
  if (gateMutation?.findings?.length) {
    log(`GATE PROBLEM: ${gateMutation.findings.length} pin(s) on the acceptance gate are not falsifiable`)
  }
}

// ------------------------------------------------------------------ sweep
// Every confirmed finding carrying a `pattern` is a sample of a class.
// Deduplicate by class so one sweeper owns each; they share a directory,
// so they must not run concurrently on the same pattern.

phase('Sweep')
const patterns = new Map()
for (const r of results) {
  for (const f of r.findings || []) {
    if (f.pattern && isMustFix(f)) patterns.set(f.pattern, f)
  }
}
let sweeps = []
if (patterns.size) {
  log(`sweeping ${patterns.size} defect class(es) tree-wide`)
  // Sequential: pattern-sweeper owns the style-gate directory exclusively,
  // so two of them concurrently would collide on the same files.
  for (const [, finding] of patterns) {
    const sweep = await run(
      `${ROLE('pattern-sweeper')}\n\n${CONTEXT}\n\nGeneralise this confirmed finding into a defect CLASS and search the whole tree for other instances:\n${brief(finding)}`,
      // Searching for a stated class and triaging hits against it is
      // mechanical; the judgement was spent naming the class upstream.
      { label: `sweep:${(finding.category || 'finding').slice(0, 20)}`, phase: 'Sweep', schema: SWEEP_SCHEMA, effort: 'medium' },
    )
    if (sweep) sweeps.push(sweep)
  }
}

// ------------------------------------------------------------------- ship

phase('Ship')
const shipReport = await run(
  `${ROLE('devops-docs')}\n\n${CONTEXT}\n\nPhase ${phaseNo} is built, reviewed, mutation-proven and seam-audited. Make it shippable and documented, then run every gate in profile.commands and report each with its literal output.\n\nPhase gate: ${plan.gate}\n\nWhat shipped:\n${brief(results.map(r => ({ id: r.order, title: r.spec ? JSON.parse(r.spec).title : r.order })))}\n\nUnresolved findings that must be recorded honestly in the status file:\n${brief(collectUnresolved())}`,
  // Running gates and reporting their output faithfully is mechanical. Every
  // other stage keeps the session tier: choosing a mutant that violates
  // exactly one pin, or auditing a seam, is judgement and is not tiered down.
  { label: `ship:phase-${phaseNo}`, phase: 'Ship', effort: 'medium' },
)

// -------------------------------------------------------------- integrate
// Stage 8. Disjoint ownership says who may WRITE; it says nothing about what
// compiles. Those are different graphs, and v2 assumed they matched: seven
// orders once merged with zero conflicts while three were red in isolation,
// because one order's tests called a signature another order had changed.
// This stage computes the dependency graph instead of inferring it, and owns
// the assertion-before-publish rule that a false-success release step needs.

phase('Integrate')
const allOrders = plan.waves.flatMap((w) => w.orders)
const integration = await run(
  `${ROLE('integrator')}\n\n${CONTEXT}${BASELINE}\n\nPhase ${phaseNo} is built and shipped. Determine whether each order can be delivered INDEPENDENTLY, and report the real dependency graph per your output contract.\n\nOrders:\n${brief(allOrders.map((o) => ({ id: o.id, src_files: o.src_files, test_files: o.test_files })))}`,
  { label: `integrate:phase-${phaseNo}`, phase: 'Integrate', schema: INTEGRATION_SCHEMA },
)
if (integration?.cycles?.length) {
  log(`DECOMPOSITION PROBLEM: ${integration.cycles.length} cycle(s) — orders that cannot be merged in any order should have been one order`)
}
const notStandalone = (integration?.orders ?? []).filter((o) => o.standalone === 'red')
if (notStandalone.length) {
  log(`${notStandalone.length} order(s) are red in isolation — ownership was disjoint, delivery is not: ${notStandalone.map((o) => o.id).join(', ')}`)
}
for (const o of (integration?.orders ?? []).filter((o) => o.own_defect)) {
  log(`ORDER DEFECT hidden by the assembled tree — ${o.id}: ${o.own_defect}`)
}

// ------------------------------------------------------------------ report

function collectUnresolved() {
  const out = []
  for (const r of results) {
    const stuck = (r.verdict?.verdict === 'fix_required' ? r.verdict.findings : []).filter(isMustFix)
    if (stuck.length) out.push({ order: r.order, kind: 'review', findings: stuck })
    const mut = (r.mutation?.findings || []).filter(f => (f.severity ?? 'major') !== 'minor')
    if (mut.length) out.push({ order: r.order, kind: 'mutation', findings: mut })
  }
  for (const s of seamReports) {
    const stuck = (s.findings || []).filter(isMustFix)
    if (stuck.length) out.push({ wave: s.wave, kind: 'seam', findings: stuck })
  }
  if (gateMutation?.findings?.length) out.push({ kind: 'gate-mutation', findings: gateMutation.findings })
  const specBlocking = (audit?.contradictions ?? []).filter(isMustFix)
  if (specBlocking.length) out.push({ kind: 'spec-contradiction', findings: specBlocking })
  return out
}

const unresolved = collectUnresolved()
return {
  phase: phaseNo,
  gate: plan.gate,
  spec_audit: audit
    ? {
        claims_checked: audit.claims_checked ?? null,
        contradictions: (audit.contradictions ?? []).length,
        blocking: (audit.contradictions ?? []).filter(isMustFix).length,
        known_bug_pinned_tests: audit.known_bug_pinned_tests ?? [],
      }
    : null,
  baseline_failures: baselineFailures,
  gate_test: gate?.file ?? null,
  gate_falsifiable: gateMutation ? (gateMutation.findings || []).length === 0 : null,
  orders: results.map(r => ({
    id: r.order,
    verdict: r.skipped ? 'skipped' : (r.verdict?.verdict ?? 'no-verdict'),
    findings: (r.findings || []).length,
    pins_checked: r.mutation?.pins_checked ?? null,
    pins_holding: r.mutation?.pins_holding ?? null,
    restored_clean: r.mutation?.restored_clean ?? null,
  })),
  seams: seamReports.map(s => ({
    wave: s.wave,
    audited: (s.seams_audited || []).length,
    tests_written: s.tests_written || [],
    findings: (s.findings || []).length,
  })),
  sweeps: sweeps.map(s => ({
    class: s.class,
    same_defect: (s.instances || []).filter(i => i.verdict === 'same-defect').length,
    gate_added: s.gate_added ?? null,
  })),
  unresolved,
  ship: shipReport,
  integration: integration
    ? {
        merge_order: integration.merge_order ?? [],
        not_standalone: (integration.orders ?? []).filter((o) => o.standalone === 'red').map((o) => o.id),
        cycles: integration.cycles ?? [],
        own_defects: (integration.orders ?? []).filter((o) => o.own_defect).map((o) => ({ id: o.id, defect: o.own_defect })),
        assembled: integration.assembled ?? null,
      }
    : null,
  shared_files_needed: plan.shared_files_needed ?? [],
  notes: plan.notes ?? [],
}
