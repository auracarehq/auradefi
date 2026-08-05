"""embed/dispatch.py — budget spending and per-connection containment.

The containment RELEASE_0.1.1 §5 #24 asked for is the subject here: one
failing connection must cost its siblings nothing but one budget unit, and
must be VISIBLE as a failure rather than reported as clean success. The
exemption for ``CassetteError`` is pinned too, because a containment that
swallows a fixture miss turns a red suite into a green one that lost rows.
"""

from __future__ import annotations

import pytest

from auradefi.embed.dispatch import run_sync
from auradefi.embed.models import ConnectionRecord, ConnectionSyncReport, SyncState
from auradefi.errors import CassetteMissError, SourceError, ValidationError

T0 = 1_700_000_000_000
TENANT = "usr_1e63721d071ea2d9"


def _connection(suffix: str, chain: str = "eip155:1") -> ConnectionRecord:
    return ConnectionRecord(
        id=f"conn_{suffix}",
        chain_id=chain,
        address="0x" + suffix[0] * 40,
        created_at_ms=T0,
    )


class _State:
    """The two SyncStatePort members dispatch itself touches."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, str]] = []

    def get_state(self, tenant_id: str, connection_id: str) -> SyncState:
        self.reads.append((tenant_id, connection_id))
        return SyncState()


class _Engine:
    """A SyncEngine stand-in: each connection id maps to a scripted outcome."""

    def __init__(self, script: dict[str, object]) -> None:
        self.script = script
        self.calls: list[str] = []

    def sync_connection(self, tenant_id, connection, remaining):  # noqa: ANN001
        self.calls.append(connection.id)
        outcome = self.script[connection.id]
        if isinstance(outcome, Exception):
            raise outcome
        return ConnectionSyncReport(
            connection_id=connection.id,
            no_op=False,
            pages_fetched=int(outcome),
            live_pages=int(outcome),
            backfill_pages=0,
            transactions_ingested=int(outcome),
            live_cursor=1,
            backfill_cursor=None,
            backfill_complete=False,
        )


def test_a_budget_below_one_is_refused():
    # pins: the guard is reached. A budget of 0 that silently did nothing
    #       would be indistinguishable from a tick with no connections.
    with pytest.raises(ValidationError):
        run_sync(_Engine({}), _State(), [], 0)


def test_no_connections_is_an_empty_report_not_an_error():
    report = run_sync(_Engine({}), _State(), [], 5)
    assert report.connections == ()
    assert report.pages_fetched == 0


def test_one_failing_connection_does_not_starve_its_siblings():
    # pins: RELEASE_0.1.1 §5 #24. Before the fix the exception escaped the
    #       loop, so every connection after the bad one was never visited —
    #       one unseeded chain starved the whole tick, forever.
    bad, good = _connection("bad"), _connection("good")
    engine = _Engine({"conn_bad": SourceError("upstream down"), "conn_good": 2})
    report = run_sync(engine, _State(), [(TENANT, bad), (TENANT, good)], 5)

    assert engine.calls == ["conn_bad", "conn_good"], "the sibling was visited"
    assert report.transactions_ingested == 2


def test_a_failed_connection_is_reported_as_failed_not_as_clean_success():
    # pins: the report-honesty half. A contained failure that reported
    #       no_op/clean would be the same defect class as #21 — a
    #       success-shaped report that is not true.
    bad = _connection("bad")
    engine = _Engine({"conn_bad": SourceError("upstream down")})
    report = run_sync(engine, _State(), [(TENANT, bad)], 5)

    assert [row.failed for row in report.connections] == [True]
    assert report.connections[0].connection_id == "conn_bad"


def test_a_contained_failure_costs_exactly_one_budget_unit():
    # pins: termination. Charging zero would let N broken connections issue
    #       N requests against a budget of 1.
    rows = [(TENANT, _connection(f"b{index}")) for index in range(4)]
    engine = _Engine({f"conn_b{index}": SourceError("down") for index in range(4)})
    report = run_sync(engine, _State(), rows, 2)

    assert engine.calls == ["conn_b0", "conn_b1"], "budget stopped the walk at 2"
    assert len(report.connections) == 2


def test_a_cassette_miss_propagates_and_is_never_filed_as_a_failure_row():
    # pins: the exemption. CassetteMissError exists so the offline
    #       guarantee fails LOUDLY; containment that files it as one
    #       connection's outage makes it fail silently, and a window the
    #       fixture never recorded then reads as an upstream problem while
    #       a partial ingest lands with nothing raised.
    bad, good = _connection("bad"), _connection("good")
    engine = _Engine({"conn_bad": CassetteMissError("not recorded"), "conn_good": 1})

    with pytest.raises(CassetteMissError):
        run_sync(engine, _State(), [(TENANT, bad), (TENANT, good)], 5)

    assert engine.calls == ["conn_bad"], "it stopped rather than continuing"
