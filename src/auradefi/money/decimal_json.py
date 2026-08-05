"""Wire form for Quantity and Money (DECISIONS pinned algorithm).

The Quantity wire shape is a public stability guarantee (SPEC rules #1/#2):

    {"raw": "<decimal int string>", "decimals": <int>,
     "numeric": "<exact decimal string>", "float": <lossy float>}

``raw`` is a JSON **string**: a JSON integer in a raw-amount field is a
rule #2 violation and is rejected on read. ``numeric`` never uses
scientific notation. ``float`` is display-only and documented lossy;
reads reconstruct from ``raw`` + ``decimals`` ONLY.

Money's wire form is a tagged decimal string (rule #1):

    {"amount": "<exact decimal string>", "currency": "<tag>"}

A float ``amount`` on read is rejected with ``ValidationError``, as are
the non-finite specials (``"NaN"``, ``"sNaN"``, ``"Infinity"``,
``"-Infinity"``). They are not exact decimal amounts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from auradefi.errors import ValidationError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

# Strict wire grammar: ASCII digits only, optional leading '-'. int() and
# Decimal() are far laxer (underscores, whitespace, '+', non-ASCII digits
# like '٥٥', specials like 'NaN'). None of that is a wire amount.
_WIRE_INT = re.compile(r"-?[0-9]+")
_WIRE_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")


def quantity_to_wire(quantity: Quantity) -> dict[str, Any]:
    """Project a ``Quantity`` to its pinned four-field wire dict.

    ``{'raw': str(q.raw), 'decimals': q.decimals, 'numeric': str(q),
    'float': float(q.as_decimal())}``. ``Raw`` is a string, ``numeric``
    is exact with no scientific notation, ``float`` is lossy display-only.
    """
    return {
        "raw": str(quantity.raw),
        "decimals": quantity.decimals,
        "numeric": str(quantity),
        "float": float(quantity.as_decimal()),
    }


def quantity_from_wire(wire: Mapping[str, Any]) -> Quantity:
    """Reconstruct a ``Quantity`` from ``raw`` + ``decimals`` ONLY.

    ``numeric`` and ``float`` are ignored (and may be absent). Raises
    ``ValidationError`` if ``raw`` is not a ``str`` (a JSON-integer raw is
    rejected, rule #2) or ``decimals`` is not an ``int``. ``raw`` must
    match ``-?[0-9]+`` exactly. ASCII digits only, so ``int()``'s laxer
    grammar (``'1_000'``, ``' 5 '``, ``'+5'``, ``'5\\n'``, Arabic-Indic
    digits) never leaks onto the wire.
    """
    raw = wire.get("raw")
    decimals = wire.get("decimals")
    if not isinstance(raw, str):
        raise ValidationError(
            f"wire 'raw' must be a string, got {type(raw).__name__} (rule #2)"
        )
    if isinstance(decimals, bool) or not isinstance(decimals, int):
        raise ValidationError(
            f"wire 'decimals' must be an int, got {type(decimals).__name__}"
        )
    if _WIRE_INT.fullmatch(raw) is None:
        raise ValidationError(
            "wire 'raw' must be an ASCII decimal integer string "
            f"matching -?[0-9]+, got {raw!r}"
        )
    return Quantity(int(raw), decimals)


def money_to_wire(money: Money) -> dict[str, Any]:
    """Project ``Money`` to ``{'amount': <exact str>, 'currency': ...}``.

    ``amount`` is the exact decimal string, never scientific notation.
    """
    return {"amount": format(money.amount, "f"), "currency": money.currency}


def money_from_wire(wire: Mapping[str, Any]) -> Money:
    """Inverse of ``money_to_wire``.

    Raises ``ValidationError`` if ``amount`` is a float (rule #1), does
    not match ``-?[0-9]+(\\.[0-9]+)?`` exactly, ASCII digits only, which
    also excludes the non-finite specials (NaN/sNaN/+/-Infinity) and
    scientific notation before ``Decimal`` ever sees the string, or the
    currency tag is invalid.
    """
    amount = wire.get("amount")
    if not isinstance(amount, str):
        raise ValidationError(
            f"wire 'amount' must be a string, got {type(amount).__name__} (rule #1)"
        )
    if _WIRE_DECIMAL.fullmatch(amount) is None:
        raise ValidationError(
            "wire 'amount' must be an ASCII decimal string matching "
            f"-?[0-9]+(\\.[0-9]+)?, got {amount!r}"
        )
    # The grammar above subsumes both guards below; they stay as defence
    # in depth should the grammar ever loosen.
    try:
        value = Decimal(amount)
    except InvalidOperation as exc:
        raise ValidationError(
            f"wire 'amount' is not a decimal string: {amount!r}"
        ) from exc
    if not value.is_finite():
        raise ValidationError(
            f"wire 'amount' must be a finite decimal string, got {amount!r} (rule #1)"
        )
    return Money(value, wire.get("currency"))
