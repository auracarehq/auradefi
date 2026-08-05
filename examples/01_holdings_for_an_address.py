"""How do I get a priced portfolio for one address?

    pip install auradefi && python 01_holdings_for_an_address.py

No keys, no setup: this runs in the Sandbox environment, which replays a
recording bundled in the package. Every layer above the transport is the
production one, so what you see here is what live code does.

The three things worth noticing are the whole design:

* the total is exact. It is computed in `Decimal`, never a float: the
  comparison at the end shows the float answer already wrong at the 17th
  digit, and that error compounds across a portfolio;
* an asset nobody will price is **held, listed and named** in
  `report.unpriced`, never valued at zero;
* going live is one line: `Auradefi.from_env()` instead of
  `Auradefi.sandbox()`, with `AURADEFI_ETHERSCAN_API_KEY` in your
  environment. The last section does exactly that if you have a key set.
"""

from __future__ import annotations

import os
from decimal import Decimal

from auradefi import Auradefi
from auradefi.money.decimal_json import money_to_wire, quantity_to_wire
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

# ------------------------------------------------------------- the whole ask
aura = Auradefi.sandbox()
(report,) = aura.holdings()

print(f"holdings for {report.address} on {report.chain_id}\n")
print(f"  {'asset':<6}{'quantity':>12}{'price':>14}    value")
for holding in report.holdings:
    print(f"  {holding.symbol:<6}{str(holding.quantity):>12}"
          f"{str(holding.price):>14}    {holding.value}")
print(f"  {'TOTAL':<6}{'':>12}{'':>14}    {report.total_value}")

assert report.total_value == Money(Decimal("5025"), "USD")

# ------------------------------------------------------------------ exactness
# These sandbox numbers are round, so a float would survive them. 5025.0 is
# 5025. That is exactly why the guarantee has to be structural rather than
# lucky: below is a real wallet-sized balance, and the float answer is wrong
# at the 17th digit before anything is even summed.
whale = Quantity(4_878_123_456_789_012_345_678, 18)
exact = whale.as_decimal() * Decimal("3584.17")
lossy = Decimal(str(float(whale.as_decimal()) * 3584.17))
assert exact != lossy
print(f"\n  4878.123456789012345678 ETH @ 3584.17")
print(f"    exact  {exact}")
print(f"    float  {lossy}  <- 17 significant digits, then guesses")
print(f"    drift  {abs(exact - lossy)} USD on ONE holding")

# ----------------------------------------------------------- unpriced assets
# Nothing in this recording is unpriced, so here is what it looks like when
# something is: the asset stays in `holdings` with price=None and value=None,
# and its id is named in `report.unpriced`. It is NEVER counted as zero, and
# there is no price source for Bitcoin or Solana assets in this package at
# all, so this is the normal case for them, not an edge case.
print(f"\n  unpriced: {report.unpriced or '(none in the sandbox recording)'}")
for holding in report.holdings:
    if holding.price is None:
        print(f"    {holding.symbol} held, not valued: {holding.quantity}")

# -------------------------------------------------------------- on the wire
# Rule #2: a raw amount is a tagged decimal STRING, never a JSON number, so
# no JavaScript client can quietly round it. The lossy float rides alongside,
# clearly labelled, for clients that only want to draw a chart.
wire = quantity_to_wire(report.holdings[0].quantity)
assert isinstance(wire["raw"], str)
print(f"\n  quantity on the wire: {wire}")
print(f"  total on the wire:    {money_to_wire(report.total_value)}")

# --------------------------------------------------------------- going live
# One line different. The key is OPTIONAL, without it Etherscan's keyless
# tier applies, and one key covers every eip155 chain.
if os.environ.get("AURADEFI_ETHERSCAN_API_KEY"):
    live = Auradefi.from_env()
    user = live.user("your-opaque-user-id")
    user.connect_address("eip155:1", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
    (live_report,) = live.holdings()
    print(f"\n  LIVE: {live_report.total_value} across "
          f"{len(live_report.holdings)} assets, {len(live_report.unpriced)} unpriced")
else:
    print("\n  live: set AURADEFI_ETHERSCAN_API_KEY and this file will also")
    print("        run Auradefi.from_env() against mainnet. Same code below it.")

print("\nOK: priced exactly, offline, with one line between here and mainnet.")
