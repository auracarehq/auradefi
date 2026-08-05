"""Holding and HoldingsReport (SPEC §3.1: Account → Holding[] ≡ Plaid Holding).

PURE models for the portfolio domain, no I/O, no HTTP client. This module
imports only ``auradefi.money``, ``auradefi.errors`` and the stdlib;
tests/portfolio/test_models.py enforces that mechanically.

Phase 1, single-tenant, library-only: an address's balances plus prices
assemble into one ``HoldingsReport`` whose ``total_value`` is an EXACT
Decimal sum (SPEC rule #1, no rounding, no floats, ever).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from auradefi.errors import CurrencyMismatchError, ValidationError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity


@dataclass(frozen=True, slots=True)
class Holding:
    """One asset balance on one account (≡ Plaid Holding).

    ``price`` is the unit USD price; ``value`` is the position USD value.
    Pricing is all-or-nothing: both set (priced) or both ``None``
    (unpriced). Exactly one of the two being ``None`` raises
    ``ValidationError`` at construction.
    """

    caip19: str
    symbol: str | None
    quantity: Quantity
    price: Money | None
    value: Money | None

    def __post_init__(self) -> None:
        """Reject half-priced holdings.

        Raises ``ValidationError`` when exactly one of ``price``/``value``
        is ``None``. Both ``None`` (unpriced) and both set are valid.
        """
        if (self.price is None) != (self.value is None):
            raise ValidationError(
                f"holding {self.caip19!r}: price and value must both be set "
                "or both be None, never one without the other"
            )


@dataclass(frozen=True, slots=True)
class HoldingsReport:
    """All holdings of one (address × chain) with an exact USD total.

    ``as_of_ms`` is an integer ms-epoch timestamp (SPEC §4.4: ms epoch,
    everywhere, always). ``unpriced`` lists the CAIP-19 ids of holdings
    that carry no value, in input order, so a consumer knows exactly what
    the total omits (SPEC §4.4 data_quality spirit: incompleteness is
    first-class, never silent).
    """

    address: str
    chain_id: str
    holdings: tuple[Holding, ...]
    total_value: Money
    unpriced: tuple[str, ...]
    as_of_ms: int

    @classmethod
    def assemble(
        cls,
        address: str,
        chain_id: str,
        holdings: Iterable[Holding],
        as_of_ms: int,
    ) -> HoldingsReport:
        """Assemble a report from holdings; pinned algorithm.

        * ``total_value`` = ``Money`` of the EXACT ``Decimal`` sum of
          ``h.value.amount`` over priced holdings, currency ``"USD"``,
          NO rounding, NO floats (SPEC rule #1), exact past any Decimal
          context precision. ``Decimal("0")`` when nothing is priced.
        * Raises ``CurrencyMismatchError`` if any holding's
          ``value.currency != "USD"``.
        * ``unpriced`` = tuple of ``caip19`` for holdings whose ``value``
          is ``None``, input order preserved.
        * ``holdings`` stored as a tuple in input order.
        """
        stored = tuple(holdings)
        total = Money(Decimal("0"), "USD")
        unpriced: list[str] = []
        for holding in stored:
            if holding.value is None:
                unpriced.append(holding.caip19)
                continue
            if holding.value.currency != "USD":
                raise CurrencyMismatchError(
                    f"holding {holding.caip19!r} is valued in "
                    f"{holding.value.currency!r}; assemble requires 'USD'"
                )
            total = total + holding.value
        return cls(
            address=address,
            chain_id=chain_id,
            holdings=stored,
            total_value=total,
            unpriced=tuple(unpriced),
            as_of_ms=as_of_ms,
        )
