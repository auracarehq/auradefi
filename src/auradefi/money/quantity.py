"""Exact on-chain amounts in base units (SPEC §4.1, rule #2).

A ``Quantity`` is an arbitrary-precision integer count of base units plus
the power-of-ten scale that turns it into a human amount. ``raw`` is a
Python ``int`` (any sign, no ceiling); ``decimals`` must be ``>= 0``.

Nothing here may round: ``as_decimal`` and ``__str__`` are exact at any
magnitude (10^77-scale values included) and ``__str__`` never emits
scientific notation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auradefi.errors import DecimalsMismatchError, ValidationError


@dataclass(frozen=True, slots=True)
class Quantity:
    """An exact base-unit amount: ``raw * 10**-decimals``.

    Equality is strict on ``(raw, decimals)`` — two quantities of equal
    numeric value but different scales are NOT equal. Arithmetic and
    ordering are defined only between quantities of equal ``decimals``;
    mixing scales raises ``DecimalsMismatchError`` (auradefi.errors).
    """

    raw: int
    decimals: int

    def __post_init__(self) -> None:
        """Validate the value; raise ``ValidationError`` if decimals < 0.

        ``bool`` is rejected for both fields BEFORE the int check —
        ``bool`` is an ``int`` subclass, and ``Quantity(True, 0)`` is a
        caller bug, never an amount.
        """
        if isinstance(self.raw, bool):
            raise ValidationError("raw must be an int, got bool")
        if not isinstance(self.raw, int):
            raise ValidationError(
                f"raw must be an int, got {type(self.raw).__name__}"
            )
        if isinstance(self.decimals, bool):
            raise ValidationError("decimals must be an int, got bool")
        if not isinstance(self.decimals, int):
            raise ValidationError(
                f"decimals must be an int, got {type(self.decimals).__name__}"
            )
        if self.decimals < 0:
            raise ValidationError(f"decimals must be >= 0, got {self.decimals}")

    def as_decimal(self) -> Decimal:
        """The exact ``Decimal`` value ``raw * 10**-decimals``.

        Exact at any magnitude — never subject to context-precision
        rounding (a 78-digit ``raw`` survives intact).
        """
        sign = 1 if self.raw < 0 else 0
        digits = tuple(int(char) for char in str(abs(self.raw)))
        return Decimal((sign, digits, -self.decimals))

    def __str__(self) -> str:
        """Exact decimal string, never scientific notation.

        Trailing fractional zeros are trimmed: ``str(Quantity(15 * 10**17,
        18)) == '1.5'`` and ``str(Quantity(1, 77))`` is ``'0.' + 76 zeros
        + '1'``. No ``'E'`` ever appears.
        """
        sign = "-" if self.raw < 0 else ""
        digits = str(abs(self.raw))
        if self.decimals == 0:
            return sign + digits
        digits = digits.rjust(self.decimals + 1, "0")
        integer = digits[: -self.decimals]
        fraction = digits[-self.decimals :].rstrip("0")
        if not fraction:
            return sign + integer
        return f"{sign}{integer}.{fraction}"

    def _require_same_scale(self, other: Quantity) -> None:
        """Raise ``DecimalsMismatchError`` unless scales are equal."""
        if self.decimals != other.decimals:
            raise DecimalsMismatchError(
                f"decimals mismatch: {self.decimals} vs {other.decimals}"
            )

    def __add__(self, other: Quantity) -> Quantity:
        """Sum of equal-decimals quantities; else ``DecimalsMismatchError``."""
        if not isinstance(other, Quantity):
            return NotImplemented
        self._require_same_scale(other)
        return Quantity(self.raw + other.raw, self.decimals)

    def __sub__(self, other: Quantity) -> Quantity:
        """Difference of equal-decimals quantities; else ``DecimalsMismatchError``."""
        if not isinstance(other, Quantity):
            return NotImplemented
        self._require_same_scale(other)
        return Quantity(self.raw - other.raw, self.decimals)

    def __neg__(self) -> Quantity:
        """The quantity with ``raw`` negated, same ``decimals``."""
        return Quantity(-self.raw, self.decimals)

    def __lt__(self, other: Quantity) -> bool:
        """Ordering on equal decimals only; else ``DecimalsMismatchError``."""
        if not isinstance(other, Quantity):
            return NotImplemented
        self._require_same_scale(other)
        return self.raw < other.raw

    def __le__(self, other: Quantity) -> bool:
        """Ordering on equal decimals only; else ``DecimalsMismatchError``."""
        if not isinstance(other, Quantity):
            return NotImplemented
        self._require_same_scale(other)
        return self.raw <= other.raw

    def __gt__(self, other: Quantity) -> bool:
        """Ordering on equal decimals only; else ``DecimalsMismatchError``."""
        if not isinstance(other, Quantity):
            return NotImplemented
        self._require_same_scale(other)
        return self.raw > other.raw

    def __ge__(self, other: Quantity) -> bool:
        """Ordering on equal decimals only; else ``DecimalsMismatchError``."""
        if not isinstance(other, Quantity):
            return NotImplemented
        self._require_same_scale(other)
        return self.raw >= other.raw
