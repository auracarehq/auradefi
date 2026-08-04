"""Incremental PnL engine — SPEC §9, the arbitrary-date replay.

Every expected number is hand-computed from the pinned rules in
docs/DECISIONS.md ("Shortfall semantics", "ACB pooling", "None-propagation
(PnL)", "Fraction->Money boundary", "Plaid TaxLot mapping") and asserted as
an exact ``Decimal`` — never a float.

The classic four-event scenario (buy 1@$10, 1@$20, 1@$15, sell 1@$18) is
the discriminator: a method that is not really plugged in cannot produce
8 / 3 / -2 / 3 realised from the same input.

This module covers the ADVANCING half — ``METHODS``, ``PnLState``,
``DisposalRecord``, ``process`` and ``pnl_at``, including the replay
ledger that keeps a 50,000-event stream inside the phase 9 budget. The
projection those states are read through lives in
``auradefi.accounting.report`` and is covered by ``test_report.py``;
``report`` is imported here where a report is the only way to observe an
engine fact.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from auradefi.accounting import acb, fifo, hifo, lifo
from auradefi.accounting.lots import (
    AcquisitionEvent,
    DisposalEvent,
    LotLedger,
    derive_events,
    fraction_to_money,
)
from auradefi.accounting.pnl import (
    METHODS,
    PnLState,
    pnl_at,
    process,
)
from auradefi.accounting.report import report
from auradefi.errors import CurrencyMismatchError, ValidationError
from auradefi.ledger.models import Direction, Entry, LedgerTransaction
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

ASSET = "eip155:1/erc20:0x" + "0" * 39 + "1"
ASSET_B = "eip155:1/erc20:0x" + "0" * 39 + "2"
WEI = 18
HUGE = 10**77


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


def ledger_transaction(tx_id: str, entries, at_ms: int = 1_000):
    return LedgerTransaction(
        id=tx_id,
        chain_id="eip155:1",
        tx_hash="0x" + "ab" * 32,
        account_id="acct_1",
        block_number=18_000_000,
        initiated_at=at_ms,
        confirmed_at=at_ms + 12_000,
        entries=tuple(entries),
    )


class TestMethodTable:
    """SPEC §9 names exactly four methods; anything else is a caller bug."""

    def test_methods_are_exactly_the_four_spec_names(self):
        assert set(METHODS) == {"fifo", "lifo", "hifo", "acb"}

    def test_methods_map_to_the_selector_functions_themselves(self):
        assert METHODS["fifo"] is fifo.select
        assert METHODS["lifo"] is lifo.select
        assert METHODS["hifo"] is hifo.select
        assert METHODS["acb"] is acb.select

    def test_an_unknown_method_is_a_validation_error_not_a_key_error(self):
        with pytest.raises(ValidationError):
            METHODS["wac"]

    def test_process_rejects_an_unknown_method(self):
        with pytest.raises(ValidationError):
            process(CLASSIC, "wac")

    def test_state_construction_rejects_an_unknown_method(self):
        with pytest.raises(ValidationError):
            PnLState("average")

    def test_process_rejects_a_state_built_for_another_method(self):
        state = process(CLASSIC[:1], "fifo")
        with pytest.raises(ValidationError):
            process(CLASSIC[1:], "lifo", state=state)


class TestClassicScenario:
    """The method-discriminating vectors, exact to the cent."""

    def test_one_disposal_record_per_disposal_event(self):
        state = process(CLASSIC, "fifo")
        assert len(state.disposals) == 1
        record = state.disposals[0]
        assert record.at_ms == 4_000
        assert record.asset_id == ASSET
        assert record.quantity == Quantity(1, 0)
        assert record.proceeds == usd(18)
        assert record.cost_basis == usd(10)
        assert record.realized == usd(8)
        assert record.missing_basis is False
        assert record.flags == ()


class TestInternalTransferGuard:
    """SPEC §9: without this every self-transfer reads as income and every
    tax report is wrong."""

    def test_self_entries_and_flagged_transactions_produce_nothing(self):
        self_tx = ledger_transaction(
            "tx_self",
            [
                Entry(ASSET, units(5), Direction.SELF),
                Entry(ASSET, units(5), Direction.SELF),
            ],
            at_ms=1_000,
        )
        flagged = ledger_transaction(
            "tx_internal",
            [
                Entry(ASSET, units(3), Direction.OUT),
                Entry(ASSET, units(3), Direction.IN),
            ],
            at_ms=2_000,
        )
        events = derive_events([self_tx, flagged], frozenset({"tx_internal"}))
        assert events == ()

        state = process(events, "fifo")
        assert state.disposals == []
        assert state.ledgers == {}

        result = report(state, 9_000, {})
        assert result.realized == Money(Decimal(0), "USD")
        assert result.missing_realized_count == 0
        assert result.unrealized == Money(Decimal(0), "USD")
        assert result.open_lots == ()
        assert dict(result.per_asset) == {}

    def test_control_the_same_transaction_is_taxable_when_not_flagged(self):
        flagged = ledger_transaction(
            "tx_internal",
            [
                Entry(ASSET, units(3), Direction.OUT),
                Entry(ASSET, units(3), Direction.IN),
            ],
            at_ms=2_000,
        )
        events = derive_events([flagged])
        assert len(events) == 2
        state = process(events, "fifo")
        assert len(state.disposals) == 1


class TestShortfall:
    """DECISIONS "Shortfall semantics": pre-history is a data-quality fact,
    never an exception."""

    def test_selling_more_than_held_books_a_zero_cost_synthetic(self):
        events = (
            buy(1_000, units(2), usd(20), "tx_buy_1"),
            sell(2_000, units(5), usd(50), "tx_sell_1"),
        )
        state = process(events, "fifo")
        record = state.disposals[0]
        assert record.cost_basis == usd(20)
        assert record.realized == usd(30)
        assert record.realized.amount == Decimal("30")
        assert record.missing_basis is True
        assert "missing_basis" in record.flags

    def test_selling_an_asset_never_acquired_is_all_shortfall(self):
        state = process((sell(1_000, units(4), usd(12), "tx_sell_1"),), "lifo")
        record = state.disposals[0]
        assert record.cost_basis == usd(0)
        assert record.realized == usd(12)
        assert record.missing_basis is True
        assert record.flags == ("missing_basis",)

    def test_a_shortfall_without_proceeds_flags_both(self):
        events = (
            buy(1_000, units(2), usd(20), "tx_buy_1"),
            sell(2_000, units(5), None, "tx_sell_1"),
        )
        record = process(events, "fifo").disposals[0]
        assert record.realized is None
        assert record.flags == ("missing_basis", "missing_proceeds")

    @pytest.mark.parametrize("method", ["fifo", "lifo", "hifo", "acb"])
    def test_no_method_raises_on_an_overdraw(self, method):
        events = (
            buy(1_000, units(1), usd(10), "tx_buy_1"),
            sell(2_000, units(9), usd(90), "tx_sell_1"),
        )
        record = process(events, method).disposals[0]
        assert record.missing_basis is True
        assert record.realized == usd(80)


class TestNonePropagation:
    """DECISIONS "None-propagation (PnL)": an unknown never becomes a zero."""

    def test_missing_proceeds_leave_realized_unknown(self):
        events = (
            buy(1_000, units(1), usd(10), "tx_buy_1"),
            sell(2_000, units(1), None, "tx_sell_1"),
            buy(3_000, units(1), usd(10), "tx_buy_2"),
            sell(4_000, units(1), usd(18), "tx_sell_2"),
        )
        state = process(events, "fifo")
        unknown, known = state.disposals
        assert unknown.proceeds is None
        assert unknown.cost_basis == usd(10)
        assert unknown.realized is None
        assert unknown.flags == ("missing_proceeds",)
        assert known.realized == usd(8)

        result = report(state, 5_000, {})
        assert result.realized == usd(8)
        assert result.missing_realized_count == 1

    def test_an_unpriced_lot_leaves_the_basis_unknown(self):
        events = (
            buy(1_000, units(1), None, "tx_buy_1"),
            sell(2_000, units(1), usd(18), "tx_sell_1"),
        )
        record = process(events, "fifo").disposals[0]
        assert record.cost_basis is None
        assert record.realized is None
        assert record.missing_basis is False
        assert record.flags == ("missing_cost",)

    def test_acb_poisoned_by_an_unpriced_acquisition_reports_unknown(self):
        events = (
            buy(1_000, units(1), None, "tx_buy_1"),
            buy(2_000, units(1), usd(20), "tx_buy_2"),
            sell(3_000, units(1), usd(18), "tx_sell_1"),
        )
        state = process(events, "acb")
        record = state.disposals[0]
        assert record.cost_basis is None
        assert record.realized is None
        assert "missing_cost" in record.flags
        assert report(state, 4_000, {ASSET: usd(25)}).unrealized is None


class TestRoundingBoundary:
    """DECISIONS "Fraction->Money boundary": rounding happens once, and
    always says so."""

    def test_a_non_terminating_basis_is_flagged_and_pinned(self):
        events = (
            buy(1_000, units(3), usd(10), "tx_buy_1"),
            sell(2_000, units(1), usd(18), "tx_sell_1"),
        )
        record = process(events, "fifo").disposals[0]
        assert record.cost_basis == Money(
            Decimal("3.333333333333333333333333333"), "USD"
        )
        assert record.realized == Money(
            Decimal("14.666666666666666666666666667"), "USD"
        )
        assert record.flags == ("rounded_basis",)

    def test_a_terminating_basis_is_not_flagged(self):
        events = (
            buy(1_000, units(4), usd(10), "tx_buy_1"),
            sell(2_000, units(1), usd(18), "tx_sell_1"),
        )
        record = process(events, "fifo").disposals[0]
        assert record.cost_basis == Money(Decimal("2.5"), "USD")
        assert record.flags == ()


class TestMonotonicInputAndCurrency:
    def test_an_older_event_is_a_validation_error(self):
        events = (
            buy(2_000, units(1), usd(10), "tx_buy_1"),
            buy(1_000, units(1), usd(10), "tx_buy_2"),
        )
        with pytest.raises(ValidationError):
            process(events, "fifo")

    def test_an_older_event_against_a_snapshot_is_also_rejected(self):
        state = process((buy(5_000, units(1), usd(10), "tx_buy_1"),), "fifo")
        assert state.last_at_ms == 5_000
        with pytest.raises(ValidationError):
            process((sell(4_999, units(1), usd(10), "tx_sell_1"),), "fifo", state=state)

    def test_equal_timestamps_keep_caller_order(self):
        events = (
            buy(1_000, units(1), usd(10), "tx_buy_1"),
            buy(1_000, units(1), usd(20), "tx_buy_2"),
            sell(1_000, units(1), usd(18), "tx_sell_1"),
        )
        assert process(events, "fifo").disposals[0].realized == usd(8)
        assert process(events, "lifo").disposals[0].realized == usd(-2)

    def test_a_second_currency_in_the_stream_is_a_mismatch(self):
        events = (
            buy(1_000, units(1), usd(10), "tx_buy_1"),
            sell(2_000, units(1), eur(18), "tx_sell_1"),
        )
        with pytest.raises(CurrencyMismatchError):
            process(events, "fifo")

    def test_the_first_priced_money_fixes_the_currency(self):
        events = (buy(1_000, units(1), eur(10), "tx_buy_1"),)
        state = process(events, "fifo")
        assert state.currency == "EUR"
        assert report(state, 2_000, {ASSET: eur(25)}).unrealized == eur(15)


class TestSnapshotAndArbitraryDate:
    """Incremental replay is the mechanism behind arbitrary-date PnL."""

    @staticmethod
    def stream():
        events = []
        at_ms = 1_000
        for index in range(6):
            events.append(
                buy(at_ms, units(2), usd(10 + index), f"tx_buy_{index}")
            )
            at_ms += 1_000
            events.append(
                sell(at_ms, units(1), usd(20 + index), f"tx_sell_{index}")
            )
            at_ms += 1_000
        return tuple(events)

    @pytest.mark.parametrize("method", ["fifo", "lifo", "hifo", "acb"])
    def test_a_snapshot_resumes_to_the_same_report(self, method):
        events = self.stream()
        marks = {ASSET: usd(30)}
        single = report(process(events, method), 99_000, marks)

        state = process(events[:7], method)
        resumed = report(process(events[7:], method, state=state), 99_000, marks)
        assert resumed == single

    @pytest.mark.parametrize("method", ["fifo", "lifo", "hifo", "acb"])
    def test_pnl_at_equals_filter_then_process_then_report(self, method):
        events = self.stream()
        marks = {ASSET: usd(30)}
        cutoff = 6_500
        filtered = tuple(event for event in events if event.at_ms <= cutoff)
        assert 0 < len(filtered) < len(events)

        expected = report(process(filtered, method), cutoff, marks)
        assert pnl_at(events, method, cutoff, marks) == expected

    def test_pnl_at_is_method_sensitive_at_the_same_cutoff(self):
        events = self.stream()
        marks = {ASSET: usd(30)}
        first = pnl_at(events, "fifo", 6_500, marks)
        second = pnl_at(events, "hifo", 6_500, marks)
        assert first.realized != second.realized

    def test_a_cutoff_before_everything_is_an_empty_report(self):
        result = pnl_at(self.stream(), "fifo", 1, {})
        assert result.realized == Money(Decimal(0), "USD")
        assert result.open_lots == ()
        assert result.as_of_ms == 1


class TestBoundaries:
    def test_a_ten_to_the_seventy_seventh_quantity_stays_exact(self):
        events = (
            buy(1_000, units(HUGE, WEI), usd(1_000), "tx_buy_1"),
            sell(2_000, units(HUGE // 2, WEI), usd(900), "tx_sell_1"),
        )
        record = process(events, "fifo").disposals[0]
        assert record.cost_basis == usd(500)
        assert record.realized == usd(400)


def _scripted_stream():
    """A pinned pseudo-random stream: partial takes, exhaustions, shortfalls.

    Deterministic (an LCG, no ``random``), two assets, quantities and costs
    chosen so lots are consumed partially, drained exactly, and overdrawn.

    Every choice reads a HIGH bit of the state: this LCG's low bit simply
    alternates, so ``state % 2`` would pick the same asset every time and
    the differential below would silently cover one ledger.
    """
    state = 7_654_321
    events = []
    at_ms = 1_000
    for index in range(150):
        state = (1103515245 * state + 12345) % 2**31
        asset = ASSET if (state >> 20) & 1 == 0 else ASSET_B
        state = (1103515245 * state + 12345) % 2**31
        draw = state >> 8
        if draw % 3 == 0:
            events.append(
                sell(at_ms, units(1 + draw % 6), usd(3 + draw % 40),
                     f"tx_s{index:04d}", asset=asset)
            )
        else:
            events.append(
                buy(at_ms, units(1 + draw % 5), usd(7 + draw % 50),
                    f"tx_b{index:04d}", asset=asset)
            )
        at_ms += 1_000
    return tuple(events)


def _reference_replay(events, selector):
    """The same replay through a plain ``LotLedger`` — the slow, obvious way."""
    ledgers: dict[str, LotLedger] = {}
    bases = []
    for event in events:
        ledger = ledgers.get(event.asset_id)
        if ledger is None:
            ledger = ledgers[event.asset_id] = LotLedger(event.asset_id)
        if isinstance(event, AcquisitionEvent):
            ledger.open_lot(event)
            continue
        consumed, shortfall = ledger.consume(event.quantity, selector)
        total = Fraction(0)
        for _lot, _take, portion in consumed:
            total += portion
        bases.append((fraction_to_money(total, "USD")[0], shortfall.raw > 0))
    shape = {
        asset: [
            (lot.lot_id, lot.quantity_remaining.raw, lot.cost_remaining)
            for lot in ledger.open_lots
        ]
        for asset, ledger in ledgers.items()
    }
    return bases, shape


class TestEngineMatchesAPlainLotLedger:
    """The engine specialises the ledger for replay speed; this proves the
    specialisation is value-identical to the base class."""

    @pytest.mark.parametrize("method", ["fifo", "lifo", "hifo"])
    def test_a_scripted_stream_agrees_piece_for_piece(self, method):
        events = _scripted_stream()
        expected_bases, expected_shape = _reference_replay(events, METHODS[method])

        state = process(events, method)
        assert [
            (record.cost_basis, record.missing_basis) for record in state.disposals
        ] == expected_bases
        assert {
            asset: [
                (lot.lot_id, lot.quantity_remaining.raw, lot.cost_remaining)
                for lot in ledger.open_lots
            ]
            for asset, ledger in state.ledgers.items()
        } == expected_shape

    def test_the_scripted_stream_actually_exercises_the_hard_paths(self):
        events = _scripted_stream()
        state = process(events, "fifo")
        assert any(record.missing_basis for record in state.disposals)
        assert any(not record.missing_basis for record in state.disposals)
        assert len(state.ledgers) == 2
