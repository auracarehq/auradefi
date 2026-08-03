export const meta = {
  name: 'phase-build',
  description: 'Build one auradefi SPEC phase: interpret -> waves of (test-author -> implementer -> harsh-review fix loop <=3)',
  whenToUse: 'Invoke with args {phase: N} (optionally a pre-validated {plan}) after the orchestrator prepared shared files. Orchestrator integrates, runs the release gate, and commits after it returns.',
  phases: [
    { title: 'Interpret', detail: 'spec-interpreter emits disjoint work orders' },
    { title: 'Build', detail: 'per order: failing tests first, then implementation' },
    { title: 'Review', detail: 'harsh review with bounded fix loop' },
  ],
}

const input = typeof args === 'string' ? JSON.parse(args) : args
const phaseNo = input?.phase
if (phaseNo === undefined || phaseNo === null) throw new Error('args.phase is required')

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
              required: ['id', 'title', 'src_files', 'test_files', 'contract', 'acceptance'],
              properties: {
                id: { type: 'string' },
                title: { type: 'string' },
                src_files: { type: 'array', items: { type: 'string' } },
                test_files: { type: 'array', items: { type: 'string' } },
                contract: { type: 'string' },
                acceptance: { type: 'array', items: { type: 'string' } },
                depends_on: { type: 'array', items: { type: 'string' } },
              },
            },
          },
        },
      },
    },
    shared_files_needed: { type: 'array', items: { type: 'string' } },
    notes: { type: 'array', items: { type: 'string' } },
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
        },
      },
    },
    recomputed_vectors: { type: 'array' },
    praise: { type: 'array', items: { type: 'string' } },
  },
}

// Role definitions live in .claude/agents/*.md; agents load them as their
// first act (mid-session registry snapshots make agentType unreliable).
const ROLE = (name) =>
  `First: Read the file .claude/agents/${name}.md in the current working directory and adopt its body as your role instructions (ignore the frontmatter). Obey its ownership limits exactly.`

let plan = input?.plan
if (!plan) {
  plan = await agent(
    `${ROLE('spec-interpreter')}\n\nDecompose auradefi SPEC phase ${phaseNo} into work orders per that output contract. Work from the repository in the current working directory.`,
    { label: `interpret:phase-${phaseNo}`, phase: 'Interpret', schema: PLAN_SCHEMA },
  )
  if (!plan) throw new Error('spec-interpreter returned nothing')
}
const orderCount = plan.waves.reduce((n, w) => n + w.orders.length, 0)
log(`phase ${phaseNo}: ${plan.waves.length} wave(s), ${orderCount} work order(s); gate: ${plan.gate}`)

const COMMON = [
  'Repository: the current working directory. Test runner: .venv/bin/pytest (never bare pytest).',
  `You are building SPEC phase ${phaseNo}. Phase gate: ${plan.gate}`,
  'Ground truth: docs/SPEC.md, docs/DECISIONS.md (pinned algorithms), tests/style/ (mechanical law).',
  'Never run git. Never touch files outside your ownership. Other agents are editing OTHER files concurrently.',
].join('\n')

async function buildOrder(order) {
  const spec = JSON.stringify(order, null, 2)

  const testReport = await agent(
    `${ROLE('test-author')}\n\n${COMMON}\n\nYou are the test-author. Your work order:\n${spec}`,
    { label: `tests:${order.id}`, phase: 'Build' },
  )
  if (testReport === null) return { order: order.id, skipped: 'test-author died' }

  let implReport = await agent(
    `${ROLE('implementer')}\n\n${COMMON}\n\nYou are the implementer. Stubs and red tests are in place. Your work order:\n${spec}\n\nTest-author report:\n${testReport}`,
    { label: `impl:${order.id}`, phase: 'Build' },
  )

  let verdict = null
  for (let round = 1; round <= 3; round++) {
    verdict = await agent(
      `${ROLE('harsh-reviewer')}\n\n${COMMON}\n\nReview round ${round} for this work order:\n${spec}\n\nImplementer report:\n${implReport}`,
      { label: `review:${order.id}#${round}`, phase: 'Review', schema: VERDICT_SCHEMA },
    )
    if (!verdict || verdict.verdict === 'approve') break
    const mustFix = verdict.findings.filter(f => f.severity !== 'minor')
    if (!mustFix.length || round === 3) break
    // Route by category: test-quality findings go to a test-author (the
    // implementer is ownership-blocked on test files), the rest to the
    // implementer. Both may run in the same round.
    const testFix = mustFix.filter(f => (f.category || '').includes('test'))
    const codeFix = mustFix.filter(f => !(f.category || '').includes('test'))
    if (testFix.length) {
      await agent(
        `${ROLE('test-author')}\n\n${COMMON}\n\nYou are the test-author, closing test-quality review findings on your work order:\n${spec}\n\nAdd ONLY the missing pinning tests described below. Never weaken or delete an existing test. Verify each pinned behavior against the CURRENT source first; if the source is actually wrong, report it instead of pinning the bug:\n${JSON.stringify(testFix, null, 2)}`,
        { label: `testfix:${order.id}#${round}`, phase: 'Review' },
      )
    }
    if (codeFix.length) {
      implReport = await agent(
        `${ROLE('implementer')}\n\n${COMMON}\n\nYou are the implementer, fixing review findings on your work order:\n${spec}\n\nAddress every blocker/major below; if you dispute one, say so in your report with evidence:\n${JSON.stringify(codeFix, null, 2)}`,
        { label: `fix:${order.id}#${round}`, phase: 'Review' },
      )
    }
  }
  return { order: order.id, verdict, implReport }
}

const results = []
for (const wave of plan.waves) {
  log(`wave ${wave.wave}: ${wave.orders.map(o => o.id).join(', ')}`)
  // Barrier between waves is intentional: waves encode dependency ordering.
  const waveResults = await parallel(wave.orders.map(o => () => buildOrder(o)))
  results.push(...waveResults.filter(Boolean))
}

const unresolved = results.filter(
  r => r.verdict && r.verdict.verdict === 'fix_required'
    && r.verdict.findings.some(f => f.severity !== 'minor'),
)
return {
  phase: phaseNo,
  gate: plan.gate,
  orders: results.map(r => ({
    id: r.order,
    verdict: r.skipped ? 'skipped' : (r.verdict?.verdict ?? 'no-verdict'),
    findings: r.verdict?.findings?.length ?? 0,
  })),
  unresolved: unresolved.map(r => ({ id: r.order, findings: r.verdict.findings })),
  shared_files_needed: plan.shared_files_needed ?? [],
  notes: plan.notes ?? [],
}
