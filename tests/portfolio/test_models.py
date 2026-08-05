"""Holding and HoldingsReport (SPEC §3.1: Account → Holding[]).

The assemble algorithm is pinned: total_value is the EXACT Decimal sum of
priced values in USD, no rounding, no floats (SPEC rule #1). The golden
addends here mirror the phase-1 gate cassette (tests/cassettes/
phase1_vitalik.json): three position values summing to exactly
18988784.99999872437900871726 USD. Every literal below was derived
independently with exact integer arithmetic over scaled coefficients,
never by calling the code under test.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from auradefi.errors import CurrencyMismatchError, ValidationError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.portfolio.models import Holding, HoldingsReport

ETH = "eip155:1/slip44:60"
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
DAI = "eip155:1/erc20:0x6b175474e89094c44da98b954eedeac495271d0f"
WBTC = "eip155:1/erc20:0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
MYSTERY = "eip155:1/erc20:0x000000000000000000000000000000000000dead"

VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"  # the phase-gate address
CHAIN = "eip155:1"
AS_OF_MS = 1_754_000_000_000  # ms epoch (SPEC §4.4), matches the frozen clock

# Phase-gate golden total: 17484023.75011947437900871726 + 254961.75000
# + 1249799.49987925, summed exactly at exponent -20 (coefficient
# 1898878499999872437900871726).
GOLDEN_TOTAL = Decimal("18988784.99999872437900871726")


def _priced(caip19: str, value: str, price: str = "1", raw: int = 10**18) -> Holding:
    return Holding(
        caip19=caip19,
        symbol=None,
        quantity=Quantity(raw, 18),
        price=Money(Decimal(price), "USD"),
        value=Money(Decimal(value), "USD"),
    )


def _unpriced(caip19: str) -> Holding:
    return Holding(
        caip19=caip19,
        symbol=None,
        quantity=Quantity(1, 18),
        price=None,
        value=None,
    )


# ------------------------------------------------------- Holding construction


def test_fully_priced_holding_constructs_and_holds_fields_verbatim():
    holding = Holding(
        caip19=USDC,
        symbol="USDC",
        quantity=Quantity(254_961_750_000, 6),
        price=Money(Decimal("1.00000"), "USD"),
        value=Money(Decimal("254961.75000"), "USD"),
    )
    assert holding.caip19 == USDC
    assert holding.symbol == "USDC"
    assert holding.quantity == Quantity(254_961_750_000, 6)
    assert holding.price == Money(Decimal("1.00000"), "USD")
    assert holding.value == Money(Decimal("254961.75000"), "USD")


def test_unpriced_holding_constructs_with_both_none():
    holding = Holding(
        caip19=MYSTERY, symbol=None, quantity=Quantity(5, 18), price=None, value=None
    )
    assert holding.price is None
    assert holding.value is None
    assert holding.symbol is None


def test_price_without_value_raises_validation_error():
    with pytest.raises(ValidationError):
        Holding(
            caip19=ETH,
            symbol="ETH",
            quantity=Quantity(10**18, 18),
            price=Money(Decimal("3500"), "USD"),
            value=None,
        )


def test_value_without_price_raises_validation_error():
    with pytest.raises(ValidationError):
        Holding(
            caip19=ETH,
            symbol="ETH",
            quantity=Quantity(10**18, 18),
            price=None,
            value=Money(Decimal("3500"), "USD"),
        )


# --------------------------------------------------------------- immutability


def test_holding_is_frozen():
    holding = _priced(ETH, "3500")
    with pytest.raises(FrozenInstanceError):
        holding.caip19 = USDC
    with pytest.raises(FrozenInstanceError):
        holding.value = None


def test_holding_slots_no_instance_dict():
    assert not hasattr(_priced(ETH, "3500"), "__dict__")


def test_report_is_frozen():
    report = HoldingsReport(
        address=VITALIK,
        chain_id=CHAIN,
        holdings=(),
        total_value=Money(Decimal("0"), "USD"),
        unpriced=(),
        as_of_ms=AS_OF_MS,
    )
    with pytest.raises(FrozenInstanceError):
        report.address = "0x0"
    with pytest.raises(FrozenInstanceError):
        report.total_value = Money(Decimal("1"), "USD")


def test_report_slots_no_instance_dict():
    report = HoldingsReport(
        address=VITALIK,
        chain_id=CHAIN,
        holdings=(),
        total_value=Money(Decimal("0"), "USD"),
        unpriced=(),
        as_of_ms=AS_OF_MS,
    )
    assert not hasattr(report, "__dict__")


# ------------------------------------------------------------------- assemble


def test_assemble_sums_priced_and_lists_unpriced():
    holdings = [
        _priced(ETH, "25"),
        _priced(USDC, "4.50"),
        _unpriced(MYSTERY),
    ]
    report = HoldingsReport.assemble(VITALIK, CHAIN, holdings, AS_OF_MS)
    assert report.total_value == Money(Decimal("29.50"), "USD")
    assert report.total_value.currency == "USD"
    assert report.unpriced == (MYSTERY,)
    assert report.holdings == tuple(holdings)


def test_assemble_golden_addends_match_phase_gate():
    # Mirrors tests/cassettes/phase1_vitalik.json. Derived independently:
    # at exponent -20 the coefficients sum to 1898878499999872437900871726,
    # i.e. exactly 18988784.99999872437900871726: inside 5% of the 19M
    # incumbent reference. A change in this number is a broken engine.
    holdings = [
        _priced(ETH, "17484023.75011947437900871726"),
        _priced(USDC, "254961.75000"),
        _priced(DAI, "1249799.49987925"),
    ]
    report = HoldingsReport.assemble(VITALIK, CHAIN, holdings, AS_OF_MS)
    assert report.total_value == Money(GOLDEN_TOTAL, "USD")
    assert report.total_value.amount == GOLDEN_TOTAL
    assert report.unpriced == ()


def test_assemble_zero_holdings_is_the_zero_report():
    report = HoldingsReport.assemble(VITALIK, CHAIN, [], AS_OF_MS)
    assert report.total_value == Money(Decimal("0"), "USD")
    assert report.holdings == ()
    assert report.unpriced == ()


def test_assemble_all_unpriced_totals_zero_usd():
    report = HoldingsReport.assemble(
        VITALIK, CHAIN, [_unpriced(MYSTERY), _unpriced(WBTC)], AS_OF_MS
    )
    assert report.total_value == Money(Decimal("0"), "USD")
    assert report.unpriced == (MYSTERY, WBTC)


def test_assemble_is_exact_past_default_context_precision():
    # 32 significant digits per addend: a naive context sum (prec=28)
    # yields 30000000000000.00000000000000: rule #1's corrupted largest
    # balance. The pinned algorithm is the EXACT sum.
    holdings = [
        _priced(ETH, "10000000000000.000000000000000001"),
        _priced(WBTC, "20000000000000.000000000000000002"),
    ]
    report = HoldingsReport.assemble(VITALIK, CHAIN, holdings, AS_OF_MS)
    assert report.total_value.amount == Decimal("30000000000000.000000000000000003")


def test_assemble_sums_signed_values_exactly():
    # Debt is a negative value (SPEC §4.3: signed values); the sum nets.
    # 50000.000000000000000007 - 741.027368947745798389
    #   = 49258.972631052254201618 (exact, derived independently).
    holdings = [
        _priced(ETH, "50000.000000000000000007"),
        _priced(DAI, "-741.027368947745798389"),
    ]
    report = HoldingsReport.assemble(VITALIK, CHAIN, holdings, AS_OF_MS)
    assert report.total_value.amount == Decimal("49258.972631052254201618")


def test_assemble_handles_huge_quantities_and_values():
    # 10^77-scale raw survives; the value sum stays exact at scale.
    # 79228162514264337593543.950335 + 0.000001
    #   = 79228162514264337593543.950336 (exact, derived independently).
    holdings = [
        _priced(ETH, "79228162514264337593543.950335", raw=10**77),
        _priced(USDC, "0.000001"),
    ]
    report = HoldingsReport.assemble(VITALIK, CHAIN, holdings, AS_OF_MS)
    assert report.total_value.amount == Decimal("79228162514264337593543.950336")
    assert report.holdings[0].quantity.raw == 10**77


def test_assemble_preserves_holding_and_unpriced_input_order():
    holdings = [
        _unpriced(MYSTERY),
        _priced(ETH, "1"),
        _unpriced(WBTC),
        _priced(USDC, "2"),
        _unpriced(DAI),
    ]
    report = HoldingsReport.assemble(VITALIK, CHAIN, holdings, AS_OF_MS)
    assert report.holdings == tuple(holdings)
    assert report.unpriced == (MYSTERY, WBTC, DAI)
    assert report.total_value == Money(Decimal("3"), "USD")


def test_assemble_accepts_a_generator():
    report = HoldingsReport.assemble(
        VITALIK,
        CHAIN,
        (holding for holding in [_priced(ETH, "25"), _priced(USDC, "4.50")]),
        AS_OF_MS,
    )
    assert report.total_value == Money(Decimal("29.50"), "USD")
    assert len(report.holdings) == 2


def test_assemble_stores_address_chain_and_as_of_ms_verbatim():
    report = HoldingsReport.assemble(VITALIK, CHAIN, [], AS_OF_MS)
    assert report.address == VITALIK
    assert report.chain_id == CHAIN
    assert report.as_of_ms == AS_OF_MS
    assert isinstance(report.as_of_ms, int)  # ms epoch int, never float


@pytest.mark.parametrize(
    "currency",
    ["EUR", "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"],
    ids=["eur", "caip19"],
)
def test_assemble_non_usd_value_raises_currency_mismatch(currency):
    bad = Holding(
        caip19=USDC,
        symbol="USDC",
        quantity=Quantity(10**6, 6),
        price=Money(Decimal("1"), currency),
        value=Money(Decimal("1"), currency),
    )
    with pytest.raises(CurrencyMismatchError):
        HoldingsReport.assemble(VITALIK, CHAIN, [_priced(ETH, "25"), bad], AS_OF_MS)


# -------------------------------------------------------------- module purity


def test_module_imports_only_money_errors_and_stdlib():
    import auradefi.portfolio.models as models

    tree = ast.parse(Path(models.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports in a pure model module"
            imported.add(node.module or "")

    offenders = sorted(
        name
        for name in imported
        if not (
            name.split(".")[0] in sys.stdlib_module_names
            or name == "auradefi.errors"
            or name == "auradefi.money"
            or name.startswith("auradefi.money.")
        )
    )
    assert not offenders, (
        "portfolio/models.py is PURE: auradefi.money, auradefi.errors and "
        f"stdlib only, but it imports: {offenders}"
    )
