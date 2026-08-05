"""One tick's budget dispatch and per-connection containment (SPEC §8).

Split out of ``embed/facade.py`` when that module reached the 400-line hard
cap: the containment RELEASE_0.1.1 §5 #24 asked for needed a paragraph of
reasoning, and the house rule is to split the module rather than to leave
the reasoning out to make it fit.

Both public entry points — ``Auradefi.sync`` and ``UserHandle.sync`` —
funnel through :func:`run_sync`, so they cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence

from auradefi.embed.models import ConnectionRecord, ConnectionSyncReport, SyncReport
from auradefi.embed.state import SyncStatePort
from auradefi.embed.sync import SyncEngine
from auradefi.errors import AuradefiError, CassetteError, ValidationError


def run_sync(
    engine: SyncEngine,
    sync_state: SyncStatePort,
    pairs: Sequence[tuple[str, ConnectionRecord]],
    budget: int,
) -> SyncReport:
    """Spend one shared budget over ``pairs``; the pinned algorithm.

    Connections are visited in order until the budget runs out; a
    throttled connection spends nothing, so the next one still gets the
    full remainder. Connections past the exhausted budget are not visited
    and contribute no row.

    CONTAINMENT (RELEASE_0.1.1 §5 #24): an ``AuradefiError`` from ONE
    connection — an unservable address, a malformed row, a chain the
    registry never held — becomes that connection's
    ``ConnectionSyncReport.failure`` row and the loop continues, so one
    bad row cannot starve its siblings. It costs ONE unit of the budget:
    the pages it really spent are not observable from here, and one unit
    is the least that still guarantees termination instead of N broken
    connections issuing N requests against a budget of 1. Anything NOT an
    ``AuradefiError`` is a bug in a host's own code and propagates
    untouched.

    ``CassetteError`` is EXEMPT and propagates. It is test-harness
    signalling, not a product failure: its whole purpose is that the
    offline guarantee (SPEC §13) "fails loudly, never silently", and a
    containment that files it as one connection's failure row makes it
    fail silently — a window the fixture never captured would read as an
    upstream outage and land a partial ingest with nothing raised. That
    is the difference between a red suite and a green one that lost rows.
    """
    if budget < 1:
        raise ValidationError(f"budget must be >= 1, got {budget}")
    remaining = budget
    rows: list[ConnectionSyncReport] = []
    for tenant_id, connection in pairs:
        if remaining < 1:
            break
        try:
            row = engine.sync_connection(tenant_id, connection, remaining)
        except CassetteError:
            raise
        except AuradefiError:
            stored = sync_state.get_state(tenant_id, connection.id)
            rows.append(ConnectionSyncReport.failure(connection.id, stored))
            remaining -= 1
            continue
        remaining -= row.pages_fetched
        rows.append(row)
    return SyncReport.assemble(rows)
