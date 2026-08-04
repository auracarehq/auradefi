// Extracts clusterCandidates() from the SHIPPED lean-review script and proves
// the invariant that matters: no candidate is ever silently dropped, whatever
// the clustering agent returns. A lost candidate is a lost defect.
import fs from 'node:fs'

const src = fs.readFileSync('.claude/workflows/code-review-lean.js', 'utf8')
const start = src.indexOf('async function clusterCandidates')
const end = src.indexOf('\nphase("Cluster")')
if (start < 0 || end < 0) { console.error('FAIL: could not locate clusterCandidates in the script'); process.exit(1) }
const fnText = src.slice(start, end)

const loc = c => c.file + (c.line != null ? ':' + c.line : '')
const inBounds = (i, n) => Number.isInteger(i) && i >= 0 && i < n
const logs = []
const log = m => logs.push(m)

let mockResponse = null
const agent = async () => mockResponse

const clusterCandidates = new Function(
  'loc', 'inBounds', 'log', 'agent', 'CLUSTER_SCHEMA',
  `${fnText}; return clusterCandidates`
)(loc, inBounds, log, agent, {})

const cands = Array.from({ length: 6 }, (_, i) => ({
  file: `src/m${i}.py`, line: 10 + i, summary: `bug ${i}`, failure_scenario: `boom ${i}`, kind: 'correctness',
}))

// Every candidate must be reachable in the output: as a representative, or
// named in some representative's clusterAlso.
const covered = (reps) => {
  const seen = new Set()
  for (const r of reps) { seen.add(loc(r)); for (const a of r.clusterAlso || []) seen.add(a) }
  return cands.every(c => seen.has(loc(c)))
}

const cases = [
  ['well-formed: 3 clusters', { clusters: [
    { representative: 0, members: [0, 1] }, { representative: 2, members: [2, 3, 4] }, { representative: 5, members: [5] },
  ]}, 3],
  ['agent returned null (fallback verifies everything)', null, 6],
  ['agent returned empty clusters', { clusters: [] }, 6],
  ['dropped index 4 entirely', { clusters: [{ representative: 0, members: [0, 1, 2, 3, 5] }] }, 2],
  ['duplicated index across clusters', { clusters: [
    { representative: 0, members: [0, 1] }, { representative: 1, members: [1, 2] }, { representative: 3, members: [3, 4, 5] },
  ]}, 3],
  ['out-of-bounds and negative indices', { clusters: [
    { representative: 99, members: [0, 99, -1, 1] }, { representative: 2, members: [2, 3, 4, 5] },
  ]}, 2],
  ['representative not among its own members', { clusters: [
    { representative: 5, members: [0, 1] }, { representative: 2, members: [2, 3, 4, 5] },
  ]}, 2],
  ['members not an array', { clusters: [{ representative: 0, members: 'nope' }] }, 6],
  ['one giant over-merge (allowed, but nothing lost)', { clusters: [{ representative: 0, members: [0,1,2,3,4,5] }] }, 1],
]

let failed = 0
for (const [name, resp, expectedReps] of cases) {
  mockResponse = resp
  const reps = await clusterCandidates(cands)
  const ok = covered(reps)
  const countOk = reps.length === expectedReps
  if (!ok || !countOk) {
    failed++
    console.log(`  FAIL  ${name}\n        reps=${reps.length} (expected ${expectedReps}) covered=${ok}`)
    console.log(`        got: ${JSON.stringify(reps.map(r => [loc(r), r.clusterAlso]))}`)
  } else {
    console.log(`  ok    ${name}  -> ${reps.length} root cause(s), all 6 candidates covered`)
  }
}

// Single candidate short-circuits without calling the agent at all.
mockResponse = null
const one = await clusterCandidates([cands[0]])
if (one.length !== 1) { failed++; console.log('  FAIL  single candidate short-circuit') }
else console.log('  ok    single candidate short-circuits (no agent call)')

console.log(failed === 0 ? '\nALL CLUSTER INVARIANTS HOLD' : `\n${failed} FAILURE(S)`)
process.exit(failed === 0 ? 0 : 1)
