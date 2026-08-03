"""Ledger backend implementations.

Only modules in this package may import an ORM (SPEC §3.2 layering gate).
memory.py backs the test suite; sqlmodel.py arrives in Phase 5.
"""
