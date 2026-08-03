"""Positions: address + chain → Position[] (SPEC §3.3, §4.3, §5).

Fixture-driven in Phase 4: chain reads arrive through
positions.protocol.ContractReader; no HTTP client may ever appear in
this domain.

Docstring-only __init__: import concrete modules.
"""
