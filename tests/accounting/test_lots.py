"""Lot ledger, taxable events, and the exact-math boundary (SPEC §9).

Golden vectors here are recomputed from docs/internal/DECISIONS.md by hand, not by
calling the code under test:

* lot id, ``"lot_" + sha256(f"{tx}|{asset}|{seq}").hexdigest()[:16]``
* basis, exact ``Fraction`` proration, never a float, never rounded
* boundary, exact Decimal iff the reduced denominator is ``2**a * 5**b``

The numbers are the point. A PnL suite that never asserts a cent is the
Zapper failure mode (1,010 fetchers, 3 test files).
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from auradefi.accounting.lots import (
    SIGNIFICANT_DIGITS,
    AcquisitionEvent,
    DisposalEvent,
    Lot,
    LotLedger,
    derive_events,
    exact_mul,
    fraction_to_money,
    lot_id,
)
from auradefi.errors import (
    CurrencyMismatchError,
    DecimalsMismatchError,
    ValidationError,
)
from auradefi.ledger.models import Direction, Entry, LedgerTransaction
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

ETH = "eip155:1/slip44:60"
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
SPAM = "eip155:1/erc20:0xdeadbeef"
ASSET = "eip155:1/erc20:0xabc"

CONFIRMED_AT = 1_700_000_000_000
INITIATED_AT = 1_699_999_999_000

# Recomputed by hand from the DECISIONS pin (see test_lot_id_* below).
LOT_ID_SEQ_0 = "lot_f462071ebe50c97e"  # txn_ab12|eip155:1/erc20:0xabc|0
LOT_ID_SEQ_1 = "lot_be16c090db5289af"  # txn_ab12|eip155:1/erc20:0xabc|1
LOT_ID_OTHER_TX = "lot_7a9409ca314fc0e7"  # txn_cd34|eip155:1/erc20:0xabc|0
LOT_ID_OTHER_ASSET = "lot_8b55628a88092eb0"  # txn_ab12|eip155:1/slip44:60|0

USD_24 = Decimal("24")


def _acquisition(
    *,
    at_ms: int = CONFIRMED_AT,
    asset_id: str = ASSET,
    raw: int = 2,
    decimals: int = 0,
    cost: str | None = "24",
    currency: str = "USD",
    source_tx_id: str = "txn_ab12",
) -> AcquisitionEvent:
    return AcquisitionEvent(
        at_ms=at_ms,
        asset_id=asset_id,
        quantity=Quantity(raw, decimals),
        cost=None if cost is None else Money(Decimal(cost), currency),
        source_tx_id=source_tx_id,
    )


def _transaction(
    *,
    entries: tuple[Entry, ...],
    tx_id: str = "txn_ab12",
    initiated_at: int = INITIATED_AT,
    confirmed_at: int | None = CONFIRMED_AT,
    removed: bool = False,
) -> LedgerTransaction:
    return LedgerTransaction(
        id=tx_id,
        chain_id="eip155:1",
        tx_hash="0xfeed",
        account_id="acc_1",
        block_number=18_000_000,
        initiated_at=initiated_at,
        confirmed_at=confirmed_at,
        entries=entries,
        removed=removed,
    )


def _mixed_entries() -> tuple[Entry, ...]:
    """IN 2 ETH, OUT 1 USDC, SELF 5 SPAM: one of each direction."""
    return (
        Entry(asset_id=ETH, quantity=Quantity(2 * 10**18, 18), direction=Direction.IN),
        Entry(asset_id=USDC, quantity=Quantity(1_000_000, 6), direction=Direction.OUT),
        Entry(asset_id=SPAM, quantity=Quantity(5, 0), direction=Direction.SELF),
    )


def _oldest_first(lots, needed):
    """A minimal FIFO-shaped selector: this module owns the mechanism, the
    costing methods own the policy, so the tests bring their own plan."""
    plan = []
    left = needed.raw
    for lot in lots:
        if left <= 0:
            break
        take = min(lot.quantity_remaining.raw, left)
        plan.append((lot, Quantity(take, needed.decimals)))
        left -= take
    return plan


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------


def test_acquisition_event_carries_a_fee_inclusive_total_and_is_frozen():
    event = AcquisitionEvent(
        at_ms=CONFIRMED_AT,
        asset_id=ASSET,
        quantity=Quantity(2, 0),
        cost=Money(USD_24, "USD"),
        source_tx_id="txn_ab12",
    )
    assert event.at_ms == CONFIRMED_AT
    assert event.asset_id == ASSET
    assert event.quantity == Quantity(2, 0)
    assert event.cost == Money(Decimal("24"), "USD")
    assert event.source_tx_id == "txn_ab12"
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.at_ms = 1


def test_disposal_event_carries_proceeds_and_is_frozen():
    event = DisposalEvent(
        at_ms=CONFIRMED_AT,
        asset_id=ASSET,
        quantity=Quantity(1, 0),
        proceeds=Money(Decimal("31.50"), "USD"),
        source_tx_id="txn_ab12",
    )
    assert event.proceeds == Money(Decimal("31.50"), "USD")
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.proceeds = None


def test_events_carry_no_price_when_none_is_supplied():
    assert _acquisition(cost=None).cost is None
    disposal = DisposalEvent(
        at_ms=CONFIRMED_AT,
        asset_id=ASSET,
        quantity=Quantity(1, 0),
        proceeds=None,
        source_tx_id="txn_ab12",
    )
    assert disposal.proceeds is None


@pytest.mark.parametrize("raw", [0, -1, -(10**77)])
def test_acquisition_quantity_must_be_positive(raw):
    with pytest.raises(ValidationError):
        AcquisitionEvent(
            at_ms=CONFIRMED_AT,
            asset_id=ASSET,
            quantity=Quantity(raw, 0),
            cost=None,
            source_tx_id="txn_ab12",
        )


@pytest.mark.parametrize("raw", [0, -1, -(10**77)])
def test_disposal_quantity_must_be_positive(raw):
    with pytest.raises(ValidationError):
        DisposalEvent(
            at_ms=CONFIRMED_AT,
            asset_id=ASSET,
            quantity=Quantity(raw, 0),
            proceeds=None,
            source_tx_id="txn_ab12",
        )


def test_event_quantity_must_be_a_quantity_not_a_decimal():
    with pytest.raises(ValidationError):
        AcquisitionEvent(
            at_ms=CONFIRMED_AT,
            asset_id=ASSET,
            quantity=Decimal("2"),
            cost=None,
            source_tx_id="txn_ab12",
        )


# --------------------------------------------------------------------------
# lot id: the wire contract (Plaid institution_lot_id)
# --------------------------------------------------------------------------


def test_lot_id_matches_the_hand_recomputed_pin():
    recomputed = (
        "lot_"
        + hashlib.sha256(b"txn_ab12|eip155:1/erc20:0xabc|0").hexdigest()[:16]
    )
    assert recomputed == LOT_ID_SEQ_0
    assert lot_id("txn_ab12", ASSET, 0) == LOT_ID_SEQ_0


def test_open_lot_stamps_the_pinned_id_and_increments_seq_per_tx_and_asset():
    ledger = LotLedger(ASSET)
    first = ledger.open_lot(_acquisition())
    second = ledger.open_lot(_acquisition())
    assert first.lot_id == LOT_ID_SEQ_0
    assert second.lot_id == LOT_ID_SEQ_1
    assert first.lot_id != second.lot_id
    assert lot_id("txn_ab12", ASSET, 1) == LOT_ID_SEQ_1


def test_seq_restarts_for_a_different_source_transaction():
    ledger = LotLedger(ASSET)
    ledger.open_lot(_acquisition())
    other = ledger.open_lot(_acquisition(source_tx_id="txn_cd34"))
    assert other.lot_id == LOT_ID_OTHER_TX


def test_lot_id_changes_with_the_asset():
    assert lot_id("txn_ab12", ETH, 0) == LOT_ID_OTHER_ASSET
    assert LOT_ID_OTHER_ASSET != LOT_ID_SEQ_0


def test_seq_counts_exhausted_lots_so_ids_are_never_reused():
    ledger = LotLedger(ASSET)
    first = ledger.open_lot(_acquisition())
    ledger.consume(Quantity(2, 0), _oldest_first)
    assert first.quantity_remaining == Quantity(0, 0)
    assert ledger.open_lot(_acquisition()).lot_id == LOT_ID_SEQ_1


# --------------------------------------------------------------------------
# derive_events. Is_internal_transfer is not optional (SPEC §9)
# --------------------------------------------------------------------------


def test_derive_events_maps_in_and_out_and_emits_nothing_for_self():
    events = derive_events([_transaction(entries=_mixed_entries())], frozenset())
    assert len(events) == 2
    acquisition, disposal = events
    assert isinstance(acquisition, AcquisitionEvent)
    assert acquisition.asset_id == ETH
    assert acquisition.quantity == Quantity(2 * 10**18, 18)
    assert acquisition.cost is None
    assert acquisition.source_tx_id == "txn_ab12"
    assert isinstance(disposal, DisposalEvent)
    assert disposal.asset_id == USDC
    assert disposal.quantity == Quantity(1_000_000, 6)
    assert disposal.proceeds is None
    assert all(event.asset_id != SPAM for event in events)


def test_an_internal_transfer_id_skips_the_entire_transaction():
    transaction = _transaction(entries=_mixed_entries())
    assert derive_events([transaction], frozenset({"txn_ab12"})) == ()


def test_a_removed_transaction_yields_no_events():
    transaction = _transaction(entries=_mixed_entries(), removed=True)
    assert derive_events([transaction], frozenset()) == ()


def test_event_time_prefers_confirmed_at():
    events = derive_events([_transaction(entries=_mixed_entries())], frozenset())
    assert [event.at_ms for event in events] == [CONFIRMED_AT, CONFIRMED_AT]


def test_event_time_falls_back_to_initiated_at_when_unconfirmed():
    transaction = _transaction(entries=_mixed_entries(), confirmed_at=None)
    events = derive_events([transaction], frozenset())
    assert [event.at_ms for event in events] == [INITIATED_AT, INITIATED_AT]


def test_events_are_sorted_by_time_stably_preserving_input_order_on_ties():
    late = _transaction(
        entries=(
            Entry(asset_id=ETH, quantity=Quantity(1, 0), direction=Direction.IN),
            Entry(asset_id=USDC, quantity=Quantity(2, 0), direction=Direction.IN),
        ),
        tx_id="txn_late",
        confirmed_at=CONFIRMED_AT + 5_000,
    )
    early = _transaction(
        entries=(Entry(asset_id=SPAM, quantity=Quantity(3, 0), direction=Direction.IN),),
        tx_id="txn_early",
        confirmed_at=CONFIRMED_AT,
    )
    events = derive_events([late, early], frozenset())
    assert [event.at_ms for event in events] == [
        CONFIRMED_AT,
        CONFIRMED_AT + 5_000,
        CONFIRMED_AT + 5_000,
    ]
    assert [event.asset_id for event in events] == [SPAM, ETH, USDC]


def test_derive_events_over_nothing_is_nothing():
    assert derive_events([], frozenset()) == ()
    assert derive_events([_transaction(entries=())], frozenset()) == ()


def test_internal_transfer_ids_defaults_to_empty():
    assert len(derive_events([_transaction(entries=_mixed_entries())])) == 2


# --------------------------------------------------------------------------
# LotLedger: opening
# --------------------------------------------------------------------------


def test_open_lot_initialises_remaining_units_and_exact_basis():
    ledger = LotLedger(ASSET)
    lot = ledger.open_lot(_acquisition())
    assert ledger.asset_id == ASSET
    assert lot.asset_id == ASSET
    assert lot.opened_at_ms == CONFIRMED_AT
    assert lot.source_tx_id == "txn_ab12"
    assert lot.quantity_original == Quantity(2, 0)
    assert lot.quantity_remaining == Quantity(2, 0)
    assert lot.cost_total == Money(Decimal("24"), "USD")
    assert lot.cost_remaining == Fraction(24)
    assert isinstance(lot.cost_remaining, Fraction)


def test_an_unpriced_acquisition_leaves_both_basis_fields_none():
    lot = LotLedger(ASSET).open_lot(_acquisition(cost=None))
    assert lot.cost_total is None
    assert lot.cost_remaining is None


def test_lots_keep_open_order_and_open_lots_drops_the_exhausted():
    ledger = LotLedger(ASSET)
    first = ledger.open_lot(_acquisition())
    second = ledger.open_lot(_acquisition(source_tx_id="txn_cd34"))
    assert ledger.lots == (first, second)
    assert ledger.open_lots == (first, second)
    ledger.consume(Quantity(2, 0), _oldest_first)
    assert ledger.lots == (first, second)
    assert ledger.open_lots == (second,)


def test_open_lot_rejects_an_event_for_another_asset():
    with pytest.raises(ValidationError):
        LotLedger(ASSET).open_lot(_acquisition(asset_id=ETH))


def test_open_lot_rejects_a_second_scale_for_the_same_asset():
    ledger = LotLedger(ASSET)
    ledger.open_lot(_acquisition(raw=2, decimals=0))
    with pytest.raises(DecimalsMismatchError):
        ledger.open_lot(_acquisition(raw=2 * 10**18, decimals=18))


def test_open_lot_rejects_a_second_cost_currency():
    ledger = LotLedger(ASSET)
    ledger.open_lot(_acquisition(cost="24", currency="USD"))
    with pytest.raises(CurrencyMismatchError):
        ledger.open_lot(_acquisition(cost="24", currency="EUR"))


# --------------------------------------------------------------------------
# LotLedger: consuming (exact proration, shortfall never raises)
# --------------------------------------------------------------------------


def test_consume_prorates_basis_exactly():
    ledger = LotLedger(ASSET)
    lot = ledger.open_lot(_acquisition(raw=2, cost="24"))
    consumed, shortfall = ledger.consume(Quantity(1, 0), _oldest_first)
    assert shortfall == Quantity(0, 0)
    assert len(consumed) == 1
    taken_lot, take, portion = consumed[0]
    assert taken_lot is lot
    assert take == Quantity(1, 0)
    assert portion == Fraction(12)
    assert lot.cost_remaining == Fraction(12)
    assert lot.quantity_remaining.raw == 1
    assert lot.quantity_original == Quantity(2, 0)
    assert lot.cost_total == Money(Decimal("24"), "USD")


def test_consume_beyond_the_held_lots_reports_a_shortfall_and_never_raises():
    ledger = LotLedger(ASSET)
    lot = ledger.open_lot(_acquisition(raw=2, cost="24"))
    consumed, shortfall = ledger.consume(Quantity(3, 0), _oldest_first)
    assert consumed == [(lot, Quantity(2, 0), Fraction(24))]
    assert shortfall == Quantity(1, 0)
    assert lot.quantity_remaining == Quantity(0, 0)
    assert lot.cost_remaining == Fraction(0)


def test_consume_against_an_empty_ledger_is_all_shortfall():
    consumed, shortfall = LotLedger(ASSET).consume(Quantity(7, 0), _oldest_first)
    assert consumed == []
    assert shortfall == Quantity(7, 0)


def test_thirds_leave_no_basis_drift():
    """Three exact thirds of a 10 USD lot sum back to 10. The whole
    reason basis is a Fraction and not a Decimal."""
    ledger = LotLedger(ASSET)
    lot = ledger.open_lot(_acquisition(raw=3, cost="10"))
    portions = []
    for _ in range(3):
        consumed, shortfall = ledger.consume(Quantity(1, 0), _oldest_first)
        assert shortfall == Quantity(0, 0)
        portions.append(consumed[0][2])
    assert portions == [Fraction(10, 3), Fraction(10, 3), Fraction(10, 3)]
    assert sum(portions) == Fraction(10)
    assert lot.cost_remaining == Fraction(0)
    assert lot.quantity_remaining == Quantity(0, 0)


def test_consume_spans_several_lots_prorating_each_against_its_own_original():
    ledger = LotLedger(ASSET)
    first = ledger.open_lot(_acquisition(raw=2, cost="24"))
    second = ledger.open_lot(_acquisition(raw=4, cost="10", source_tx_id="txn_cd34"))
    consumed, shortfall = ledger.consume(Quantity(3, 0), _oldest_first)
    assert shortfall == Quantity(0, 0)
    assert consumed == [
        (first, Quantity(2, 0), Fraction(24)),
        (second, Quantity(1, 0), Fraction(10, 4)),
    ]
    assert first.cost_remaining == Fraction(0)
    assert second.cost_remaining == Fraction(15, 2)
    assert second.quantity_remaining == Quantity(3, 0)


def test_consuming_an_unpriced_lot_yields_a_none_portion():
    ledger = LotLedger(ASSET)
    lot = ledger.open_lot(_acquisition(raw=2, cost=None))
    consumed, _ = ledger.consume(Quantity(1, 0), _oldest_first)
    assert consumed == [(lot, Quantity(1, 0), None)]
    assert lot.cost_remaining is None
    assert lot.quantity_remaining == Quantity(1, 0)


def test_the_selector_only_ever_sees_lots_with_units_remaining():
    seen: list[tuple] = []

    def _recording(lots, needed):
        seen.append((tuple(lot.lot_id for lot in lots), needed))
        return _oldest_first(lots, needed)

    ledger = LotLedger(ASSET)
    ledger.open_lot(_acquisition(raw=2, cost="24"))
    second = ledger.open_lot(_acquisition(raw=2, cost="30"))
    ledger.consume(Quantity(2, 0), _recording)
    ledger.consume(Quantity(1, 0), _recording)
    assert seen == [
        ((LOT_ID_SEQ_0, LOT_ID_SEQ_1), Quantity(2, 0)),
        ((LOT_ID_SEQ_1,), Quantity(1, 0)),
    ]
    assert second.quantity_remaining == Quantity(1, 0)


def test_consume_survives_a_ten_to_the_seventy_seven_scale_lot():
    huge = 10**77
    third = 33333333333333333333333333333333333333333333333333333333333333333333333333333
    assert third == huge // 3
    ledger = LotLedger(ASSET)
    lot = ledger.open_lot(_acquisition(raw=huge, decimals=18, cost="1"))
    consumed, shortfall = ledger.consume(Quantity(third, 18), _oldest_first)
    assert shortfall == Quantity(0, 18)
    assert consumed[0][2] == Fraction(third, huge)
    assert lot.cost_remaining == Fraction(1) - Fraction(third, huge)
    assert lot.quantity_remaining == Quantity(huge - third, 18)


def test_consume_rejects_a_scale_the_ledger_does_not_hold():
    ledger = LotLedger(ASSET)
    ledger.open_lot(_acquisition(raw=2, decimals=0))
    with pytest.raises(DecimalsMismatchError):
        ledger.consume(Quantity(1, 18), _oldest_first)


def test_consume_rejects_a_plan_that_overreaches_a_lot():
    ledger = LotLedger(ASSET)
    ledger.open_lot(_acquisition(raw=2))
    with pytest.raises(ValidationError):
        ledger.consume(
            Quantity(9, 0), lambda lots, needed: [(lots[0], Quantity(5, 0))]
        )


def test_consume_rejects_a_plan_totalling_more_than_needed():
    ledger = LotLedger(ASSET)
    ledger.open_lot(_acquisition(raw=2))
    ledger.open_lot(_acquisition(raw=2))
    with pytest.raises(ValidationError):
        ledger.consume(
            Quantity(1, 0),
            lambda lots, needed: [
                (lots[0], Quantity(1, 0)),
                (lots[1], Quantity(1, 0)),
            ],
        )


@pytest.mark.parametrize("take_raw", [0, -1])
def test_consume_rejects_a_non_positive_take(take_raw):
    ledger = LotLedger(ASSET)
    ledger.open_lot(_acquisition(raw=2))
    with pytest.raises(ValidationError):
        ledger.consume(
            Quantity(1, 0), lambda lots, needed: [(lots[0], Quantity(take_raw, 0))]
        )


def test_consume_rejects_a_lot_this_ledger_does_not_hold():
    stranger = Lot(
        lot_id="lot_ffffffffffffffff",
        opened_at_ms=CONFIRMED_AT,
        asset_id=ASSET,
        quantity_original=Quantity(2, 0),
        quantity_remaining=Quantity(2, 0),
        cost_total=Money(USD_24, "USD"),
        cost_remaining=Fraction(24),
        source_tx_id="txn_zz99",
    )
    ledger = LotLedger(ASSET)
    ledger.open_lot(_acquisition(raw=2))
    with pytest.raises(ValidationError):
        ledger.consume(Quantity(1, 0), lambda lots, needed: [(stranger, Quantity(1, 0))])


def test_lot_is_deliberately_mutable_ledger_internal_state():
    lot = Lot(
        lot_id=LOT_ID_SEQ_0,
        opened_at_ms=CONFIRMED_AT,
        asset_id=ASSET,
        quantity_original=Quantity(2, 0),
        quantity_remaining=Quantity(2, 0),
        cost_total=Money(USD_24, "USD"),
        cost_remaining=Fraction(24),
        source_tx_id="txn_ab12",
    )
    lot.quantity_remaining = Quantity(1, 0)
    lot.cost_remaining = Fraction(12)
    assert lot.quantity_remaining == Quantity(1, 0)
    assert lot.cost_remaining == Fraction(12)


# --------------------------------------------------------------------------
# exact_mul: context-free, never rounded
# --------------------------------------------------------------------------


def test_exact_mul_golden_vectors():
    assert exact_mul(Decimal("1.5"), Decimal("2.5")) == Decimal("3.75")
    assert str(exact_mul(Decimal("1.5"), Decimal("2.5"))) == "3.75"
    # trailing zero preserved: the same vector positions/drill.py pins
    assert str(exact_mul(Decimal("10"), Decimal("3584.17"))) == "35841.70"
    assert str(exact_mul(Decimal("-1.5"), Decimal("2.5"))) == "-3.75"
    assert str(exact_mul(Decimal("-1.5"), Decimal("-2.5"))) == "3.75"
    assert str(exact_mul(Decimal("0"), Decimal("3584.17"))) == "0.00"


def test_exact_mul_survives_a_forty_digit_operand_without_context_rounding():
    wide = Decimal("1." + "2" * 39)
    product = exact_mul(wide, Decimal("3"))
    assert str(product) == "3.666666666666666666666666666666666666666"
    assert len(product.as_tuple().digits) == 40
    assert product != wide * Decimal("3")  # the context-bound product rounds


# --------------------------------------------------------------------------
# fraction_to_money: the ONE rounding boundary, always flagged
# --------------------------------------------------------------------------


def test_terminating_denominator_is_exact():
    assert fraction_to_money(Fraction(3, 4)) == (Money(Decimal("0.75"), "USD"), True)
    money, is_exact = fraction_to_money(Fraction(3, 4))
    assert str(money.amount) == "0.75"
    assert is_exact is True


def test_exactness_survives_beyond_the_context_precision():
    """1/2**50 needs 35 significant digits and is still EXACT: a naive
    Decimal division at 28 digits would silently round and mis-flag it."""
    money, is_exact = fraction_to_money(Fraction(1, 2**50))
    assert is_exact is True
    assert format(money.amount, "f") == (
        "0.00000000000000088817841970012523233890533447265625"
    )
    assert Fraction(money.amount) == Fraction(1, 2**50)
    assert len(money.amount.as_tuple().digits) == 35


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Fraction(24), "24"),
        (Fraction(0), "0"),
        (Fraction(-3, 4), "-0.75"),
        (Fraction(-1, 8), "-0.125"),
        (Fraction(7, 5**30), "0.000000000000000000007516192768"),
    ],
)
def test_terminating_values_round_trip_exactly(value, expected):
    money, is_exact = fraction_to_money(value)
    assert is_exact is True
    assert format(money.amount, "f") == expected
    assert Fraction(money.amount) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Fraction(10, 3), "3.333333333333333333333333333"),
        (Fraction(-10, 3), "-3.333333333333333333333333333"),
        (Fraction(2, 3), "0.6666666666666666666666666667"),
        (Fraction(1, 7), "0.1428571428571428571428571429"),
    ],
)
def test_repeating_values_round_half_even_at_28_significant_digits(value, expected):
    money, is_exact = fraction_to_money(value)
    assert is_exact is False
    assert str(money.amount) == expected
    assert len(money.amount.as_tuple().digits) == SIGNIFICANT_DIGITS == 28
    assert Fraction(money.amount) != value


def test_fraction_to_money_honours_the_currency():
    money, is_exact = fraction_to_money(Fraction(3, 4), "EUR")
    assert money == Money(Decimal("0.75"), "EUR")
    assert is_exact is True
    caip, _ = fraction_to_money(Fraction(1, 2), ETH)
    assert caip.currency == ETH


def test_fraction_to_money_rejects_a_malformed_currency():
    with pytest.raises(ValidationError):
        fraction_to_money(Fraction(3, 4), "usd")


# --------------------------------------------------------------------------
# purity. Accounting/ is pure over ledger reads (SPEC §3.3)
# --------------------------------------------------------------------------


def _imported_names() -> set[str]:
    import auradefi.accounting.lots as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize(
    "banned",
    ["httpx", "requests", "urllib3", "socket", "auradefi.clock", "auradefi.config"],
)
def test_the_module_imports_no_io_and_no_clock(banned):
    assert not any(
        name == banned or name.startswith(f"{banned}.") for name in _imported_names()
    )


def test_the_module_imports_only_money_ledger_and_foundation():
    internal = {
        name.split(".")[1]
        for name in _imported_names()
        if name.split(".")[0] == "auradefi" and len(name.split(".")) > 1
    }
    assert internal <= {"money", "ledger", "errors", "accounting"}
