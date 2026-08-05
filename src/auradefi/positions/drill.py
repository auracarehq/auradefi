"""Pure drill: raw positions × prices → valued result (SPEC §5.3, §6.3).

"Separate chain I/O from pricing. ``raw_balances()`` → chain reads only;
``drill(raw, prices)`` → pure, no I/O. Persist raw balances and re-drill
against fresh prices without touching an RPC.". SPEC §5.3, verbatim.
A price tick must not cost a re-scan: :func:`drill` takes only data and
returns only data. This module imports nothing that does I/O.

DECISIONS.md "Drill rounding = NONE": every valuation is
:func:`exact_mul`: context-free coefficient multiplication (sign XOR,
integer coefficient product, exponents added), never rounded to context
precision. The sign convention is pinned there too: an underlying's
value is negative iff ``meta_type == BORROWED`` (unit price stays
positive); ``net_worth = gross_assets - total_debt`` equals the naive
signed sum ALWAYS.

§6.3 projection: :class:`SyntheticHolding` is a LOCAL shape. Positions
may not import portfolio (layering); Phase 5 wiring maps it 1:1 onto the
Plaid ``Holding``. A ``BORROWED`` underlying becomes a NEGATIVE-quantity
holding (consistent with ``tax_lots[].position_type: SHORT``) so a
Plaid-only client summing ``institution_value`` gets the right net
worth: the projection invariant, guarded by its own contract test.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal

from auradefi.errors import (
    CurrencyMismatchError,
    UnknownAssetError,
    ValidationError,
)
from auradefi.money.fiat import Money
from auradefi.positions.models import (
    GroupInfo,
    MetaType,
    Position,
    PositionGroup,
    Underlying,
    make_group,
)


@dataclass(frozen=True, slots=True)
class DrillResult:
    """A fully valued snapshot: positions, groups, and the signed triple.

    ``positions`` are the input positions re-built via
    ``dataclasses.replace`` with every underlying valued; ``groups`` are
    built with ``make_group`` (total computed, never passed) and sorted
    by ``group_id``. ``gross_assets``/``total_debt`` are both >= 0;
    ``net_worth = gross_assets - total_debt``: the explicit triple
    SPEC §4.3 demands (Zerion defect #1 fixed). All Money USD.
    """

    positions: tuple[Position, ...]
    groups: tuple[PositionGroup, ...]
    gross_assets: Money
    total_debt: Money
    net_worth: Money


@dataclass(frozen=True, slots=True)
class SyntheticHolding:
    """One Plaid-shaped holding projected from a valued underlying.

    LOCAL shape (SPEC §6.3). Positions/ may not import portfolio/;
    Phase 5 maps this 1:1 onto the Plaid ``Holding``. ``quantity`` is a
    SIGNED ``Decimal``: negative iff the underlying was ``BORROWED``.
    ``institution_price`` stays positive; ``institution_value`` is
    ``exact_mul(quantity, price)`` so the naive sum is the net worth.
    """

    asset_id: str
    quantity: Decimal
    institution_price: Money
    institution_value: Money


def exact_mul(a: Decimal, b: Decimal) -> Decimal:
    """Context-free exact product of two decimals (DECISIONS.md, pinned).

    Sign = XOR of the operand signs; coefficient = integer product of
    the operand coefficients; exponent = sum of the operand exponents.
    NEVER context-rounded. A 78-digit coefficient survives intact, and
    ``exact_mul(Decimal('10'), Decimal('3584.17'))`` is exactly
    ``Decimal('35841.70')``, trailing zero preserved.
    """
    a_sign, a_digits, a_exponent = a.as_tuple()
    b_sign, b_digits, b_exponent = b.as_tuple()
    a_coefficient = int("".join(map(str, a_digits)))
    b_coefficient = int("".join(map(str, b_digits)))
    digits = tuple(int(char) for char in str(a_coefficient * b_coefficient))
    return Decimal((a_sign ^ b_sign, digits, a_exponent + b_exponent))


def _priced(asset_id: str, prices: Mapping[str, Money]) -> Money:
    """The USD price for ``asset_id``: missing → ``UnknownAssetError``
    naming the CAIP-19; non-USD → ``CurrencyMismatchError``."""
    price = prices.get(asset_id)
    if price is None:
        raise UnknownAssetError(f"no price for {asset_id}")
    if price.currency != "USD":
        raise CurrencyMismatchError(
            f"price for {asset_id} must be 'USD', got {price.currency!r}"
        )
    return price


def _valued(underlying: Underlying, prices: Mapping[str, Money]) -> Underlying:
    """The underlying rebuilt with ``price`` and ``value`` attached:
    value negated iff ``BORROWED``; the unit price stays positive."""
    price = _priced(underlying.asset_id, prices)
    amount = exact_mul(underlying.quantity.as_decimal(), price.amount)
    if underlying.meta_type is MetaType.BORROWED:
        amount = amount.copy_negate()
    return replace(underlying, price=price, value=Money(amount, "USD"))


def _merged_group_info(members: Sequence[Position]) -> GroupInfo | None:
    """The single ``GroupInfo`` the members agree on (``None`` if none
    carry one); two conflicting non-``None`` infos → ``ValidationError``."""
    merged: GroupInfo | None = None
    for position in members:
        info = position.group_info
        if info is None:
            continue
        if merged is None:
            merged = info
        elif info != merged:
            raise ValidationError(
                f"conflicting GroupInfo in group {position.group_id!r}"
            )
    return merged


def drill(raw: Sequence[Position], prices: Mapping[str, Money]) -> DrillResult:
    """Value raw positions against a price map. Pure, no I/O ever.

    Contract (SPEC §5.3; DECISIONS.md sign convention, rounding=NONE):

    * ``prices`` is keyed by canonical CAIP-19; every price must be
      ``'USD'``, anything else raises ``CurrencyMismatchError``;
    * a missing price for any underlying's ``asset_id`` raises
      ``UnknownAssetError`` naming the CAIP-19;
    * each underlying is valued ``exact_mul(quantity.as_decimal(),
      price.amount)``, negated iff ``meta_type`` is ``BORROWED`` (the
      unit ``price`` stays positive), attached via
      ``dataclasses.replace``, inputs are never mutated;
    * ``groups`` are built with ``make_group`` after merging the member
      positions' ``GroupInfo``s (two conflicting non-``None`` infos in
      one group raise ``ValidationError``), sorted by ``group_id``;
    * ``gross_assets`` = exact sum of the non-negative underlying
      values; ``total_debt`` = exact sum of the absolute negative ones
      (both >= 0); ``net_worth = gross_assets - total_debt`` == the
      naive signed sum ALWAYS. All Money USD.
    """
    zero = Money(Decimal("0"), "USD")
    valued: list[Position] = []
    gross = zero
    debt = zero
    for position in raw:
        underlyings = tuple(
            _valued(underlying, prices) for underlying in position.underlyings
        )
        valued.append(replace(position, underlyings=underlyings))
        for underlying in underlyings:
            if underlying.value.amount < 0:
                debt = debt + (-underlying.value)
            else:
                gross = gross + underlying.value
    members_by_group: dict[str, list[Position]] = {}
    for position in valued:
        members_by_group.setdefault(position.group_id, []).append(position)
    groups = tuple(
        make_group(tuple(members), group_info=_merged_group_info(members))
        for _, members in sorted(members_by_group.items())
    )
    return DrillResult(
        positions=tuple(valued),
        groups=groups,
        gross_assets=gross,
        total_debt=debt,
        net_worth=gross - debt,
    )


def project_to_synthetic_holdings(
    result: DrillResult,
) -> tuple[SyntheticHolding, ...]:
    """Project a drilled result onto Plaid-shaped holdings (SPEC §6.3).

    One :class:`SyntheticHolding` per valued underlying, in position
    order then underlying order: ``quantity`` =
    ``quantity.as_decimal()`` negated iff ``BORROWED``;
    ``institution_price`` = the positive unit price;
    ``institution_value`` = ``exact_mul(signed quantity, price.amount)``
    as Money USD.

    INVARIANT (the Phase 4 gate): the exact ``Decimal`` sum of
    ``institution_value`` amounts equals ``result.net_worth.amount``.
    """
    holdings: list[SyntheticHolding] = []
    for position in result.positions:
        for underlying in position.underlyings:
            quantity = underlying.quantity.as_decimal()
            if underlying.meta_type is MetaType.BORROWED:
                quantity = quantity.copy_negate()
            price = underlying.price
            value = exact_mul(quantity, price.amount)
            holdings.append(
                SyntheticHolding(
                    asset_id=underlying.asset_id,
                    quantity=quantity,
                    institution_price=price,
                    institution_value=Money(value, "USD"),
                )
            )
    return tuple(holdings)
