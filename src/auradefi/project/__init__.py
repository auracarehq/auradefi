"""Pure output projections: internal model -> wire format (SPEC §3.3, §6).

Every module in this package is a pure function of its inputs — no I/O,
no DB, no framework, mechanically enforced by tests/style/test_layering.py.
A host takes only the shape it can use; a consumer wanting the richer
native model bypasses projection entirely.
"""
