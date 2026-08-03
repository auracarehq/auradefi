"""Wire form for Quantity and Money — DECISIONS pinned algorithm.

These dicts are public stability guarantees (SPEC rule #3): golden vectors
are hardcoded literals derived independently of the code under test.
1.2345678901234568 below is the double nearest 1.234567890123456789
(hex 0x1.3c0ca428c59fbp+0), matching Zerion's published example.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from auradefi.errors import ValidationError
from auradefi.money.decimal_json import (
    money_from_wire,
    money_to_wire,
    quantity_from_wire,
    quantity_to_wire,
)
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

HUGE = 10**77 + 1  # 78 digits: '1' + 76 zeros + '1'

# --------------------------------------------------------- quantity_to_wire


def test_quantity_to_wire_golden_vector():
    wire = quantity_to_wire(Quantity(1_234_567_890_123_456_789, 18))
    assert wire == {
        "raw": "1234567890123456789",
        "decimals": 18,
        "numeric": "1.234567890123456789",
        "float": 1.2345678901234568,
    }


def test_quantity_to_wire_raw_is_a_string_never_a_json_integer():
    wire = quantity_to_wire(Quantity(HUGE, 18))
    assert isinstance(wire["raw"], str)  # rule #2
    assert isinstance(wire["decimals"], int)
    assert isinstance(wire["numeric"], str)
    assert isinstance(wire["float"], float)


def test_quantity_to_wire_negative_golden():
    wire = quantity_to_wire(Quantity(-1_500_000_000_000_000_000, 18))
    assert wire == {
        "raw": "-1500000000000000000",
        "decimals": 18,
        "numeric": "-1.5",
        "float": -1.5,
    }


def test_quantity_to_wire_zero_golden():
    wire = quantity_to_wire(Quantity(0, 18))
    assert wire == {"raw": "0", "decimals": 18, "numeric": "0", "float": 0.0}


def test_quantity_to_wire_huge_is_exact_with_no_scientific_notation():
    wire = quantity_to_wire(Quantity(HUGE, 18))
    assert wire["raw"] == "1" + "0" * 76 + "1"
    assert wire["numeric"] == (
        "100000000000000000000000000000000000000000000000000000000000"
        ".000000000000000001"
    )
    assert "E" not in wire["numeric"] and "e" not in wire["numeric"]


# ------------------------------------------------------- quantity_from_wire


def test_quantity_from_wire_reads_raw_and_decimals():
    wire = {
        "raw": "1234567890123456789",
        "decimals": 18,
        "numeric": "1.234567890123456789",
        "float": 1.2345678901234568,
    }
    assert quantity_from_wire(wire) == Quantity(1_234_567_890_123_456_789, 18)


def test_quantity_from_wire_ignores_numeric_and_float():
    wire = {"raw": "7", "decimals": 3, "numeric": "999999", "float": 0.001}
    assert quantity_from_wire(wire) == Quantity(7, 3)


def test_quantity_from_wire_works_without_display_fields():
    assert quantity_from_wire({"raw": "-5", "decimals": 2}) == Quantity(-5, 2)


def test_quantity_from_wire_rejects_json_integer_raw():
    with pytest.raises(ValidationError):  # rule #2
        quantity_from_wire({"raw": 5, "decimals": 0})


@pytest.mark.parametrize(
    "raw",
    ["1_000", " 5 ", "+5", "5\n", "٥٥", "5.0"],
    ids=["underscore", "whitespace", "plus_sign", "newline", "arabic_indic", "decimal_point"],
)
def test_quantity_from_wire_rejects_non_strict_integer_strings(raw):
    # The wire grammar is exactly -?[0-9]+ — everything int() tolerates
    # beyond that (underscores, whitespace, '+', non-ASCII digits) is out.
    with pytest.raises(ValidationError):
        quantity_from_wire({"raw": raw, "decimals": 0})


@pytest.mark.parametrize("decimals", ["18", 18.0, None])
def test_quantity_from_wire_rejects_non_int_decimals(decimals):
    with pytest.raises(ValidationError):
        quantity_from_wire({"raw": "5", "decimals": decimals})


@pytest.mark.parametrize(
    ("raw", "decimals"),
    [
        (-1_500_000_000_000_000_000, 18),
        (0, 18),
        (0, 0),
        (HUGE, 18),
        (-HUGE, 77),
        (1, 77),
    ],
)
def test_quantity_round_trips_through_wire_and_json(raw, decimals):
    quantity = Quantity(raw, decimals)
    assert quantity_from_wire(quantity_to_wire(quantity)) == quantity
    # And through an actual JSON encode/decode, as it ships on the wire.
    revived = json.loads(json.dumps(quantity_to_wire(quantity)))
    assert quantity_from_wire(revived) == quantity


# ------------------------------------------------------------ money_to_wire


def test_money_to_wire_golden_vector():
    wire = money_to_wire(Money(Decimal("4321.55"), "USD"))
    assert wire == {"amount": "4321.55", "currency": "USD"}


def test_money_to_wire_rule_one_example_exact():
    wire = money_to_wire(Money(Decimal("-741.027368947745798389"), "USD"))
    assert wire == {"amount": "-741.027368947745798389", "currency": "USD"}


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (Decimal("5E-9"), "0.000000005"),  # str(Decimal('5E-9')) is '5E-9'
        (Decimal("1E+2"), "100"),
        (Decimal("-2.5E-7"), "-0.00000025"),
    ],
)
def test_money_to_wire_never_scientific_notation(amount, expected):
    wire = money_to_wire(Money(amount, "USD"))
    assert wire["amount"] == expected


def test_money_to_wire_caip19_currency_passthrough():
    caip19 = "eip155:1/erc20:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    wire = money_to_wire(Money(Decimal("0.25"), caip19))
    assert wire == {"amount": "0.25", "currency": caip19}


# ---------------------------------------------------------- money_from_wire


def test_money_from_wire_golden_vector():
    money = money_from_wire({"amount": "4321.55", "currency": "USD"})
    assert money == Money(Decimal("4321.55"), "USD")


def test_money_from_wire_rejects_float_amount():
    with pytest.raises(ValidationError):  # rule #1
        money_from_wire({"amount": 4321.55, "currency": "USD"})


def test_money_from_wire_rejects_invalid_currency():
    with pytest.raises(ValidationError):
        money_from_wire({"amount": "1", "currency": "usd"})


@pytest.mark.parametrize(
    "amount",
    [
        "1_000",
        " 5 ",
        "٥.٥",
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "inf",
        "nan",
        "1e5",
    ],
    ids=[
        "underscore",
        "whitespace",
        "arabic_indic",
        "NaN",
        "sNaN",
        "Infinity",
        "neg_Infinity",
        "inf",
        "nan",
        "scientific",
    ],
)
def test_money_from_wire_rejects_non_strict_decimal_strings(amount):
    # The wire grammar is exactly -?[0-9]+(\.[0-9]+)? — everything
    # Decimal() tolerates beyond that (underscores, whitespace, non-ASCII
    # digits, the non-finite specials, scientific notation) is out.
    with pytest.raises(ValidationError):
        money_from_wire({"amount": amount, "currency": "USD"})


@pytest.mark.parametrize(
    ("amount", "currency"),
    [
        ("4321.55", "USD"),
        ("-741.027368947745798389", "USD"),
        ("0", "EUR"),
        ("0.000001", "eip155:1/erc20:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
    ],
)
def test_money_round_trips_through_wire_and_json(amount, currency):
    money = Money(Decimal(amount), currency)
    assert money_from_wire(money_to_wire(money)) == money
    revived = json.loads(json.dumps(money_to_wire(money)))
    assert money_from_wire(revived) == money
