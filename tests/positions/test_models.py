"""Contract tests for auradefi.positions.models (SPEC §4.3; DECISIONS.md).

The id literals below were derived INDEPENDENTLY of the code under test,
via ``python3 -c`` over the algorithms pinned in docs/internal/DECISIONS.md:

    "pos_" + sha256(f"{adapter_id}|{chain_id}|{contract_lower}|{discriminator}"
                    .encode()).hexdigest()[:16]
    "grp_" + sha256(f"{adapter_id}|{chain_id}|{group_key}"
                    .encode()).hexdigest()[:16]

A stability contract is a hardcoded string, not a call to the function
under test. The MetaType pairs are the SPEC §4.3 literals, hardcoded —
NEVER imported from decode: the layer contract forbids positions→decode,
and both test trees pin the same seven (name, value) pairs so drift is a
red test, not a debate (DECISIONS.md "Duplication waiver").
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from auradefi.errors import CurrencyMismatchError, ValidationError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.positions.models import (
    Apy,
    GroupInfo,
    MetaType,
    Position,
    PositionGroup,
    PositionKind,
    PositionType,
    ProtocolModule,
    Range,
    Underlying,
    group_id_for,
    make_group,
    position_id,
)

# Derived independently (see module docstring); NEVER regenerate from the
# implementation.
POS_AAVE_AWETH = "pos_baff12a5eafb77f6"  # aave-v3|eip155:1|0x4d5f47...4e8|""
POS_UNIV3_912345 = "pos_447985e390bf1d89"  # uniswap-v3|eip155:1|0xc364...e88|912345
POS_SUSHI_137 = "pos_4d52a69cc1742480"  # sushiswap|eip155:137|0x0769...41f|""
POS_AAVE_DEBT = "pos_456abd2f26e0032b"  # aave-v3|eip155:1|0x87870b...4e2|debt:usdc
GRP_AAVE_POOL = "grp_0f89caffe413b09f"  # aave-v3|eip155:1|0x87870b...4e2
GRP_UNIV2_PAIR = "grp_b351d79d77bc24eb"  # uniswap-v2|eip155:1|0xb4e16d...9dc

MS = 1_754_000_000_000  # ms-epoch, matches the repo's frozen clock era
AWETH = "eip155:1/erc20:0x4d5f47fa6a74757f35c14fd3a6ef8e3c9bc514e8"
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "eip155:1/erc20:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

# SPEC §4.3 MetaType literals, verbatim — the duplication-waiver golden.
META_TYPE_GOLDEN = [
    ("WALLET", "wallet"),
    ("SUPPLIED", "supplied"),
    ("BORROWED", "borrowed"),
    ("CLAIMABLE", "claimable"),
    ("VESTING", "vesting"),
    ("LOCKED", "locked"),
    ("NFT", "nft"),
]


def supplied_underlying(**overrides) -> Underlying:
    """10 aWETH supplied at 3584.17 USD → value 35841.70 USD."""
    fields = {
        "asset_id": AWETH,
        "quantity": Quantity(10 * 10**18, 18),
        "meta_type": MetaType.SUPPLIED,
        "price": Money(Decimal("3584.17"), "USD"),
        "value": Money(Decimal("35841.70"), "USD"),
    }
    fields.update(overrides)
    return Underlying(**fields)


def borrowed_underlying(**overrides) -> Underlying:
    """4999.195 USDC borrowed — value NEGATIVE, unit price positive."""
    fields = {
        "asset_id": USDC,
        "quantity": Quantity(4_999_195_000, 6),
        "meta_type": MetaType.BORROWED,
        "price": Money(Decimal("1.000000"), "USD"),
        "value": Money(Decimal("-4999.195000"), "USD"),
    }
    fields.update(overrides)
    return Underlying(**fields)


def raw_underlying(**overrides) -> Underlying:
    """Raw (undrilled): price and value BOTH None (SPEC §5.3)."""
    fields = {
        "asset_id": WETH,
        "quantity": Quantity(2 * 10**18, 18),
        "meta_type": MetaType.SUPPLIED,
    }
    fields.update(overrides)
    return Underlying(**fields)


def make_position(**overrides) -> Position:
    fields = {
        "id": POS_AAVE_AWETH,
        "adapter_id": "aave-v3",
        "chain_id": "eip155:1",
        "contract_address": "0x4d5f47fa6a74757f35c14fd3a6ef8e3c9bc514e8",
        "kind": PositionKind.APP_TOKEN,
        "position_type": PositionType.DEPOSIT,
        "protocol_module": ProtocolModule.LENDING,
        "group_id": GRP_AAVE_POOL,
        "underlyings": (supplied_underlying(),),
    }
    fields.update(overrides)
    return Position(**fields)


class TestEnums:
    def test_meta_type_pins_the_seven_spec_literals(self):
        # Duplication waiver: hardcoded goldens, never imported from decode.
        assert [(m.name, m.value) for m in MetaType] == META_TYPE_GOLDEN

    def test_position_kind_exactly_two_members(self):
        assert [(m.name, m.value) for m in PositionKind] == [
            ("APP_TOKEN", "app_token"),
            ("CONTRACT_POSITION", "contract_position"),
        ]

    def test_position_type_exactly_seven_members(self):
        assert [(m.name, m.value) for m in PositionType] == [
            ("WALLET", "wallet"),
            ("DEPOSIT", "deposit"),
            ("LOAN", "loan"),
            ("LOCKED", "locked"),
            ("STAKED", "staked"),
            ("REWARD", "reward"),
            ("INVESTMENT", "investment"),
        ]

    def test_protocol_module_exactly_twelve_members(self):
        assert [(m.name, m.value) for m in ProtocolModule] == [
            ("LENDING", "lending"),
            ("LIQUIDITY_POOL", "liquidity_pool"),
            ("YIELD", "yield"),
            ("FARMING", "farming"),
            ("STAKED", "staked"),
            ("LEVERAGED_FARMING", "leveraged_farming"),
            ("VESTING", "vesting"),
            ("REWARDS", "rewards"),
            ("LOCKED", "locked"),
            ("NFT_STAKED", "nft_staked"),
            ("DEPOSIT", "deposit"),
            ("INVESTMENT", "investment"),
        ]

    def test_enums_are_str_enums(self):
        assert isinstance(PositionKind.APP_TOKEN, str)
        assert isinstance(PositionType.LOAN, str)
        assert isinstance(ProtocolModule.LENDING, str)
        assert isinstance(MetaType.BORROWED, str)


class TestApy:
    def test_accepts_apr_and_apy_periods(self):
        for period in ("apr", "apy"):
            apy = Apy(
                rate=Decimal("0.0432"),
                period=period,
                gross=True,
                source="aave-v3",
                as_of_ms=MS,
            )
            assert apy.period == period
            assert apy.rate == Decimal("0.0432")
            assert apy.as_of_ms == MS

    @pytest.mark.parametrize("bad", ["weekly", "APR", "APY", "", "daily"])
    def test_other_periods_raise_validation_error(self, bad):
        with pytest.raises(ValidationError):
            Apy(
                rate=Decimal("0.0432"),
                period=bad,
                gross=False,
                source="aave-v3",
                as_of_ms=MS,
            )

    def test_frozen(self):
        apy = Apy(Decimal("0.01"), "apr", True, "aave-v3", MS)
        with pytest.raises(FrozenInstanceError):
            apy.rate = Decimal("0.02")


class TestUnderlying:
    def test_valued_construction(self):
        u = supplied_underlying()
        assert u.asset_id == AWETH
        assert u.quantity == Quantity(10 * 10**18, 18)
        assert u.meta_type is MetaType.SUPPLIED
        assert u.price == Money(Decimal("3584.17"), "USD")
        assert u.value == Money(Decimal("35841.70"), "USD")

    def test_raw_construction_both_none(self):
        u = raw_underlying()
        assert u.price is None
        assert u.value is None

    def test_price_without_value_raises(self):
        with pytest.raises(ValidationError):
            raw_underlying(price=Money(Decimal("3584.17"), "USD"))

    def test_value_without_price_raises(self):
        with pytest.raises(ValidationError):
            raw_underlying(value=Money(Decimal("7168.34"), "USD"))

    def test_borrowed_value_is_negative_price_positive(self):
        u = borrowed_underlying()
        assert u.value.amount < 0
        assert u.price.amount > 0

    def test_frozen(self):
        u = supplied_underlying()
        with pytest.raises(FrozenInstanceError):
            u.asset_id = USDC


class TestRangeAndGroupInfo:
    def test_range_fields(self):
        r = Range(tick_lower=-887272, tick_upper=887272, in_range=True)
        assert (r.tick_lower, r.tick_upper, r.in_range) == (-887272, 887272, True)

    def test_range_frozen(self):
        r = Range(0, 100, False)
        with pytest.raises(FrozenInstanceError):
            r.in_range = True

    def test_group_info_defaults_all_none(self):
        info = GroupInfo()
        assert info.health_factor is None
        assert info.ltv is None
        assert info.liquidation_price is None

    def test_group_info_fields(self):
        info = GroupInfo(
            health_factor=Decimal("2.5"),
            ltv=Decimal("0.8025"),
            liquidation_price=Money(Decimal("1650.00"), "USD"),
        )
        assert info.health_factor == Decimal("2.5")
        assert info.ltv == Decimal("0.8025")
        assert info.liquidation_price == Money(Decimal("1650.00"), "USD")


class TestPinnedIds:
    """DECISIONS.md pinned algorithms — hardcoded goldens, never derived
    from the functions under test."""

    def test_position_id_lowercases_checksummed_address(self):
        # Golden derived from the LOWERCASED address; checksummed input
        # must produce the identical id.
        checksummed = "0x4d5F47FA6A74757f35C14fD3a6Ef8E3C9BC514E8"
        assert position_id("aave-v3", "eip155:1", checksummed) == POS_AAVE_AWETH
        assert (
            position_id("aave-v3", "eip155:1", checksummed.lower())
            == POS_AAVE_AWETH
        )

    def test_position_id_with_discriminator(self):
        assert (
            position_id(
                "uniswap-v3",
                "eip155:1",
                "0xc36442b4a4522e871399cd717abdd847ab11fe88",
                "912345",
            )
            == POS_UNIV3_912345
        )

    def test_position_id_default_discriminator_is_empty_string(self):
        explicit = position_id(
            "sushiswap", "eip155:137", "0x0769fd68dFb93167989C6f7254cd0D766Fb2841F", ""
        )
        defaulted = position_id(
            "sushiswap", "eip155:137", "0x0769fd68dFb93167989C6f7254cd0D766Fb2841F"
        )
        assert explicit == defaulted == POS_SUSHI_137

    def test_position_id_discriminator_changes_the_id(self):
        assert (
            position_id(
                "uniswap-v3",
                "eip155:1",
                "0xc36442b4a4522e871399cd717abdd847ab11fe88",
            )
            != POS_UNIV3_912345
        )

    def test_group_id_for_golden(self):
        assert (
            group_id_for(
                "aave-v3", "eip155:1", "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
            )
            == GRP_AAVE_POOL
        )
        assert (
            group_id_for(
                "uniswap-v2", "eip155:1", "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"
            )
            == GRP_UNIV2_PAIR
        )

    def test_group_id_for_lowercases_0x_group_key(self):
        # DECISIONS.md: "0x addresses lowercased" covers both id functions.
        assert (
            group_id_for(
                "aave-v3", "eip155:1", "0x87870BCA3F3FD6335C3F4CE8392D69350B4FA4E2"
            )
            == GRP_AAVE_POOL
        )


class TestPositionValidation:
    def test_empty_underlyings_raises(self):
        with pytest.raises(ValidationError):
            make_position(underlyings=())

    def test_frozen(self):
        position = make_position()
        with pytest.raises(FrozenInstanceError):
            position.group_id = GRP_UNIV2_PAIR

    def test_optional_fields_default_to_none(self):
        position = make_position()
        assert position.apy is None
        assert position.range is None
        assert position.group_info is None


class TestPositionValue:
    def test_value_and_unclaimed_fees_are_properties(self):
        assert isinstance(inspect.getattr_static(Position, "value"), property)
        assert isinstance(
            inspect.getattr_static(Position, "unclaimed_fees"), property
        )

    def test_signed_sum_supplied_minus_borrowed(self):
        # 35841.70 + (-4999.195000) == 30842.505 exactly, no rounding.
        position = make_position(
            underlyings=(supplied_underlying(), borrowed_underlying()),
            group_info=GroupInfo(health_factor=Decimal("2.5")),
        )
        assert position.value == Money(Decimal("30842.505"), "USD")

    def test_single_valued_underlying(self):
        position = make_position()
        assert position.value == Money(Decimal("35841.70"), "USD")

    def test_any_unvalued_underlying_makes_value_none(self):
        position = make_position(
            underlyings=(supplied_underlying(), raw_underlying())
        )
        assert position.value is None

    def test_all_raw_underlyings_make_value_none(self):
        position = make_position(underlyings=(raw_underlying(),))
        assert position.value is None

    def test_huge_magnitudes_survive_exactly(self):
        # 10^77-scale — rule #1's named casualty. Decimal(int) is exact;
        # Decimal + Decimal in a test would round at context precision.
        big = supplied_underlying(
            price=Money(Decimal(10**59), "USD"),
            value=Money(Decimal(10**77), "USD"),
        )
        dust = supplied_underlying(
            asset_id=USDC,
            quantity=Quantity(1_000_000, 6),
            price=Money(Decimal("1"), "USD"),
            value=Money(Decimal(1), "USD"),
        )
        position = make_position(underlyings=(big, dust))
        assert position.value == Money(Decimal(10**77 + 1), "USD")

    def test_currency_mismatch_propagates(self):
        eur = supplied_underlying(
            asset_id=USDC,
            price=Money(Decimal("0.92"), "EUR"),
            value=Money(Decimal("9.20"), "EUR"),
        )
        position = make_position(underlyings=(supplied_underlying(), eur))
        with pytest.raises(CurrencyMismatchError):
            position.value


class TestUnclaimedFees:
    def test_returns_exactly_the_claimable_underlyings_in_order(self):
        fee0 = supplied_underlying(
            asset_id=USDC,
            quantity=Quantity(12_345_678, 6),
            meta_type=MetaType.CLAIMABLE,
            price=Money(Decimal("1.000000"), "USD"),
            value=Money(Decimal("12.345678"), "USD"),
        )
        fee1 = supplied_underlying(
            asset_id=WETH,
            quantity=Quantity(5 * 10**15, 18),
            meta_type=MetaType.CLAIMABLE,
            price=Money(Decimal("3584.17"), "USD"),
            value=Money(Decimal("17.92085"), "USD"),
        )
        position = make_position(
            id=POS_UNIV3_912345,
            adapter_id="uniswap-v3",
            contract_address="0xc36442b4a4522e871399cd717abdd847ab11fe88",
            kind=PositionKind.CONTRACT_POSITION,
            position_type=PositionType.DEPOSIT,
            protocol_module=ProtocolModule.LIQUIDITY_POOL,
            underlyings=(supplied_underlying(), fee0, fee1),
            range=Range(tick_lower=-887272, tick_upper=887272, in_range=True),
        )
        assert position.unclaimed_fees == (fee0, fee1)

    def test_empty_when_nothing_claimable(self):
        position = make_position()
        assert position.unclaimed_fees == ()


class TestMakeGroup:
    def test_signature_has_no_total_value_parameter(self):
        # SPEC §4.3 defect #2: group totals are COMPUTED, never supplied.
        parameters = inspect.signature(make_group).parameters
        assert list(parameters) == ["positions", "group_info"]
        assert "total_value" not in parameters
        assert parameters["group_info"].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters["group_info"].default is None

    def _aave_pair(self) -> tuple[Position, Position]:
        supply = make_position()
        borrow = make_position(
            id=POS_AAVE_DEBT,
            contract_address="0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2",
            kind=PositionKind.CONTRACT_POSITION,
            position_type=PositionType.LOAN,
            underlyings=(borrowed_underlying(),),
        )
        return supply, borrow

    def test_computes_total_value_exactly(self):
        supply, borrow = self._aave_pair()
        group = make_group((supply, borrow))
        assert isinstance(group, PositionGroup)
        assert group.group_id == GRP_AAVE_POOL
        assert group.positions == (supply, borrow)
        assert group.total_value == Money(Decimal("30842.505"), "USD")

    def test_group_info_maps_onto_the_group(self):
        supply, borrow = self._aave_pair()
        group = make_group(
            (supply, borrow),
            group_info=GroupInfo(
                health_factor=Decimal("2.5"),
                ltv=Decimal("0.8025"),
                liquidation_price=Money(Decimal("1650.00"), "USD"),
            ),
        )
        assert group.health_factor == Decimal("2.5")
        assert group.ltv == Decimal("0.8025")
        assert group.liquidation_price == Money(Decimal("1650.00"), "USD")

    def test_without_group_info_risk_fields_are_none(self):
        supply, borrow = self._aave_pair()
        group = make_group((supply, borrow))
        assert group.health_factor is None
        assert group.ltv is None
        assert group.liquidation_price is None

    def test_empty_positions_raise(self):
        with pytest.raises(ValidationError):
            make_group(())

    def test_mixed_group_ids_raise(self):
        supply, borrow = self._aave_pair()
        stray = make_position(group_id=GRP_UNIV2_PAIR)
        with pytest.raises(ValidationError):
            make_group((supply, borrow, stray))

    def test_any_unvalued_position_raises(self):
        supply, _ = self._aave_pair()
        undrilled = make_position(underlyings=(raw_underlying(),))
        with pytest.raises(ValidationError):
            make_group((supply, undrilled))

    def test_group_is_frozen(self):
        supply, borrow = self._aave_pair()
        group = make_group((supply, borrow))
        with pytest.raises(FrozenInstanceError):
            group.total_value = Money(Decimal("0"), "USD")
