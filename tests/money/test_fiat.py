"""Money: tagged exact decimal (SPEC §4.1, rule #1).

Rule #1's named casualty is silent corruption of exactly the largest
balances, so exactness is asserted past the default Decimal context
precision (28 significant digits).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from auradefi.errors import CurrencyMismatchError, ValidationError
from auradefi.money.fiat import Money

USDC_ETH = "eip155:1/erc20:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
NATIVE_BASE = "eip155:8453/slip44:60"

# --------------------------------------------------------------- construction


@pytest.mark.parametrize("currency", ["USD", "EUR", "JPY", "GBP"])
def test_iso4217_shaped_currency_accepted(currency):
    assert Money(Decimal("1"), currency).currency == currency


@pytest.mark.parametrize("currency", [USDC_ETH, NATIVE_BASE])
def test_caip19_currency_accepted(currency):
    assert Money(Decimal("1"), currency).currency == currency


@pytest.mark.parametrize(
    "currency",
    ["usd", "Usd", "US", "USDC", "", "U5D", "US$", "usd ", " USD", "123"],
)
def test_invalid_currency_raises_validation_error(currency):
    with pytest.raises(ValidationError):
        Money(Decimal("1"), currency)


def test_amount_held_verbatim():
    amount = Decimal("-741.027368947745798389")  # SPEC rule #1's own example
    assert Money(amount, "USD").amount == amount


@pytest.mark.parametrize(
    "amount",
    [4321.55, 1, "1.5", True, None],
    ids=["float", "int", "str", "bool", "none"],
)
def test_non_decimal_amount_raises_validation_error(amount):
    # rule #1: the amount is a Decimal or it is not Money at all.
    with pytest.raises(ValidationError):
        Money(amount, "USD")


def test_nan_amount_raises_validation_error():
    with pytest.raises(ValidationError):
        Money(Decimal("NaN"), "USD")


# --------------------------------------------------------------- immutability


def test_frozen_attribute_assignment_raises():
    money = Money(Decimal("1"), "USD")
    with pytest.raises(FrozenInstanceError):
        money.amount = Decimal("2")
    with pytest.raises(FrozenInstanceError):
        money.currency = "EUR"


def test_slots_no_instance_dict():
    assert not hasattr(Money(Decimal("1"), "USD"), "__dict__")


# ----------------------------------------------------------------- arithmetic


def test_add_same_currency():
    total = Money(Decimal("1.10"), "USD") + Money(Decimal("2.20"), "USD")
    assert total == Money(Decimal("3.30"), "USD")


def test_sub_same_currency_can_go_negative():
    result = Money(Decimal("0"), "USD") - Money(
        Decimal("741.027368947745798389"), "USD"
    )
    assert result == Money(Decimal("-741.027368947745798389"), "USD")


def test_add_is_exact_past_default_context_precision():
    # 32 significant digits: the default Decimal context (prec=28) would
    # silently round the sum — exactly rule #1's corrupted-largest-balance.
    a = Money(Decimal("10000000000000.000000000000000001"), "USD")
    b = Money(Decimal("20000000000000.000000000000000002"), "USD")
    assert (a + b).amount == Decimal("30000000000000.000000000000000003")


def test_sub_is_exact_past_default_context_precision():
    a = Money(Decimal("30000000000000.000000000000000003"), "USD")
    b = Money(Decimal("10000000000000.000000000000000001"), "USD")
    assert (a - b).amount == Decimal("20000000000000.000000000000000002")


def test_add_mismatched_currency_raises():
    with pytest.raises(CurrencyMismatchError):
        Money(Decimal("1"), "USD") + Money(Decimal("1"), "EUR")


def test_sub_mismatched_currency_raises():
    with pytest.raises(CurrencyMismatchError):
        Money(Decimal("1"), "USD") - Money(Decimal("1"), "EUR")


def test_caip19_and_iso_never_mix():
    with pytest.raises(CurrencyMismatchError):
        Money(Decimal("1"), "USD") + Money(Decimal("1"), USDC_ETH)


def test_caip19_same_currency_adds():
    total = Money(Decimal("0.5"), USDC_ETH) + Money(Decimal("0.25"), USDC_ETH)
    assert total == Money(Decimal("0.75"), USDC_ETH)


def test_neg():
    assert -Money(Decimal("5"), "USD") == Money(Decimal("-5"), "USD")
    assert -Money(Decimal("-5"), "USD") == Money(Decimal("5"), "USD")


# -------------------------------------------------------------------- __str__


def test_str_is_amount_space_currency():
    assert str(Money(Decimal("4321.55"), "USD")) == "4321.55 USD"


def test_str_negative_exact():
    money = Money(Decimal("-741.027368947745798389"), "USD")
    assert str(money) == "-741.027368947745798389 USD"


def test_str_caip19_currency():
    assert str(Money(Decimal("1.5"), NATIVE_BASE)) == f"1.5 {NATIVE_BASE}"
