"""Contract tests for auradefi.positions.drill (SPEC §5.3, §6.3).

DECISIONS.md "Drill rounding = NONE": valuation is context-free
coefficient multiplication, never rounded. Drill is PURE — raw balances
persist and re-drill against fresh prices with zero chain reads; a
price tick must not cost a re-scan. Golden vectors below were derived
by hand from the pinned algorithm and hardcoded (rule #5); pricing is
pinned to block 20_450_000.
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from auradefi.errors import (
    CurrencyMismatchError,
    UnknownAssetError,
    ValidationError,
)
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.positions.drill import (
    DrillResult,
    SyntheticHolding,
    drill,
    exact_mul,
    project_to_synthetic_holdings,
)
from auradefi.positions.models import (
    GroupInfo,
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
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "eip155:1/erc20:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
UNIV2_PAIR = "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"  # USDC/WETH
AAVE_POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
AWETH = "0x4d5f47fa6a74757f35c14fd3a6ef8e3c9bc514e8"

# Pinned @ block 20_450_000 (rule #5: golden fixtures at a block height).
USDC_USD = Money(Decimal("0.999839"), "USD")
WETH_USD = Money(Decimal("3584.17"), "USD")
PRICES = {USDC: USDC_USD, WETH: WETH_USD}


def make_position(adapter_id, contract, underlyings, *, kind, position_type,
                  protocol_module, group_key=None, group_info=None):
    return Position(
        id=position_id(adapter_id, CHAIN, contract),
        adapter_id=adapter_id,
        chain_id=CHAIN,
        contract_address=contract,
        kind=kind,
        position_type=position_type,
        protocol_module=protocol_module,
        group_id=group_id_for(adapter_id, CHAIN, group_key or contract),
        underlyings=tuple(underlyings),
        group_info=group_info,
    )


def univ2_raw() -> Position:
    """UniV2-shaped: SUPPLIED 52000.000000 USDC + SUPPLIED 14.5 WETH."""
    return make_position(
        "uniswap-v2",
        UNIV2_PAIR,
        (
            Underlying(USDC, Quantity(52_000_000_000, 6), MetaType.SUPPLIED),
            Underlying(WETH, Quantity(145 * 10**17, 18), MetaType.SUPPLIED),
        ),
        kind=PositionKind.APP_TOKEN,
        position_type=PositionType.DEPOSIT,
        protocol_module=ProtocolModule.LIQUIDITY_POOL,
    )


def cdp_raw() -> Position:
    """CDP: SUPPLIED 2 WETH + BORROWED 1000.5 USDC in one leaf."""
    return make_position(
        "aave-v3",
        AAVE_POOL,
        (
            Underlying(WETH, Quantity(2 * 10**18, 18), MetaType.SUPPLIED),
            Underlying(USDC, Quantity(1_000_500_000, 6), MetaType.BORROWED),
        ),
        kind=PositionKind.CONTRACT_POSITION,
        position_type=PositionType.LOAN,
        protocol_module=ProtocolModule.LENDING,
    )


def aave_pair(supply_info=None, borrow_info=None):
    """Two positions sharing one Aave group (risk unit = the Pool)."""
    supply = make_position(
        "aave-v3",
        AWETH,
        (Underlying(WETH, Quantity(2 * 10**18, 18), MetaType.SUPPLIED),),
        kind=PositionKind.APP_TOKEN,
        position_type=PositionType.DEPOSIT,
        protocol_module=ProtocolModule.LENDING,
        group_key=AAVE_POOL,
        group_info=supply_info,
    )
    borrow = make_position(
        "aave-v3",
        AAVE_POOL,
        (Underlying(USDC, Quantity(1_000_500_000, 6), MetaType.BORROWED),),
        kind=PositionKind.CONTRACT_POSITION,
        position_type=PositionType.LOAN,
        protocol_module=ProtocolModule.LENDING,
        group_key=AAVE_POOL,
        group_info=borrow_info,
    )
    return supply, borrow


class TestExactMul:
    def test_golden_vectors_pin_the_exact_forms(self):
        # Derived by hand: 10 × 358417·10⁻² and 5000 × 999839·10⁻⁶.
        a = exact_mul(Decimal("10"), Decimal("3584.17"))
        assert a == Decimal("35841.70")
        assert str(a) == "35841.70"  # trailing zero preserved
        b = exact_mul(Decimal("5000"), Decimal("0.999839"))
        assert b == Decimal("4999.195000")
        assert str(b) == "4999.195000"

    def test_sign_is_xor_of_operand_signs(self):
        assert exact_mul(Decimal("-2"), Decimal("3")) == Decimal("-6")
        assert exact_mul(Decimal("-2"), Decimal("-3")) == Decimal("6")
        assert exact_mul(Decimal("2"), Decimal("3")) == Decimal("6")

    def test_exponents_add_and_trailing_zeros_survive(self):
        # 15·10⁻¹ × 20·10⁻² → 300·10⁻³, NOT '0.3'.
        assert str(exact_mul(Decimal("1.5"), Decimal("0.20"))) == "0.300"

    def test_never_context_rounded(self):
        # 30 significant digits — the default 28-digit context would
        # destroy the tail; the coefficient product must not.
        ones = Decimal("1" * 30)
        assert str(exact_mul(ones, Decimal("3"))) == "3" * 30

    def test_huge_scale_survives_exactly(self):
        big = Quantity(10**77, 18).as_decimal()
        assert exact_mul(big, Decimal("2")) == Decimal(2 * 10**59)


class TestDrillGoldenUniV2:
    def test_underlying_values_and_prices_attached(self):
        drilled = drill([univ2_raw()], PRICES)
        usdc_leg, weth_leg = drilled.positions[0].underlyings
        assert usdc_leg.value == Money(Decimal("51991.628"), "USD")
        assert usdc_leg.price == USDC_USD
        assert weth_leg.value == Money(Decimal("51970.465"), "USD")
        assert weth_leg.price == WETH_USD

    def test_position_value_golden(self):
        drilled = drill([univ2_raw()], PRICES)
        value = drilled.positions[0].value
        assert value.amount == Decimal("103962.093")
        assert value.currency == "USD"

    def test_signed_triple_when_nothing_is_borrowed(self):
        drilled = drill([univ2_raw()], PRICES)
        assert drilled.gross_assets.amount == Decimal("103962.093")
        assert drilled.total_debt.amount == Decimal("0")
        assert drilled.net_worth.amount == Decimal("103962.093")
        assert {m.currency for m in (
            drilled.gross_assets, drilled.total_debt, drilled.net_worth
        )} == {"USD"}

    def test_raw_input_is_never_mutated(self):
        raw = univ2_raw()
        drilled = drill([raw], PRICES)
        assert raw.underlyings[0].price is None
        assert raw.underlyings[0].value is None
        assert drilled.positions[0] is not raw  # dataclasses.replace

    def test_one_group_with_computed_total(self):
        raw = univ2_raw()
        drilled = drill([raw], PRICES)
        assert len(drilled.groups) == 1
        group = drilled.groups[0]
        assert group.group_id == raw.group_id
        assert group.total_value == drilled.positions[0].value
        assert group.health_factor is None

    def test_redrill_with_fresh_prices_and_no_chain_seam(self):
        # SPEC §5.3: a price tick must not cost a re-scan. drill cannot
        # even RECEIVE a ContractReader — its signature is data-only.
        assert list(inspect.signature(drill).parameters) == ["raw", "prices"]
        raw = univ2_raw()
        first = drill([raw], PRICES)
        repriced = drill([raw], {USDC: USDC_USD, WETH: Money(Decimal("3600"), "USD")})
        assert repriced.net_worth.amount == Decimal("104191.628")
        assert repriced.net_worth.amount != first.net_worth.amount


class TestDrillSigns:
    def test_borrowed_value_negative_unit_price_positive(self):
        drilled = drill([cdp_raw()], PRICES)
        borrowed = drilled.positions[0].underlyings[1]
        assert borrowed.value.amount == Decimal("-1000.3389195")
        assert borrowed.value.amount < 0
        assert borrowed.price == USDC_USD  # stays positive

    def test_triple_equals_the_naive_signed_sum(self):
        drilled = drill([cdp_raw()], PRICES)
        assert drilled.gross_assets.amount == Decimal("7168.34")
        assert drilled.total_debt.amount == Decimal("1000.3389195")
        assert drilled.net_worth.amount == Decimal("6168.0010805")
        assert drilled.gross_assets.amount >= 0
        assert drilled.total_debt.amount >= 0
        naive = Decimal("0")
        for underlying in drilled.positions[0].underlyings:
            naive = naive + underlying.value.amount
        assert naive == drilled.net_worth.amount


class TestDrillErrors:
    def test_missing_price_raises_unknown_asset_naming_the_caip19(self):
        with pytest.raises(UnknownAssetError) as excinfo:
            drill([cdp_raw()], {WETH: WETH_USD})  # no USDC price
        assert USDC in str(excinfo.value)

    def test_non_usd_price_raises_currency_mismatch(self):
        eur_prices = {WETH: WETH_USD, USDC: Money(Decimal("0.92"), "EUR")}
        with pytest.raises(CurrencyMismatchError):
            drill([cdp_raw()], eur_prices)


class TestGroupInfoMerging:
    def test_one_members_info_surfaces_on_the_group(self):
        info = GroupInfo(health_factor=Decimal("5.8125"), ltv=Decimal("0.8000"))
        drilled = drill(aave_pair(borrow_info=info), PRICES)
        assert len(drilled.groups) == 1
        assert drilled.groups[0].health_factor == Decimal("5.8125")
        assert drilled.groups[0].ltv == Decimal("0.8000")

    def test_equal_infos_on_both_members_merge_cleanly(self):
        info = GroupInfo(health_factor=Decimal("5.8125"), ltv=Decimal("0.8000"))
        drilled = drill(aave_pair(supply_info=info, borrow_info=info), PRICES)
        assert drilled.groups[0].health_factor == Decimal("5.8125")

    def test_conflicting_infos_raise_validation_error(self):
        with pytest.raises(ValidationError):
            drill(
                aave_pair(
                    supply_info=GroupInfo(health_factor=Decimal("2")),
                    borrow_info=GroupInfo(health_factor=Decimal("5.8125")),
                ),
                PRICES,
            )

    def test_group_total_is_computed_never_passed(self):
        drilled = drill(aave_pair(), PRICES)
        assert drilled.groups[0].total_value.amount == Decimal("6168.0010805")

    def test_groups_sorted_by_group_id(self):
        drilled = drill([univ2_raw(), *aave_pair()], PRICES)
        group_ids = [group.group_id for group in drilled.groups]
        assert group_ids == sorted(group_ids)
        assert len(group_ids) == 2


class TestShapes:
    def test_drill_result_is_frozen(self):
        drilled = drill([univ2_raw()], PRICES)
        with pytest.raises(FrozenInstanceError):
            drilled.net_worth = Money(Decimal("0"), "USD")

    def test_synthetic_holding_is_frozen(self):
        holding = SyntheticHolding(
            asset_id=USDC,
            quantity=Decimal("1"),
            institution_price=Money(Decimal("1"), "USD"),
            institution_value=Money(Decimal("1"), "USD"),
        )
        with pytest.raises(FrozenInstanceError):
            holding.quantity = Decimal("2")

    def test_empty_raw_drills_to_a_zero_usd_triple(self):
        drilled = drill([], PRICES)
        assert drilled.positions == ()
        assert drilled.groups == ()
        for money in (drilled.gross_assets, drilled.total_debt, drilled.net_worth):
            assert money.amount == Decimal("0")
            assert money.currency == "USD"


class TestProjection:
    def test_one_holding_per_valued_underlying_in_order(self):
        holdings = project_to_synthetic_holdings(drill([cdp_raw()], PRICES))
        assert isinstance(holdings, tuple)
        assert [holding.asset_id for holding in holdings] == [WETH, USDC]

    def test_supplied_holding_positive(self):
        weth_holding = project_to_synthetic_holdings(drill([cdp_raw()], PRICES))[0]
        assert weth_holding.quantity == Decimal("2")
        assert weth_holding.quantity > 0
        assert weth_holding.institution_price == WETH_USD
        assert weth_holding.institution_value.amount == Decimal("7168.34")
        assert weth_holding.institution_value.currency == "USD"

    def test_borrowed_holding_negative_quantity_positive_price(self):
        usdc_holding = project_to_synthetic_holdings(drill([cdp_raw()], PRICES))[1]
        assert usdc_holding.quantity == Decimal("-1000.5")
        assert usdc_holding.quantity < 0
        assert usdc_holding.institution_price == USDC_USD
        assert usdc_holding.institution_price.amount > 0
        assert usdc_holding.institution_value.amount == Decimal("-1000.3389195")

    def test_naive_sum_equals_net_worth(self):
        drilled = drill([univ2_raw(), cdp_raw()], PRICES)
        holdings = project_to_synthetic_holdings(drilled)
        assert len(holdings) == 4
        total = Decimal("0")
        for holding in holdings:
            total = total + holding.institution_value.amount
        assert total == drilled.net_worth.amount

    def test_huge_quantity_boundary_survives_projection(self):
        whale = make_position(
            "uniswap-v2",
            UNIV2_PAIR,
            (Underlying(WETH, Quantity(10**77, 18), MetaType.SUPPLIED),),
            kind=PositionKind.APP_TOKEN,
            position_type=PositionType.DEPOSIT,
            protocol_module=ProtocolModule.LIQUIDITY_POOL,
        )
        drilled = drill([whale], {WETH: Money(Decimal("2"), "USD")})
        assert drilled.net_worth.amount == Decimal(2 * 10**59)
        (holding,) = project_to_synthetic_holdings(drilled)
        assert holding.institution_value.amount == Decimal(2 * 10**59)
