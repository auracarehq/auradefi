"""THE PHASE 1 GATE (SPEC rule #5; SPEC §11 Phase 1 "done when": a
known-rich address returns a USD total within a few % of an incumbent).

Wires the REAL EtherscanV2(cassette client, api_key='TESTKEY') +
Inquirer([DefiLlamaOracle(cassette client)]) + HoldingsService with a
FrozenClock over ONE shared httpx client replaying
tests/cassettes/phase1_vitalik.json — Cassette.client() serves BOTH hosts
(api.etherscan.io and coins.llama.fi) through one MockTransport — and
asserts hardcoded golden numbers. A number changes -> this file goes red.

Golden vectors derived independently from the cassette bodies via exact
Decimal arithmetic at 200-digit precision (never floats, never the code
under test):

    ETH   4878.123456789012345678 * 3584.17  = 17484023.75011947437900871726
    DAI   255000                  * 0.99985  = 254961.75
    USDC  1250000.75              * 0.999839 = 1249799.49987925
    TOTAL                                    = 18988784.99999872437900871726

The cassette's tokentx page also proves discovery hygiene: a mixed-case
duplicate USDC contract dedupes after lowercasing, and a spam row with
tokenDecimal "" is skipped additively — exactly three holdings survive.

Incumbent reference: 19,000,000 USD. Actual delta ~0.059%, far inside the
5% band (SPEC §11 Phase 1).
"""

from __future__ import annotations

from decimal import Decimal

from auradefi.clock import FrozenClock
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.portfolio.holdings import HoldingsService
from auradefi.prices.inquirer import Inquirer
from auradefi.prices.oracles.defillama import DefiLlamaOracle
from auradefi.sources.evm.etherscan import EtherscanV2

CHAIN = "eip155:1"
ADDRESS = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
AS_OF_MS = 1_754_000_000_000

ETH = "eip155:1/slip44:60"
DAI = "eip155:1/erc20:0x6b175474e89094c44da98b954eedeac495271d0f"
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

GOLDEN_TOTAL_USD = Decimal("18988784.99999872437900871726")
INCUMBENT_REFERENCE_USD = Decimal("19000000")


def _phase1_report(cassette):
    """The full Phase 1 stack over one shared cassette-backed client."""
    client = cassette("phase1_vitalik").client()
    source = EtherscanV2(client, api_key="TESTKEY")
    prices = Inquirer([DefiLlamaOracle(client)])
    service = HoldingsService(source, prices, clock=FrozenClock(AS_OF_MS))
    return service.holdings(CHAIN, ADDRESS)


def test_exactly_three_holdings_in_order_eth_dai_usdc(cassette):
    report = _phase1_report(cassette)
    # spam row (tokenDecimal "") skipped; duplicate USDC contract deduped.
    assert tuple(h.caip19 for h in report.holdings) == (ETH, DAI, USDC)
    assert tuple(h.symbol for h in report.holdings) == ("ETH", "DAI", "USDC")


def test_eth_holding_golden_numbers(cassette):
    eth = _phase1_report(cassette).holdings[0]
    assert eth.quantity == Quantity(4878123456789012345678, 18)
    assert eth.price == Money(Decimal("3584.17"), "USD")
    assert eth.value is not None
    assert eth.value.amount == Decimal("17484023.75011947437900871726")
    assert eth.value.currency == "USD"


def test_dai_holding_golden_numbers(cassette):
    dai = _phase1_report(cassette).holdings[1]
    assert dai.quantity == Quantity(255000000000000000000000, 18)
    assert dai.price == Money(Decimal("0.99985"), "USD")
    assert dai.value is not None
    assert dai.value.amount == Decimal("254961.75")
    assert dai.value.currency == "USD"


def test_usdc_holding_golden_numbers(cassette):
    usdc = _phase1_report(cassette).holdings[2]
    assert usdc.quantity == Quantity(1250000750000, 6)
    assert usdc.price == Money(Decimal("0.999839"), "USD")
    assert usdc.value is not None
    assert usdc.value.amount == Decimal("1249799.49987925")
    assert usdc.value.currency == "USD"


def test_total_value_is_the_golden_usd_total(cassette):
    report = _phase1_report(cassette)
    assert report.total_value == Money(GOLDEN_TOTAL_USD, "USD")


def test_nothing_unpriced_and_identity_echoed(cassette):
    report = _phase1_report(cassette)
    assert report.unpriced == ()
    assert report.as_of_ms == AS_OF_MS
    assert report.address == ADDRESS
    assert report.chain_id == CHAIN


def test_total_is_within_five_percent_of_the_incumbent(cassette):
    # SPEC §11 Phase 1 "done when", stated as arithmetic. Actual delta is
    # ~0.00059 (0.059%) — two orders of magnitude inside the band.
    total = _phase1_report(cassette).total_value.amount
    delta = abs(total - INCUMBENT_REFERENCE_USD) / INCUMBENT_REFERENCE_USD
    assert delta < Decimal("0.05")
