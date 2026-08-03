"""Audit: append-only token-mint log (SPEC §7.2 — the gap Vezgo shipped).

Pinned record shape (docs/DECISIONS.md "Audit record shape"): seq is
per-project from 1, event is exactly "token.minted", append-only with no
delete/update/clear, and an unknown project reads as () so the log is not
a tenant-existence probe surface.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from auradefi.clock import FrozenClock
from auradefi.tenancy.audit import AuditLog, AuditRecord

T0 = 1_767_225_600_000  # 2026-01-01T00:00:00Z

A = "proj_a"
B = "proj_b"


def mint(log, clock, project_id=A, user="user-1", key_id="key_0a1b2c3d", ip="203.0.113.7"):
    return log.record_token_mint(project_id, user, key_id, ip, clock)


# -------------------------------------------------------------- record shape


def test_record_token_mint_returns_the_pinned_record_shape():
    log = AuditLog()
    record = mint(log, FrozenClock(T0))
    assert record == AuditRecord(
        seq=1,
        event="token.minted",
        project_id=A,
        external_user_id="user-1",
        key_id="key_0a1b2c3d",
        ip="203.0.113.7",
        at_ms=T0,
    )


def test_event_is_exactly_token_minted():
    record = mint(AuditLog(), FrozenClock(T0))
    assert record.event == "token.minted"


def test_audit_record_is_frozen():
    record = mint(AuditLog(), FrozenClock(T0))
    with pytest.raises(FrozenInstanceError):
        record.seq = 99
    with pytest.raises(FrozenInstanceError):
        record.ip = "198.51.100.1"


# ------------------------------------------------------- sequence and order


def test_three_mints_yield_seq_1_2_3_with_clock_timestamps_in_order():
    clock = FrozenClock(T0)
    log = AuditLog()
    first = mint(log, clock, user="user-1", key_id="key_1")
    clock.advance(1_000)
    second = mint(log, clock, user="user-2", key_id="key_1", ip="203.0.113.8")
    clock.advance(41)
    third = mint(log, clock, user="user-1", key_id="key_2", ip="198.51.100.9")
    assert (first.seq, second.seq, third.seq) == (1, 2, 3)
    assert (first.at_ms, second.at_ms, third.at_ms) == (T0, T0 + 1_000, T0 + 1_041)
    assert log.entries(A) == (first, second, third)


def test_seq_is_per_project_each_starting_at_1():
    clock = FrozenClock(T0)
    log = AuditLog()
    a_first = mint(log, clock, project_id=A)
    b_first = mint(log, clock, project_id=B, user="user-9")
    a_second = mint(log, clock, project_id=A)
    assert (a_first.seq, a_second.seq) == (1, 2)
    assert b_first.seq == 1
    assert log.entries(A) == (a_first, a_second)
    assert log.entries(B) == (b_first,)


# --------------------------------------------------------------- tenant scope


def test_entries_are_project_scoped_and_never_leak():
    clock = FrozenClock(T0)
    log = AuditLog()
    mint(log, clock, project_id=A)
    mint(log, clock, project_id=B, user="user-9", key_id="key_b")
    assert all(record.project_id == A for record in log.entries(A))
    assert all(record.project_id == B for record in log.entries(B))


def test_unknown_project_yields_empty_tuple_not_an_error():
    log = AuditLog()
    mint(log, FrozenClock(T0), project_id=A)
    empty = log.entries("proj_never_seen")
    assert empty == ()
    assert isinstance(empty, tuple)


def test_state_lives_on_the_instance_not_the_class():
    clock = FrozenClock(T0)
    first_log = AuditLog()
    second_log = AuditLog()
    mint(first_log, clock)
    assert second_log.entries(A) == ()
    assert mint(second_log, clock).seq == 1  # sequences independent per instance


# ---------------------------------------------------------------- append-only


def test_entries_returns_a_tuple_with_stable_repeated_reads():
    clock = FrozenClock(T0)
    log = AuditLog()
    record = mint(log, clock)
    first_read = log.entries(A)
    second_read = log.entries(A)
    assert isinstance(first_read, tuple)
    assert first_read == second_read == (record,)


def test_audit_log_exposes_no_mutation_surface():
    log = AuditLog()
    for name in ("remove", "delete", "clear", "update"):
        assert not hasattr(log, name), f"append-only: AuditLog must not expose {name}()"
