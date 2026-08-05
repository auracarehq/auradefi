"""How do I answer "what did they make, and what tax lots are open"?

    pip install auradefi
    python examples/08_report_cost_basis_and_pnl.py

Cost basis is where crypto tools quietly disagree with each other. This
package's position is that there is no single right answer. There are four
legal ones, and the caller picks:

    fifo   oldest lot first        (default nearly everywhere)
    lifo   newest lot first
    hifo   most expensive first    (minimises a gain)
    acb    one pooled average cost (Canada)

Everything is computed from an event stream, at whatever instant you ask
about, so there is no pre-computed state to go stale and no "as of last
night's batch". Ask about a millisecond before a sale and the sale has not
happened yet.

Also here: `tax_lots[]` in Plaid's shape, straight out of the report, and
the flag that fires when a `Fraction` had to be rounded into `Money`. The
one place exactness cannot survive contact with a currency.
"""

from __future__ import annotations

from decimal import Decimal

from auradefi.accounting.lots import AcquisitionEvent, DisposalEvent, derive_events
from auradefi.accounting.pnl import pnl_at
from auradefi.ledger.models import Direction, Entry, LedgerTransaction, transaction_id
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

ETH = "eip155:1/slip44:60"
DAY = 86_400_000
T0 = 1_700_000_000_000
MARKS = {ETH: Money(Decimal("50"), "USD")}      # what it is worth today

# Three buys and one sale: 1 unit at 10, 1 at 30, 1 at 26, then one sold at 40.
TRADES = (
    AcquisitionEvent(T0 + 0 * DAY, ETH, Quantity(1, 0), Money(Decimal("10"), "USD"), "txn_b1"),
    AcquisitionEvent(T0 + 1 * DAY, ETH, Quantity(1, 0), Money(Decimal("30"), "USD"), "txn_b2"),
    AcquisitionEvent(T0 + 2 * DAY, ETH, Quantity(1, 0), Money(Decimal("26"), "USD"), "txn_b3"),
    DisposalEvent(T0 + 3 * DAY, ETH, Quantity(1, 0), Money(Decimal("40"), "USD"), "txn_s1"),
)

# ------------------------------------------------- 1. four methods, four answers
print("bought 1 @ 10, 1 @ 30, 1 @ 26; sold 1 @ 40; mark today 50\n")
print(f"  {'method':<6}{'realised':>12}{'unrealised':>14}   open lots")
reports = {}
for method in ("fifo", "lifo", "hifo", "acb"):
    report = pnl_at(TRADES, method, T0 + 3 * DAY, MARKS)
    reports[method] = report
    print(f"  {method:<6}{str(report.realized):>12}{str(report.unrealized):>14}"
          f"   {len(report.open_lots)}")

# Each is right, under its own rule: FIFO sells the 10, LIFO the 26, HIFO the 30.
assert [str(reports[method].realized) for method in ("fifo", "lifo", "hifo", "acb")] == [
    "30 USD", "14 USD", "10 USD", "18 USD"]
print("\n  FIFO sold the 10 (gain 30), LIFO the 26 (14), HIFO the 30 (10);")
print("  ACB pooled all three to an average 22 (18). Same trades, four legal answers.")

# ------------------------------------------------------- 2. any instant, exactly
# One millisecond before the sale, the sale has not happened. Nothing is
# pre-aggregated, so this is a question you can always ask.
before = pnl_at(TRADES, "fifo", T0 + 3 * DAY - 1, MARKS)
assert before.realized == Money(Decimal("0"), "USD")
assert len(before.open_lots) == 3
after = reports["fifo"]
print(f"\n1 ms before the sale: realised {before.realized}, {len(before.open_lots)} open lots")
print(f"1 ms after:           realised {after.realized}, {len(after.open_lots)} open lots")

# A year later with no further trades: realised is unchanged, unrealised moved.
later = pnl_at(TRADES, "fifo", T0 + 400 * DAY, {ETH: Money(Decimal("80"), "USD")})
assert later.realized == after.realized
assert later.unrealized.amount > after.unrealized.amount
print(f"400 days later at 80:  realised {later.realized} (unchanged), "
      f"unrealised {later.unrealized}")

# --------------------------------------------------------- 3. Plaid's tax_lots
# `open_lots` is already in Plaid's `tax_lots[]` shape, with a DETERMINISTIC
# `institution_lot_id`, so the same acquisition reports the same lot id
# across runs, across backends and across processes.
print("\nopen lots (Plaid tax_lots[] shape, FIFO):")
print(f"  {'institution_lot_id':<26}{'qty':>5}{'bought at':>11}"
      f"{'cost basis':>13}{'value now':>12}")
for lot in after.open_lots:
    print(f"  {lot.institution_lot_id:<26}{str(lot.quantity):>5}"
          f"{str(lot.purchase_price):>11}{str(lot.cost_basis):>13}"
          f"{str(lot.current_value):>12}")

repeated = pnl_at(TRADES, "fifo", T0 + 3 * DAY, MARKS)
assert [lot.institution_lot_id for lot in repeated.open_lots] == [
    lot.institution_lot_id for lot in after.open_lots]
assert after.open_lots[0].position_type == "LONG"
print("  ids are derived, so a second run reports the same lot ids")

# ------------------------------------------------- 4. when rounding happens
# A third of a unit has no exact decimal cost. The arithmetic stays in
# `Fraction` until the last step, and the report SAYS it rounded rather than
# hiding a cent.
thirds = (
    AcquisitionEvent(T0, ETH, Quantity(3, 0), Money(Decimal("10"), "USD"), "txn_t1"),
    DisposalEvent(T0 + DAY, ETH, Quantity(1, 0), Money(Decimal("5"), "USD"), "txn_t2"),
)
rounded = pnl_at(thirds, "fifo", T0 + DAY, MARKS)
print(f"\nsold 1 of a 3-unit lot bought for 10 (basis 10/3):")
print(f"  realised {rounded.realized}  flags={sorted(rounded.flags)}")
assert "rounded_basis" in rounded.flags, "a rounded boundary must be visible"

# ---------------------------------------- 5. straight from your ledger rows
# `derive_events` turns stored transactions into the event stream: IN is an
# acquisition, OUT a disposal, SELF nothing (moving your own coins is not
# income), and a reorged-away row nothing at all. Costs come out as None, 
# pricing is deliberately NOT the accounting layer's job, so you attach your
# own marks and keep one source of truth for prices.
def ledger_row(index: int, direction: Direction) -> LedgerTransaction:
    tx_hash = "0x" + f"{index:02x}" * 32
    return LedgerTransaction(
        id=transaction_id("eip155:1", tx_hash, "acct_1"), chain_id="eip155:1",
        tx_hash=tx_hash, account_id="acct_1", block_number=18_000_000 + index,
        initiated_at=T0 + index * DAY, confirmed_at=T0 + index * DAY + 500,
        entries=(Entry(asset_id=ETH, quantity=Quantity(10**18, 18),
                       direction=direction),),
    )


rows = [ledger_row(1, Direction.IN), ledger_row(2, Direction.OUT),
        ledger_row(3, Direction.SELF)]
events = derive_events(rows)
assert [type(event).__name__ for event in events] == ["AcquisitionEvent", "DisposalEvent"]
assert events[0].cost is None and events[1].proceeds is None
print(f"\nderive_events over {len(rows)} ledger rows -> "
      f"{[type(event).__name__.removesuffix('Event') for event in events]} "
      "(the SELF transfer is not income)")

print("\nOK: four methods, any instant, deterministic lot ids, honest rounding.")
