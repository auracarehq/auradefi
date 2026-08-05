"""Quantity: exact base-unit amounts (SPEC §4.1, rule #2).

Golden strings below were derived independently with context-free Decimal
tuple construction (Decimal((sign, digits, -decimals))), never with the
code under test.
"""

from __future__ import annotations

import operator
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from auradefi.errors import DecimalsMismatchError, ValidationError
from auradefi.money.quantity import Quantity

WEI_15 = 1_500_000_000_000_000_000  # 1.5 tokens at 18 decimals
HUGE = 10**77 + 1  # 78 digits: '1' + 76 zeros + '1'

# ---------------------------------------------------------------- construction


def test_holds_raw_and_decimals_verbatim():
    quantity = Quantity(raw=WEI_15, decimals=18)
    assert quantity.raw == WEI_15
    assert quantity.decimals == 18


def test_raw_is_arbitrary_precision_any_sign():
    assert Quantity(HUGE, 18).raw == HUGE
    assert Quantity(-HUGE, 18).raw == -HUGE
    assert Quantity(0, 0).raw == 0


def test_zero_decimals_is_valid():
    assert Quantity(5, 0).decimals == 0


@pytest.mark.parametrize("decimals", [-1, -18, -(10**6)])
def test_negative_decimals_raises_validation_error(decimals):
    with pytest.raises(ValidationError):
        Quantity(1, decimals)


def test_bool_raw_raises_validation_error():
    # bool is an int subclass; Quantity(True, 0) is a caller bug, not 1.
    with pytest.raises(ValidationError):
        Quantity(True, 0)


def test_bool_decimals_raises_validation_error():
    with pytest.raises(ValidationError):
        Quantity(1, False)


# --------------------------------------------------------------- immutability


def test_frozen_attribute_assignment_raises():
    quantity = Quantity(1, 2)
    with pytest.raises(FrozenInstanceError):
        quantity.raw = 99
    with pytest.raises(FrozenInstanceError):
        quantity.decimals = 3


def test_slots_no_instance_dict():
    assert not hasattr(Quantity(1, 2), "__dict__")


# ----------------------------------------------------------------- as_decimal


def test_as_decimal_golden():
    value = Quantity(1_234_567_890_123_456_789, 18).as_decimal()
    assert isinstance(value, Decimal)
    assert value == Decimal("1.234567890123456789")


def test_as_decimal_is_exact_at_78_digit_scale():
    # Default Decimal context (prec=28) rounds this: an implementation
    # dividing raw / 10**decimals under the default context fails here.
    expected = Decimal(
        "100000000000000000000000000000000000000000000000000000000000"
        ".000000000000000001"
    )
    assert Quantity(HUGE, 18).as_decimal() == expected


def test_as_decimal_negative_and_zero():
    assert Quantity(-WEI_15, 18).as_decimal() == Decimal("-1.5")
    assert Quantity(0, 18).as_decimal() == Decimal("0")


# -------------------------------------------------------------------- __str__


@pytest.mark.parametrize(
    ("raw", "decimals", "expected"),
    [
        (WEI_15, 18, "1.5"),
        (-WEI_15, 18, "-1.5"),
        (1, 77, "0." + "0" * 76 + "1"),
        (10**77, 0, "1" + "0" * 77),
        (HUGE, 77, "1." + "0" * 76 + "1"),
        (10**18, 18, "1"),
        (100, 0, "100"),  # Decimal('100').normalize() emits '1E+2': banned
        (0, 18, "0"),
        (5, 0, "5"),
        (1_234_567_890_123_456_789, 18, "1.234567890123456789"),
    ],
)
def test_str_is_exact_and_never_scientific(raw, decimals, expected):
    text = str(Quantity(raw, decimals))
    assert text == expected
    assert "E" not in text and "e" not in text


# ----------------------------------------------------------------- arithmetic


def test_add_equal_decimals():
    total = Quantity(WEI_15, 18) + Quantity(500_000_000_000_000_000, 18)
    assert total == Quantity(2_000_000_000_000_000_000, 18)


def test_sub_equal_decimals_can_go_negative():
    assert Quantity(1, 18) - Quantity(2, 18) == Quantity(-1, 18)


def test_add_mismatched_decimals_raises():
    with pytest.raises(DecimalsMismatchError):
        Quantity(1, 6) + Quantity(1, 18)


def test_sub_mismatched_decimals_raises():
    with pytest.raises(DecimalsMismatchError):
        Quantity(1, 18) - Quantity(1, 6)


def test_arithmetic_is_exact_at_78_digit_scale():
    assert Quantity(HUGE, 18) + Quantity(HUGE, 18) == Quantity(2 * HUGE, 18)


def test_neg():
    assert -Quantity(5, 2) == Quantity(-5, 2)
    assert -Quantity(-5, 2) == Quantity(5, 2)
    assert -(-Quantity(HUGE, 18)) == Quantity(HUGE, 18)


# ------------------------------------------------------------------- ordering


def test_ordering_on_equal_decimals():
    small, big = Quantity(-5, 18), Quantity(3, 18)
    assert small < big
    assert small <= big
    assert big > small
    assert big >= small
    assert Quantity(3, 18) <= Quantity(3, 18)
    assert Quantity(3, 18) >= Quantity(3, 18)
    assert not Quantity(3, 18) < Quantity(3, 18)


@pytest.mark.parametrize(
    "compare", [operator.lt, operator.le, operator.gt, operator.ge]
)
def test_ordering_across_decimals_raises(compare):
    with pytest.raises(DecimalsMismatchError):
        compare(Quantity(1, 6), Quantity(1, 18))


# ------------------------------------------------------------------- equality


def test_equality_strict_on_raw_and_decimals():
    assert Quantity(15, 1) == Quantity(15, 1)
    assert Quantity(15, 1) != Quantity(16, 1)
    # Numerically identical values at different scales are NOT equal.
    assert Quantity(15, 1) != Quantity(150, 2)
    assert Quantity(15, 1) != Quantity(WEI_15, 18)


def test_equal_quantities_are_interchangeable_as_keys():
    assert Quantity(5, 2) in {Quantity(5, 2)}
    assert hash(Quantity(5, 2)) == hash(Quantity(5, 2))
