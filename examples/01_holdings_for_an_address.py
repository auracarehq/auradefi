"""How do I get a priced portfolio for one address?

    pip install auradefi && python 01_holdings_for_an_address.py

Discover an address' assets, quote them, and get an exact `Decimal` total.
Three things worth noticing, because they are the whole design:

* the total is computed in `Decimal`, never a float — see the comparison at
  the end, where the float answer is already wrong at the 17th digit;
* an asset nobody would price is **named in `report.unpriced`**, not
  silently valued at zero;
* the transport is a port. This file replays a recorded cassette so it runs
  offline with no API key; the last section shows the two-line change that
  points the same code at the real Etherscan and DefiLlama.

Self-contained: nothing here reads the repository.
"""

from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path

from auradefi.clock import FrozenClock
from auradefi.money.decimal_json import money_to_wire, quantity_to_wire
from auradefi.money.fiat import Money
from auradefi.portfolio.holdings import HoldingsService
from auradefi.prices.inquirer import Inquirer
from auradefi.prices.oracles.defillama import DefiLlamaOracle
from auradefi.sources.evm.etherscan import EtherscanV2
from auradefi.testing.cassettes import load

CHAIN = "eip155:1"
ADDRESS = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
SHIB = "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce"
ETHERSCAN = "https://api.etherscan.io/v2/api"
LLAMA = "https://coins.llama.fi/prices"


def recorded(method: str, url: str, payload: dict) -> dict:
    return {"request": {"method": method, "url": url},
            "response": {"status": 200, "json": payload}}


def etherscan_ok(result: object) -> dict:
    return {"status": "1", "message": "OK", "result": result}


# Exactly the four requests this example makes, recorded. An unrecorded
# request raises CassetteMissError, which is what makes "offline" a
# guarantee rather than a hope.
CASSETTE = {"interactions": [
    recorded("GET", f"{ETHERSCAN}?chainid=1&module=account&action=balance"
                    f"&address={ADDRESS}&tag=latest",
             etherscan_ok("2500000000000000000")),
    # Asset discovery: which tokens has this address ever touched? The
    # duplicate mixed-case DAI row dedupes; a row with no decimals is
    # unusable and skipped rather than guessed at.
    recorded("GET", f"{ETHERSCAN}?chainid=1&module=account&action=tokentx"
                    f"&address={ADDRESS}&startblock=0&endblock=99999999"
                    "&page=1&offset=1000&sort=asc",
             etherscan_ok([
                 {"contractAddress": DAI, "tokenSymbol": "DAI", "tokenDecimal": "18"},
                 {"contractAddress": DAI.upper(), "tokenSymbol": "DAI", "tokenDecimal": "18"},
                 {"contractAddress": USDC, "tokenSymbol": "USDC", "tokenDecimal": "6"},
                 {"contractAddress": SHIB, "tokenSymbol": "SHIB", "tokenDecimal": "18"},
                 {"contractAddress": "0xdead", "tokenSymbol": "SCAM", "tokenDecimal": ""},
             ])),
    *[recorded("GET", f"{ETHERSCAN}?chainid=1&module=account&action=tokenbalance"
                      f"&contractaddress={contract}&address={ADDRESS}&tag=latest",
               etherscan_ok(balance))
      for contract, balance in ((DAI, "255000000000000000000000"),
                               (USDC, "1250000750000"),
                               (SHIB, "1000000000000000000000000"))],
    # One price request for every discovered asset, keys in sorted order.
    # SHIB is asked for and not answered — that is the interesting case.
    recorded("GET", f"{LLAMA}/current/coingecko:ethereum,ethereum:{DAI},"
                    f"ethereum:{SHIB},ethereum:{USDC}",
             {"coins": {
                 "coingecko:ethereum": {"price": 3584.17, "symbol": "ETH"},
                 f"ethereum:{DAI}": {"price": 0.99985, "symbol": "DAI"},
                 f"ethereum:{USDC}": {"price": 0.999839, "symbol": "USDC"},
             }}),
]}

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "holdings.json"
    path.write_text(json.dumps(CASSETTE), encoding="utf-8")
    client = load(path).client()

    # ---------------------------------------------------------------- wire it
    # Both collaborators share one client, exactly as a host would wire them.
    service = HoldingsService(
        source=EtherscanV2(client, api_key=None),
        prices=Inquirer([DefiLlamaOracle(client)]),
        clock=FrozenClock(1_754_000_000_000),
    )
    report = service.holdings(CHAIN, ADDRESS)

print(f"holdings for {ADDRESS} at {report.as_of_ms} ms\n")
print(f"  {'asset':<6}{'quantity':>28}{'price':>14}    value")
for holding in report.holdings:
    print(f"  {holding.symbol:<6}{str(holding.quantity):>28}"
          f"{str(holding.price):>14}    {holding.value}")
print(f"  {'TOTAL':<6}{'':>28}{'':>14}    {report.total_value}")

# The unpriced asset is held and reported, never valued at zero.
assert report.unpriced == (f"{CHAIN}/erc20:{SHIB}",)
(shib,) = [holding for holding in report.holdings if holding.symbol == "SHIB"]
assert (shib.price, shib.value) == (None, None)
assert shib.quantity.as_decimal() == Decimal("1000000")
print(f"\n  unpriced: {report.unpriced} — held, listed, and NOT counted as 0")

# -------------------------------------------------------------- exactness
assert report.total_value == Money(Decimal("1513721.674879250000"), "USD")
float_total = sum(float(holding.quantity.as_decimal()) * float(holding.price.amount)
                  for holding in report.holdings if holding.price is not None)
assert Decimal(str(float_total)) != report.total_value.amount
print(f"\n  exact  {report.total_value.amount}")
print(f"  float  {float_total!r}  <- wrong, and wrong in a way that compounds")

# ------------------------------------------------------------- on the wire
# Rule #2: a raw amount is a tagged decimal STRING, never a JSON number, so
# no JavaScript client can quietly round it. The lossy float rides along
# beside it, clearly labelled, for the clients that only want to draw a chart.
wire = quantity_to_wire(report.holdings[1].quantity)
assert wire == {"raw": "255000000000000000000000", "decimals": 18,
                "numeric": "255000", "float": 255000.0}
print(f"\n  quantity on the wire: {json.dumps(wire)}")
print(f"  total on the wire:    {json.dumps(money_to_wire(report.total_value))}")

# ------------------------------------------------------------ going live
# Replace the two cassette lines above with a real client and the same code
# talks to mainnet:
#
#     import httpx
#     client = httpx.Client(timeout=10)
#     service = HoldingsService(
#         source=EtherscanV2(client, api_key=os.environ["ETHERSCAN_API_KEY"]),
#         prices=Inquirer([DefiLlamaOracle(client)]),
#     )
#
# Nothing else changes: clock=None means SystemClock, and one Etherscan V2
# key covers every chain in the registry. Token balances cost one request
# each — there is no multicall in the package yet (README, *What is not
# there*), so budget accordingly on a wide address.
print("\nOK — priced offline, exactly, with the unpriced asset named.")
