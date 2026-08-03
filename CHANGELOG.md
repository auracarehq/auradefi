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
