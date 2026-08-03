# Changelog

All notable changes to auradefi. Format follows Keep a Changelog; versions
follow SemVer once past 1.0.

## [Unreleased]

## [0.1.0] — in progress

### Added
- Repository scaffold: spec (`docs/SPEC.md`), Apache-2.0 licence, packaging
  (hatchling, `py.typed`), CI, Docker (test + runtime stages).
- Foundation modules: `errors` (single exception taxonomy), `clock`
  (ms-epoch Clock port), `config` (frozen Settings, env loader).
- Style gates as tests: size caps (300 soft / 400 hard, no allowlist),
  structure, placement (tests mirror source; tables only in models.py),
  layering (acyclic domain graph; no web framework outside `api/`; no ORM
  outside `ledger/backends/`; HTTP clients only in I/O domains).
- Cassette harness (`auradefi.testing.cassettes`) — recorded-HTTP replay
  with hard-failing misses; the suite runs fully offline.
- **Phase 0 foundation domains** (built test-first by the agent loop):
  - `money/` — `Quantity` (arbitrary-precision int + decimals, exact
    string rendering, never scientific notation), `Money` (Decimal +
    currency), and the pinned four-field wire form with a strict ASCII
    grammar (rule #2: a raw amount is never a JSON number).
  - `chains/` — CAIP-2 registry (no vendor name zoo), EVM/Bitcoin/Solana
    family helpers, seeded with Ethereum/Polygon/Base/BTC/SOL.
  - `assets/` — CAIP-19 parse/canonicalize, chain-agnostic `Asset` with
    per-chain `Implementation` (decimals on the implementation),
    deterministic permanently-stable asset ids (rule #3), both-ways
    registry lookup, groups with decimals-equality enforcement and a
    `single` fallback, additive-never-destructive spam scoring (rule #9).
  - `ledger/` — `LedgerPort` protocol, deterministic transaction ids,
    last-modified cursor tokens, upsert diffing, reorg planning
    (removed + re-added, SPEC §6.4), and a tenant-isolated `MemoryLedger`
    with resurrection semantics and pagination.
  - SPEC §13 contract tests: reorg removed+re-added with monotonic
    cursor, upsert idempotence, tenant isolation that actively tries to
    leak. 664 tests, all offline.
