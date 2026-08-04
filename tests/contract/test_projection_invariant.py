"""THE PHASE 4 GATE: synthetic Holdings sum to the same net worth
(SPEC §6.3, §11 phase 4, §13 contract tests).

SPEC §6.3 verbatim: "an Aave position supplying 10 ETH and borrowing
5,000 USDC emits two synthetic Holdings — +10 ETH and a NEGATIVE-
quantity USDC Holding. A Plaid-only client sums institution_value and
gets the right net worth." The negative quantity is the ONLY way to
make the naive sum correct (consistent with tax_lots position_type:
SHORT). Get the sign convention wrong and nothing errors — net worth is
silently wrong (§6.1's named casualty). So the invariant is asserted
here by exact Decimal equality, twice, under two price sets.

Deliberately unmirrored (tests/contract/ is mirror-exempt): this file
guards the phase, not one module. Every number below was derived by
hand from DECISIONS.md "Drill rounding = NONE" and hardcoded (rule #5).
"""

from __future__ import annotations

from decimal import Decimal

from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.positions.drill import drill, project_to_synthetic_holdings
from auradefi.positions.models import (
    MetaType,
    Position,
    PositionKind,
    PositionType,
    ProtocolModule,
    Underlying,
    group_id_for,
    position_id,
)

CHAIN = "eip155:1"
ETH = "eip155:1/slip44:60"
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
AAVE_POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
AWETH = "0x4d5f47fa6a74757f35c14fd3a6ef8e3c9bc514e8"

# SPEC §6.3 verbatim: supply 10 ETH, borrow 5,000 USDC.
PRICES = {
    ETH: Money(Decimal("3584.17"), "USD"),
    USDC: Money(Decimal("0.999839"), "USD"),
}
REPRICED = {
    ETH: Money(Decimal("3600"), "USD"),
    USDC: Money(Decimal("0.999839"), "USD"),
}

# Hand-derived goldens (sign XOR, coefficient product, exponents added):
#   10 × 3584.17      = 35841.70        (gross_assets)
#   5000 × 0.999839   = 4999.195000     (total_debt)
#   net worth         = 30842.505
#   10 × 3600 − debt  = 31000.805       (repriced)
GROSS = Decimal("35841.70")
DEBT = Decimal("4999.195")
NET = Decimal("30842.505")
NET_REPRICED = Decimal("31000.805")


def aave_group() -> list[Position]:
    """The §6.3 Aave-like group: supply AppToken + borrow leaf, one
    risk unit (SPEC §4.3: Aave supply = lending+deposit, borrow =
    lending+loan)."""
    group_id = group_id_for("aave-v3", CHAIN, AAVE_POOL)
    supply = Position(
        id=position_id("aave-v3", CHAIN, AWETH),
        adapter_id="aave-v3",
        chain_id=CHAIN,
        contract_address=AWETH,
        kind=PositionKind.APP_TOKEN,
        position_type=PositionType.DEPOSIT,
        protocol_module=ProtocolModule.LENDING,
        group_id=group_id,
        underlyings=(
            Underlying(ETH, Quantity(10 * 10**18, 18), MetaType.SUPPLIED),
        ),
    )
    borrow = Position(
        id=position_id("aave-v3", CHAIN, AAVE_POOL),
        adapter_id="aave-v3",
        chain_id=CHAIN,
        contract_address=AAVE_POOL,
        kind=PositionKind.CONTRACT_POSITION,
        position_type=PositionType.LOAN,
        protocol_module=ProtocolModule.LENDING,
        group_id=group_id,
        underlyings=(
            Underlying(USDC, Quantity(5000 * 10**6, 6), MetaType.BORROWED),
        ),
    )
    return [supply, borrow]


def test_the_signed_triple_is_pinned():
    drilled = drill(aave_group(), PRICES)
    assert drilled.gross_assets.amount == GROSS
    assert drilled.total_debt.amount == DEBT
    assert drilled.net_worth.amount == NET
    assert drilled.gross_assets.currency == "USD"
    assert drilled.total_debt.currency == "USD"
    assert drilled.net_worth.currency == "USD"


def test_two_synthetic_holdings_plus_ten_eth_minus_five_thousand_usdc():
    holdings = project_to_synthetic_holdings(drill(aave_group(), PRICES))
    assert len(holdings) == 2
    by_asset = {holding.asset_id: holding for holding in holdings}

    eth = by_asset[ETH]
    assert eth.quantity == Decimal("10")
    assert eth.quantity > 0
    assert eth.institution_value.amount == Decimal("35841.70")

    usdc = by_asset[USDC]
    assert usdc.quantity == Decimal("-5000")
    assert usdc.quantity < 0  # strictly negative — the Plaid extension
    assert usdc.institution_price.amount == Decimal("0.999839")
    assert usdc.institution_price.amount > 0  # price NEVER goes negative
    assert usdc.institution_value.amount == Decimal("-4999.195000")
    assert usdc.institution_value.currency == "USD"


def test_the_invariant_naive_sum_equals_net_worth_exactly():
    # What a Plaid-only client does: sum institution_value, trust it.
    drilled = drill(aave_group(), PRICES)
    holdings = project_to_synthetic_holdings(drilled)
    total = Decimal("0")
    for holding in holdings:
        total = total + holding.institution_value.amount
    assert isinstance(total, Decimal)
    assert total == drilled.net_worth.amount
    assert total == NET


def test_the_invariant_survives_repricing():
    # Same raw positions, fresh ETH price, no chain reads (SPEC §5.3):
    # the invariant is structural, not a lucky pair of numbers.
    drilled = drill(aave_group(), REPRICED)
    assert drilled.net_worth.amount == NET_REPRICED
    total = Decimal("0")
    for holding in project_to_synthetic_holdings(drilled):
        total = total + holding.institution_value.amount
    assert total == drilled.net_worth.amount
    assert total == NET_REPRICED


def test_the_group_is_one_risk_unit_matching_the_net():
    drilled = drill(aave_group(), PRICES)
    assert len(drilled.groups) == 1
    assert drilled.groups[0].total_value.amount == NET
