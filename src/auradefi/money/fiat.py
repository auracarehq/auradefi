"""Tagged decimal money (SPEC §4.1, rule #1).

``Money`` pairs an exact ``Decimal`` amount with a currency tag. The
currency is either a 3-letter uppercase ISO-4217-shaped code (``"USD"``)
or a CAIP-19 identifier (recognised by containing ``'/'``) for crypto
denomination; anything else raises ``ValidationError``.

Arithmetic never crosses currencies (``CurrencyMismatchError``) and never
rounds. The largest balances survive exactly (rule #1's named casualty).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from auradefi.errors import CurrencyMismatchError, ValidationError

_ISO_4217_SHAPE = re.compile(r"[A-Z]{3}\Z")


def _coefficient_at(value: Decimal, exponent: int) -> int:
    """``value`` as an exact signed integer count of ``10**exponent`` units.

    ``exponent`` must not exceed the value's own exponent, so scaling is a
    pure integer multiplication, no context, no rounding.
    """
    sign, digits, value_exponent = value.as_tuple()
    coefficient = int("".join(map(str, digits))) * 10 ** (value_exponent - exponent)
    return -coefficient if sign else coefficient


def _exact_sum(left: Decimal, right: Decimal) -> Decimal:
    """Context-free exact sum, never rounded to context precision."""
    exponent = min(left.as_tuple().exponent, right.as_tuple().exponent)
    total = _coefficient_at(left, exponent) + _coefficient_at(right, exponent)
    sign = 1 if total < 0 else 0
    digits = tuple(int(char) for char in str(abs(total)))
    return Decimal((sign, digits, exponent))


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount denominated in one currency.

    ``amount`` is a ``Decimal`` (never a float); ``currency`` is validated
    at construction: 3-letter uppercase code or CAIP-19 (contains '/').
    """

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        """Validate ``amount`` and ``currency``; ``ValidationError`` otherwise.

        ``amount`` must be a finite ``Decimal``: floats, ints, strings and
        the non-finite specials (NaN, sNaN, +/-Infinity) are rejected so
        every constructed ``Money`` serialises exactly (rule #1) and its
        arithmetic stays inside the ``auradefi.errors`` taxonomy.
        """
        if not isinstance(self.amount, Decimal):
            raise ValidationError(
                f"amount must be a Decimal, got {type(self.amount).__name__}"
            )
        if not self.amount.is_finite():
            raise ValidationError(
                f"amount must be finite, got {self.amount} (rule #1)"
            )
        if not isinstance(self.currency, str):
            raise ValidationError(
                f"currency must be a str, got {type(self.currency).__name__}"
            )
        if "/" in self.currency:  # CAIP-19 shaped
            return
        if not _ISO_4217_SHAPE.fullmatch(self.currency):
            raise ValidationError(
                "currency must be a 3-letter uppercase code or a CAIP-19 "
                f"(contains '/'): {self.currency!r}"
            )

    def _require_same_currency(self, other: Money) -> None:
        """Raise ``CurrencyMismatchError`` unless currencies are equal."""
        if self.currency != other.currency:
            raise CurrencyMismatchError(
                f"currency mismatch: {self.currency!r} vs {other.currency!r}"
            )

    def __add__(self, other: Money) -> Money:
        """Exact sum in the same currency; else ``CurrencyMismatchError``.

        Exact at any magnitude, never context-precision rounded.
        """
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(_exact_sum(self.amount, other.amount), self.currency)

    def __sub__(self, other: Money) -> Money:
        """Exact difference in the same currency; else ``CurrencyMismatchError``.

        Exact at any magnitude, never context-precision rounded.
        """
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(
            _exact_sum(self.amount, other.amount.copy_negate()), self.currency
        )

    def __neg__(self) -> Money:
        """The money with ``amount`` negated, same currency."""
        return Money(self.amount.copy_negate(), self.currency)

    def __str__(self) -> str:
        """``'<exact amount> <currency>'``, e.g. ``'4321.55 USD'``."""
        return f"{self.amount:f} {self.currency}"
