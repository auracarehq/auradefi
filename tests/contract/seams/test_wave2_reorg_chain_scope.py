"""SEAM AUDIT — wave 0.1.1-wave2: what feeds ``plan_reorg`` its stored view.

Order ``ledger-reorg``'s declared seam names the write side of
``plan_reorg`` — "the ledger upsert path … which you do NOT own". This
file audits the READ side, which nobody named: ``plan_reorg``'s first
argument is called ``existing_for_chain``, and the port a host is given
(``src/auradefi/ledger/port.py``) declares no chain-scoped read at all.
Its only enumeration is ``sync(tenant_id, cursor=None)``, which is
TENANT-wide by design ("no call may read or write across tenants" — the
scope rule is per tenant, never per chain).

``plan_reorg`` neither filters by ``chain_id`` nor validates that its
input holds one chain, so the value a host can actually obtain removes
the wrong rows. Order ``embed-ids-loop`` #26 makes this the normal case
rather than an exotic one: chain-scoped connection ids exist precisely so
that ONE address can be watched on eip155:1 and eip155:137 at once, and
both chains' transactions land in ONE tenant's ledger.
"""

from __future__ import annotations

import pytest

from auradefi.errors import ValidationError
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.ledger.models import Direction, Entry, LedgerTransaction
from auradefi.ledger.port import LedgerPort
from auradefi.ledger.reorg import plan_reorg
from auradefi.money.quantity import Quantity

TENANT = "usr_seam_reorg"
NATIVE = "eip155:1/slip44:60"


def _txn(txn_id: str, chain_id: str, block: int) -> LedgerTransaction:
    """One stored transaction on ``chain_id`` at ``block``."""
    return LedgerTransaction(
        id=txn_id,
        chain_id=chain_id,
        tx_hash="0x" + txn_id,
        account_id="conn_multi",
        block_number=block,
        initiated_at=1_700_000_000_000,
        confirmed_at=1_700_000_000_000,
        entries=(Entry(NATIVE, Quantity(1, 18), Direction.IN),),
    )


def test_the_declared_port_offers_no_chain_scoped_read():
    """``plan_reorg`` asks for one chain; the port can only give a tenant.

    Pinning it makes the gap explicit rather than leaving each host to
    discover it: the argument name promises a filter that no declared
    method can perform.
    """
    surface = {name for name in dir(LedgerPort) if not name.startswith("_")}
    assert surface == {"upsert", "sync", "get", "mark_removed"}, (
        "the declared LedgerPort surface changed; re-check whether a "
        f"chain-scoped read now exists: {sorted(surface)}"
    )


def test_a_mainnet_reorg_does_not_orphan_a_polygon_transaction():
    """The stored view a host CAN obtain spans every chain in the tenant.

    ``sync(tenant_id, None)`` is the only enumeration ``LedgerPort``
    declares. Feeding it to ``plan_reorg`` for a mainnet fork USED TO put
    an untouched Polygon transaction into ``remove_ids``, and the backend
    marked it removed — a row still canonical on its own chain vanished
    from the host's ledger with no error anywhere. #26 made that the
    normal case, not an exotic one, by letting one address be watched on
    two chains into one tenant.

    ``plan_reorg`` now refuses a multi-chain view, so the wrong call is
    loud and the right one is unchanged.
    """
    ledger = MemoryLedger()
    mainnet = _txn("mainnet_row", "eip155:1", 200)
    polygon = _txn("polygon_row", "eip155:137", 500)
    ledger.upsert(TENANT, [mainnet, polygon])

    stored_view = [
        event.transaction for event in ledger.sync(TENANT, None, 1000).events
    ]
    assert {row.chain_id for row in stored_view} == {"eip155:1", "eip155:137"}

    # The finding was real and is now closed by REFUSING the mixed view.
    # Refusing rather than filtering is deliberate: a filter would let a
    # caller keep passing the tenant-wide view and quietly act on half of
    # it, which is the same silent-partial-action shape as the defect.
    with pytest.raises(ValidationError) as excinfo:
        plan_reorg(stored_view, [mainnet], from_block=150)
    assert "eip155:137" in str(excinfo.value)

    # And the correctly-scoped call a host must now make still works: the
    # Polygon row is untouched because it was never in the diff.
    plan = plan_reorg(
        [row for row in stored_view if row.chain_id == "eip155:1"],
        [mainnet],
        from_block=150,
    )
    assert "polygon_row" not in plan.remove_ids
    assert plan.remove_ids == ()
