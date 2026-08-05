"""The reporting projection over replayed PnL state — SPEC §9, SPEC §6.2.

Every expected number is hand-computed from the pinned rules in
docs/DECISIONS.md ("Shortfall semantics", "ACB pooling", "None-propagation
(PnL)", "Fraction->Money boundary", "Plaid TaxLot mapping") and asserted as
an exact ``Decimal`` — never a float.

The classic four-event scenario (buy 1@$10, 1@$20, 1@$15, sell 1@$18) is
the discriminator: a method that is not really plugged in cannot produce
8 / 3 / -2 / 3 realised and 15 / 20 / 25 / 20 unrealised from the same
input.

The engine that produces the state under test lives in
``auradefi.accounting.pnl`` and is covered by ``test_pnl.py``; the
generator constants below are duplicated between the two modules on
purpose — the suite has no cross-test imports and no ``tests/__init__.py``.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from auradefi.accounting.lots import AcquisitionEvent, DisposalEvent
from auradefi.accounting.pnl import DisposalRecord, process
from auradefi.accounting.report import AssetPnL, PnLReport, TaxLot, report
from auradefi.errors import CurrencyMismatchError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

ASSET = "eip155:1/erc20:0x" + "0" * 39 + "1"
ASSET_B = "eip155:1/erc20:0x" + "0" * 39 + "2"
WEI = 18


def usd(amount: str | int) -> Money:
    return Money(Decimal(amount), "USD")


def eur(amount: str | int) -> Money:
    return Money(Decimal(amount), "EUR")


def units(count: int, decimals: int = 0) -> Quantity:
    return Quantity(count, decimals)


def buy(at_ms, quantity, cost, tx, asset=ASSET) -> AcquisitionEvent:
    return AcquisitionEvent(at_ms, asset, quantity, cost, tx)


def sell(at_ms, quantity, proceeds, tx, asset=ASSET) -> DisposalEvent:
    return DisposalEvent(at_ms, asset, quantity, proceeds, tx)


#: buys 1 @ $10, 1 @ $20, 1 @ $15, then sells 1 @ $18.
CLASSIC = (
    buy(1_000, units(1), usd(10), "tx_buy_1"),
    buy(2_000, units(1), usd(20), "tx_buy_2"),
    buy(3_000, units(1), usd(15), "tx_buy_3"),
    sell(4_000, units(1), usd(18), "tx_sell_1"),
)

#: FIFO sells the $10 lot, LIFO the $15, HIFO the $20; ACB pools 45/3 = 15.
CLASSIC_REALIZED = {"fifo": "8", "lifo": "3", "hifo": "-2", "acb": "3"}

#: 2 units left, marked at $25 = $50, less the basis each method left
#: behind: 35 / 30 / 25, and 30 from the ACB pool (45 - 15).
CLASSIC_UNREALIZED = {"fifo": "15", "lifo": "20", "hifo": "25", "acb": "20"}


class TestClassicScenario:
    """The method-discriminating vectors, exact to the cent."""

    @pytest.mark.parametrize("method", ["fifo", "lifo", "hifo", "acb"])
    def test_realized_is_the_hand_computed_amount(self, method):
        result = report(process(CLASSIC, method), 5_000, {})
        assert result.realized == Money(Decimal(CLASSIC_REALIZED[method]), "USD")
        assert result.realized.amount == Decimal(CLASSIC_REALIZED[method])
        assert result.missing_realized_count == 0

    @pytest.mark.parametrize("method", ["fifo", "lifo", "hifo", "acb"])
    def test_unrealized_marks_the_two_surviving_units(self, method):
        result = report(process(CLASSIC, method), 5_000, {ASSET: usd(25)})
        assert result.unrealized == Money(Decimal(CLASSIC_UNREALIZED[method]), "USD")

    @pytest.mark.parametrize("method", ["fifo", "lifo", "hifo", "acb"])
    def test_per_asset_mirrors_the_totals(self, method):
        result = report(process(CLASSIC, method), 5_000, {ASSET: usd(25)})
        asset = result.per_asset[ASSET]
        assert asset == AssetPnL(
            realized=Money(Decimal(CLASSIC_REALIZED[method]), "USD"),
            unrealized=Money(Decimal(CLASSIC_UNREALIZED[method]), "USD"),
            quantity_held=Quantity(2, 0),
        )

    def test_the_report_carries_the_method_and_the_as_of_instant(self):
        result = report(process(CLASSIC, "hifo"), 4_242, {})
        assert result.method == "hifo"
        assert result.as_of_ms == 4_242


class TestNonePropagation:
    """DECISIONS "None-propagation (PnL)": an unknown never becomes a zero."""

    def test_unrealized_is_none_when_a_held_asset_lacks_a_mark(self):
        events = (
            buy(1_000, units(1), usd(10), "tx_buy_1"),
            buy(2_000, units(1), usd(30), "tx_buy_2", asset=ASSET_B),
        )
        result = report(process(events, "fifo"), 3_000, {ASSET: usd(25)})
        assert result.unrealized is None
        assert result.per_asset[ASSET].unrealized == usd(15)
        assert result.per_asset[ASSET_B].unrealized is None

    def test_unrealized_is_none_when_a_remaining_lot_lacks_basis(self):
        events = (
            buy(1_000, units(1), None, "tx_buy_1"),
            buy(2_000, units(1), usd(10), "tx_buy_2"),
        )
        result = report(process(events, "fifo"), 3_000, {ASSET: usd(25)})
        assert result.unrealized is None
        assert result.per_asset[ASSET].unrealized is None

    def test_a_flat_asset_needs_no_mark_and_is_an_exact_zero(self):
        events = (
            buy(1_000, units(10**18, WEI), usd(10), "tx_buy_1"),
            sell(2_000, units(10**18, WEI), usd(18), "tx_sell_1"),
        )
        result = report(process(events, "fifo"), 3_000, {})
        assert result.unrealized == usd(0)
        assert result.per_asset[ASSET].unrealized == usd(0)
        assert result.per_asset[ASSET].quantity_held == Quantity(0, WEI)
        assert result.open_lots == ()


class TestMarkCurrency:
    """A mark must be denominated in the currency the stream fixed."""

    def test_a_mark_in_another_currency_is_a_mismatch(self):
        state = process(CLASSIC, "fifo")
        with pytest.raises(CurrencyMismatchError):
            report(state, 5_000, {ASSET: eur(25)})

    def test_an_unpriced_stream_defaults_to_usd(self):
        events = (buy(1_000, units(1), None, "tx_buy_1"),)
        state = process(events, "fifo")
        assert state.currency is None
        result = report(state, 2_000, {})
        assert result.realized == Money(Decimal(0), "USD")


class TestTaxLots:
    """DECISIONS "Plaid TaxLot mapping" — the wire shape, pinned."""

    def test_an_open_lot_maps_to_the_pinned_plaid_fields(self):
        events = (
            buy(1_600_000_000_000, units(2), usd(20), "txn_b0000000"),
            sell(1_600_000_060_000, units(1), usd(25), "txn_s0000000"),
        )
        result = report(
            process(events, "fifo"), 1_600_000_120_000, {ASSET: usd(25)}
        )
        assert result.open_lots == (
            TaxLot(
                institution_lot_id="lot_b065cd6ded99875f",
                original_purchase_datetime=1_600_000_000_000,
                quantity=Decimal("1"),
                purchase_price=usd(10),
                cost_basis=usd(10),
                current_value=usd(25),
                position_type="LONG",
            ),
        )

    def test_current_value_is_none_without_a_mark(self):
        events = (buy(1_000, units(2), usd(20), "tx_buy_1"),)
        (lot,) = report(process(events, "fifo"), 2_000, {}).open_lots
        assert lot.current_value is None
        assert lot.cost_basis == usd(20)
        assert lot.purchase_price == usd(10)

    def test_an_unpriced_lot_has_neither_price_nor_basis(self):
        events = (buy(1_000, units(2), None, "tx_buy_1"),)
        (lot,) = report(process(events, "fifo"), 2_000, {ASSET: usd(7)}).open_lots
        assert lot.purchase_price is None
        assert lot.cost_basis is None
        assert lot.current_value == usd(14)

    def test_open_lots_sort_by_asset_then_time_then_id(self):
        events = (
            buy(2_000, units(1), usd(10), "tx_b", asset=ASSET_B),
            buy(1_000, units(1), usd(10), "tx_a", asset=ASSET_B),
            buy(3_000, units(1), usd(10), "tx_c", asset=ASSET),
            buy(1_000, units(1), usd(10), "tx_d", asset=ASSET),
        )
        ordered = tuple(sorted(events, key=lambda event: event.at_ms))
        lots = report(process(ordered, "fifo"), 9_000, {}).open_lots
        assert [lot.position_type for lot in lots] == ["LONG"] * 4
        # ASSET sorts before ASSET_B, so the asset key outranks time: the
        # 3,000 lot precedes the 1,000 lot of the other asset.
        assert [lot.original_purchase_datetime for lot in lots] == [
            1_000,
            3_000,
            1_000,
            2_000,
        ]

    def test_every_lot_id_is_the_pinned_twenty_character_shape(self):
        lots = report(process(CLASSIC, "fifo"), 5_000, {}).open_lots
        for lot in lots:
            assert lot.institution_lot_id.startswith("lot_")
            assert len(lot.institution_lot_id) == 20
            assert isinstance(lot.original_purchase_datetime, int)


class TestImmutability:
    """A report is a snapshot of state the caller does not own."""

    @pytest.mark.parametrize(
        "instance,attribute",
        [
            (
                DisposalRecord(1, ASSET, Quantity(1, 0), None, None, None, False, ()),
                "at_ms",
            ),
            (AssetPnL(Money(Decimal(0), "USD"), None, Quantity(0, 0)), "realized"),
            (TaxLot("lot_x", 1, Decimal(1), None, None, None), "quantity"),
            (
                PnLReport(1, "fifo", Money(Decimal(0), "USD"), 0, None, {}, ()),
                "method",
            ),
        ],
    )
    def test_report_values_are_frozen(self, instance, attribute):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, attribute, "mutated")

    def test_per_asset_mapping_cannot_be_written_through(self):
        result = report(process(CLASSIC, "fifo"), 5_000, {})
        with pytest.raises(TypeError):
            result.per_asset["x"] = None  # type: ignore[index]


class TestRoundedBasisIsFlagged:
    """RELEASE_0.1.1 §5 #29 — the boundary that rounds must SAY it rounded.

    docs/DECISIONS.md pins the Fraction->Money boundary as "ROUND_HALF_EVEN
    at 28 significant digits with flag `rounded_basis` … rounding exists
    only at this boundary and is always flagged", and
    ``fraction_to_money`` returns ``(money, is_exact)`` with its own
    docstring saying ``is_exact=False`` is "what the caller reports as the
    ``rounded_basis`` flag". Every call site indexed ``[0]`` and dropped
    the bit, so a rounded figure was indistinguishable from an exact one
    and the pinned promise was never kept.
    """

    #: 1 unit bought for $10 and 3 units bought for $10 in one ACB pool:
    #: the per-unit basis is 10/3, whose denominator is not 2^a·5^b, so the
    #: boundary MUST round.
    THIRDS = (
        buy(1_000, units(3), usd(10), "tx_thirds"),
        sell(2_000, units(1), usd(5), "tx_thirds_sell"),
    )

    def test_a_rounded_unrealized_total_is_flagged(self):
        # pins: the report-level flag. A total the caller cannot tell is
        #       inexact is the whole defect — it reads as cent-accurate.
        result = report(process(self.THIRDS, "acb"), 3_000, {ASSET: usd(7)})

        assert result.unrealized is not None
        assert "rounded_basis" in result.flags, (
            f"unrealized={result.unrealized.amount} was rounded at the "
            f"Fraction->Money boundary but flags={result.flags}"
        )

    def test_a_rounded_per_asset_figure_is_flagged(self):
        result = report(process(self.THIRDS, "acb"), 3_000, {ASSET: usd(7)})

        assert "rounded_basis" in result.per_asset[ASSET].flags

    def test_a_rounded_tax_lot_basis_is_flagged(self):
        result = report(process(self.THIRDS, "fifo"), 3_000, {ASSET: usd(7)})

        assert result.open_lots, "the fixture must leave a lot open"
        assert any("rounded_basis" in lot.flags for lot in result.open_lots), (
            "no open lot flagged a rounded basis: "
            f"{[(lot.cost_basis, lot.flags) for lot in result.open_lots]}"
        )

    def test_an_exact_report_carries_no_flag(self):
        # The control. Flagging everything would be as useless as flagging
        # nothing: CLASSIC is exact to the cent by construction.
        result = report(process(CLASSIC, "fifo"), 5_000, {ASSET: usd(25)})

        assert result.flags == ()
        assert result.per_asset[ASSET].flags == ()
        assert all(lot.flags == () for lot in result.open_lots)


class TestAcbPoolVersusLotsIsDiscoverable:
    """Issue #16 — the divergence is deliberate; SILENCE about it was not.

    Under `method="acb"`, `unrealized` subtracts the ACB POOL's cost while
    each open lot reports its own remaining basis, and the two do not agree.
    That is pinned in docs/DECISIONS.md ("ACB pooling") and STATUS.md, and it
    is correct: the pool is what ACB costs with, the lots stay ground truth
    for lot-level reporting.

    What was missing is any way to notice at the point of use. A caller sums
    `TaxLot.cost_basis`, compares it with what `unrealized` implies, finds a
    gap, and concludes the library is broken. So the report now NAMES which
    cost it used and exposes both figures.
    """

    #: 1 @ $10, 1 @ $20, 1 @ $15, then sell 1 @ $18. ACB consumes 45/3 = 15,
    #: leaving a pool of 30; the lots left standing are the $20 and the $15,
    #: summing to 35. The two differ by exactly 5, permanently.
    POOL_BASIS = "30"
    LOTS_BASIS = "35"

    def test_acb_names_the_pool_as_the_basis_source(self):
        # pins: the report says WHICH cost `unrealized` subtracted. Without
        #       this a caller cannot tell a pooled figure from a lot-summed
        #       one, and the two legitimately differ under ACB.
        result = report(process(CLASSIC, "acb"), 5_000, {ASSET: usd(25)})
        assert result.basis_source == "pool"

    @pytest.mark.parametrize("method", ["fifo", "lifo", "hifo"])
    def test_lot_methods_name_the_lots_as_the_basis_source(self, method):
        # The control: only ACB pools. For every other method the two figures
        # agree, and the label must say so rather than being cosmetic.
        result = report(process(CLASSIC, method), 5_000, {ASSET: usd(25)})
        assert result.basis_source == "lots"
        assert result.unrealized_basis == result.open_lots_basis

    def test_both_bases_are_exposed_and_differ_under_acb(self):
        # pins: BOTH numbers are reachable, so the gap is inspectable instead
        #       of being a discrepancy the caller has to reverse-engineer.
        result = report(process(CLASSIC, "acb"), 5_000, {ASSET: usd(25)})

        assert result.unrealized_basis == usd(self.POOL_BASIS)
        assert result.open_lots_basis == usd(self.LOTS_BASIS)
        assert result.unrealized_basis != result.open_lots_basis, (
            "the fixture must actually reach the divergence it documents"
        )

    def test_unrealized_is_the_mark_less_the_pool_not_less_the_lots(self):
        # pins: which of the two figures `unrealized` is derived from. 2 units
        #       marked at $25 = $50; minus the pool's 30 is 20, minus the
        #       lots' 35 would be 15. This is the assertion that would catch
        #       a "fix" that quietly switched ACB to lot-summed basis.
        result = report(process(CLASSIC, "acb"), 5_000, {ASSET: usd(25)})

        assert result.unrealized == usd("20")
        assert result.unrealized != usd("15")

    def test_open_lots_basis_is_none_when_any_open_lot_is_unpriced(self):
        # pins: the sum does not silently treat an unknown basis as zero —
        #       profile rule "incomplete data is DECLARED, never defaulted".
        events = (
            buy(1_000, units(1), usd(10), "tx_priced"),
            buy(2_000, units(1), None, "tx_unpriced"),
        )
        result = report(process(events, "acb"), 3_000, {ASSET: usd(25)})

        assert any(lot.cost_basis is None for lot in result.open_lots)
        assert result.open_lots_basis is None
