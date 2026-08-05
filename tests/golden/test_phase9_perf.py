"""Phase 9 perf gate. 50,000 events must replay in seconds (SPEC §11).

Correctness at 50,000 events is worthless if it takes a minute: the whole
claim in SPEC §9 is that incremental lot-tracking makes arbitrary-date PnL
*available* on an active wallet, where Zerion's pre-computed marks are
not. A quadratic engine, one that rescans every lot ever opened on every
disposal, passes the golden vectors and misses the point, so the budget
is a test.

This module duplicates the generator constants of
``tests/golden/test_phase9_pnl.py`` verbatim: the suite has no
cross-test imports and no ``tests/__init__.py``, and a shared helper would
be a hidden dependency between two gates that must fail independently.

``time.monotonic`` is read HERE and nowhere else in the accounting tree.
``accounting/`` is pure and takes no clock.
"""

from __future__ import annotations

import time
from decimal import Decimal

from auradefi.accounting.lots import AcquisitionEvent, DisposalEvent
from auradefi.accounting.pnl import pnl_at, process, report
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

T0 = 1_600_000_000_000
PAIRS = 25_000
ASSETS = tuple(f"eip155:1/erc20:0x{a:040x}" for a in range(5))

BUY_QUANTITY = Quantity(2, 0)
SELL_QUANTITY = Quantity(1, 0)
CHEAP_COST = Money(Decimal(20), "USD")
DEAR_COST = Money(Decimal(24), "USD")
SALE_PROCEEDS = Money(Decimal(15), "USD")
MARKS = {asset: Money(Decimal("20"), "USD") for asset in ASSETS}

MID = 1_600_750_599_999
END = 1_601_500_000_000

REALIZED_AT_END = Decimal("100000")
REALIZED_AT_MID = Decimal("50050")

#: Seconds for one 50,000-event replay plus one arbitrary-date query.
#: Not a machine-speed measurement: a shape check. The naive engine is
#: quadratic and lands three times over; anything O(open lots) a disposal
#: lands well under.
BUDGET_SECONDS = 10.0


def _events() -> tuple[AcquisitionEvent | DisposalEvent, ...]:
    """The pinned 50,000-event stream (constants duplicated on purpose)."""
    draw = 20_260_802
    events: list[AcquisitionEvent | DisposalEvent] = []
    for index in range(PAIRS):
        asset = ASSETS[index % 5]
        slot = T0 + index * 60_000
        cost = CHEAP_COST if (index // 5) % 2 == 0 else DEAR_COST
        draw = (1103515245 * draw + 12345) % 2**31
        events.append(
            AcquisitionEvent(
                slot + draw % 20_000,
                asset,
                BUY_QUANTITY,
                cost,
                f"txn_b{index:07d}",
            )
        )
        draw = (1103515245 * draw + 12345) % 2**31
        events.append(
            DisposalEvent(
                slot + 30_000 + draw % 20_000,
                asset,
                SELL_QUANTITY,
                SALE_PROCEEDS,
                f"txn_s{index:07d}",
            )
        )
    return tuple(events)


def test_a_fifty_thousand_event_replay_and_one_arbitrary_date_fit_the_budget():
    """Build the stream outside the clock; the engine is what is timed."""
    events = _events()
    assert len(events) == 50_000

    started = time.monotonic()
    full = report(process(events, "fifo"), END, MARKS)
    mid = pnl_at(events, "fifo", MID, MARKS)
    elapsed = time.monotonic() - started

    assert full.realized == Money(REALIZED_AT_END, "USD")
    assert full.realized.amount == REALIZED_AT_END
    assert mid.realized.amount == REALIZED_AT_MID
    assert len(full.open_lots) == 12_500
    assert elapsed < BUDGET_SECONDS, (
        f"50,000-event FIFO replay plus one arbitrary-date query took "
        f"{elapsed:.2f}s, over the {BUDGET_SECONDS}s budget: the engine is "
        f"rescanning closed lots"
    )
