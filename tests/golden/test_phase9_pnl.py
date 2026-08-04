"""Phase 9 gate — arbitrary-date PnL on a 50,000-event wallet (SPEC §11).

The thing Zerion cannot do: *"PnL is pre-computed at standard marks ...
other values are supported only if fewer than 3,000 transactions sit
between your timestamp and the nearest mark — otherwise the request errors
out"* (SPEC §9). This gate asks for PnL at three arbitrary instants inside
a 50,000-event stream and pins the answers.

The stream is fully generated — no wall clock, no ``random``, one LCG — so
the totals below are closed-form facts, not recordings. Two of the three
cutoffs are chosen where FIFO and LIFO DISAGREE, which is what proves the
method is really plugged in at an arbitrary date rather than being one
hard-coded algorithm; the third is a point of symmetry where they must
agree exactly.

Each asset receives, in order, pairs of (buy 2 units, sell 1 unit) whose
unit cost alternates $10, $12, $10, ... against a flat $15 sale, so FIFO
realises 5, 5, 3, 3, ... and LIFO 5, 3, 5, 3, .... Both average $4 a pair;
their running totals differ by exactly $2 an asset whenever a whole number
of pairs ≡ 2 (mod 4) has completed, and agree on multiples of 4.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

import pytest

from auradefi.accounting.lots import AcquisitionEvent, DisposalEvent
from auradefi.accounting.pnl import pnl_at, process, report
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

T0 = 1_600_000_000_000
PAIRS = 25_000
ASSETS = tuple(f"eip155:1/erc20:0x{a:040x}" for a in range(5))

BUY_QUANTITY = Quantity(2, 0)
SELL_QUANTITY = Quantity(1, 0)
CHEAP_COST = Money(Decimal(20), "USD")
DEAR_COST = Money(Decimal(24), "USD")
SALE_PROCEEDS = Money(Decimal(15), "USD")
MARK = Money(Decimal("20"), "USD")
MARKS = {asset: MARK for asset in ASSETS}

#: Cutoffs. MID and LATE land after a whole pair whose count per asset is
#: 2 (mod 4), where FIFO and LIFO differ by $2 an asset; END is past
#: everything, where 5,000 pairs an asset (0 mod 4) makes them equal.
MID = 1_600_750_599_999
LATE = 1_601_200_599_999
END = 1_601_500_000_000
CUTOFFS = (MID, LATE, END)

#: Events at or before each cutoff — MID and LATE both land 59,999 ms into
#: a 60,000 ms slot, past that pair's sell window, so the split never
#: depends on the draw.
EVENTS_AT_CUTOFF = {MID: 25_020, LATE: 40_020, END: 50_000}

REALIZED = {
    ("fifo", MID): Decimal("50050"),
    ("lifo", MID): Decimal("50040"),
    ("fifo", LATE): Decimal("80050"),
    ("lifo", LATE): Decimal("80040"),
    ("fifo", END): Decimal("100000"),
    ("lifo", END): Decimal("100000"),
}

#: FIFO drains its oldest lot every second pair, so half its lots close;
#: LIFO always sells the unit it just bought, so nothing ever closes.
OPEN_LOTS = {
    ("fifo", MID): 6_255,
    ("lifo", MID): 12_510,
    ("fifo", LATE): 10_005,
    ("lifo", LATE): 20_010,
    ("fifo", END): 12_500,
    ("lifo", END): 25_000,
}

#: 5,000 units held an asset marked at $20 = $100,000, less $55,000 of
#: surviving basis (110,000 bought, 55,000 consumed): $45,000 an asset.
UNREALIZED_AT_END = Decimal("225000")


@lru_cache(maxsize=1)
def _events() -> tuple[AcquisitionEvent | DisposalEvent, ...]:
    """The pinned 50,000-event stream."""
    draw = 20_260_802
    events: list[AcquisitionEvent | DisposalEvent] = []
    for index in range(PAIRS):
        asset = ASSETS[index % 5]
        slot = T0 + index * 60_000
        cost = CHEAP_COST if (index // 5) % 2 == 0 else DEAR_COST
        draw = (1103515245 * draw + 12345) % 2**31
        events.append(
            AcquisitionEvent(
                slot + draw % 20_000,
                asset,
                BUY_QUANTITY,
                cost,
                f"txn_b{index:07d}",
            )
        )
        draw = (1103515245 * draw + 12345) % 2**31
        events.append(
            DisposalEvent(
                slot + 30_000 + draw % 20_000,
                asset,
                SELL_QUANTITY,
                SALE_PROCEEDS,
                f"txn_s{index:07d}",
            )
        )
    return tuple(events)


@lru_cache(maxsize=2)
def _reports(method: str):
    """One incremental pass, reported at each cutoff — the snapshot path."""
    events = _events()
    state = None
    reports = {}
    start = 0
    for cutoff in CUTOFFS:
        stop = start
        while stop < len(events) and events[stop].at_ms <= cutoff:
            stop += 1
        state = process(events[start:stop], method, state=state)
        reports[cutoff] = report(state, cutoff, MARKS)
        start = stop
    return reports


class TestTheGenerator:
    """The stream itself is a golden vector; drift here invalidates the rest."""

    def test_fifty_thousand_events_in_strictly_increasing_time(self):
        events = _events()
        assert len(events) == 50_000
        stamps = [event.at_ms for event in events]
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == 50_000

    def test_the_first_and_last_pairs_are_pinned(self):
        events = _events()
        assert [event.at_ms for event in events[:4]] == [
            1_600_000_005_491,
            1_600_000_046_720,
            1_600_000_061_481,
            1_600_000_090_622,
        ]
        assert [event.at_ms for event in events[-2:]] == [
            1_601_499_948_157,
            1_601_499_985_730,
        ]
        assert events[0].source_tx_id == "txn_b0000000"
        assert events[-1].source_tx_id == "txn_s0024999"

    def test_costs_alternate_per_asset_and_assets_cycle(self):
        events = _events()
        assert events[0].asset_id == ASSETS[0]
        assert events[2].asset_id == ASSETS[1]
        assert events[0].cost == CHEAP_COST
        assert events[10].cost == DEAR_COST  # pair 5 -> k == 1
        assert events[20].cost == CHEAP_COST  # pair 10 -> k == 2

    @pytest.mark.parametrize("cutoff", CUTOFFS)
    def test_each_cutoff_admits_the_pinned_number_of_events(self, cutoff):
        events = _events()
        admitted = sum(1 for event in events if event.at_ms <= cutoff)
        assert admitted == EVENTS_AT_CUTOFF[cutoff]


class TestArbitraryDateRealized:
    """Three arbitrary instants, two methods, six pinned totals."""

    @pytest.mark.parametrize("method", ["fifo", "lifo"])
    @pytest.mark.parametrize("cutoff", CUTOFFS)
    def test_realized_is_the_closed_form_total(self, method, cutoff):
        result = _reports(method)[cutoff]
        assert result.realized == Money(REALIZED[method, cutoff], "USD")
        assert result.realized.amount == REALIZED[method, cutoff]
        assert result.realized.currency == "USD"
        assert result.as_of_ms == cutoff
        assert result.method == method

    @pytest.mark.parametrize("cutoff", [MID, LATE])
    def test_the_methods_disagree_at_an_arbitrary_date(self, cutoff):
        fifo_total = _reports("fifo")[cutoff].realized.amount
        lifo_total = _reports("lifo")[cutoff].realized.amount
        assert fifo_total - lifo_total == Decimal("10")

    def test_the_methods_agree_at_the_symmetric_end(self):
        assert _reports("fifo")[END].realized == _reports("lifo")[END].realized

    @pytest.mark.parametrize("method", ["fifo", "lifo"])
    def test_no_disposal_is_missing_basis_or_proceeds(self, method):
        result = _reports(method)[END]
        assert result.missing_realized_count == 0

    @pytest.mark.parametrize("method", ["fifo", "lifo"])
    def test_realized_splits_evenly_across_the_five_assets(self, method):
        result = _reports(method)[END]
        assert sorted(result.per_asset) == sorted(ASSETS)
        for asset in ASSETS:
            assert result.per_asset[asset].realized == Money(Decimal("20000"), "USD")
            assert result.per_asset[asset].quantity_held == Quantity(5_000, 0)


class TestUnrealizedAndOpenLots:
    @pytest.mark.parametrize("method", ["fifo", "lifo"])
    def test_unrealized_at_the_end_is_the_closed_form_total(self, method):
        result = _reports(method)[END]
        assert result.unrealized == Money(UNREALIZED_AT_END, "USD")
        for asset in ASSETS:
            assert result.per_asset[asset].unrealized == Money(
                Decimal("45000"), "USD"
            )

    @pytest.mark.parametrize("method", ["fifo", "lifo"])
    @pytest.mark.parametrize("cutoff", CUTOFFS)
    def test_open_lot_counts_discriminate_the_methods(self, method, cutoff):
        result = _reports(method)[cutoff]
        assert len(result.open_lots) == OPEN_LOTS[method, cutoff]

    @pytest.mark.parametrize("method", ["fifo", "lifo"])
    def test_every_tax_lot_carries_the_plaid_shape(self, method):
        lots = _reports(method)[END].open_lots
        for lot in lots:
            assert lot.position_type == "LONG"
            assert lot.institution_lot_id.startswith("lot_")
            assert len(lot.institution_lot_id) == 20
            assert isinstance(lot.original_purchase_datetime, int)
            assert isinstance(lot.quantity, Decimal)
            assert lot.quantity > 0
        assert len({lot.institution_lot_id for lot in lots}) == len(lots)
        # Sorted by (asset_id, opened_at_ms, lot_id): time ascends inside an
        # asset and restarts at each of the four asset boundaries.
        stamps = [lot.original_purchase_datetime for lot in lots]
        descents = sum(
            1 for before, after in zip(stamps, stamps[1:]) if after < before
        )
        assert descents == 4

    def test_lifo_lots_are_all_half_consumed_and_fifo_lots_are_whole(self):
        lifo_lots = _reports("lifo")[END].open_lots
        assert {lot.quantity for lot in lifo_lots} == {Decimal("1")}
        fifo_lots = _reports("fifo")[END].open_lots
        assert {lot.quantity for lot in fifo_lots} == {Decimal("2")}


class TestPnlAtIsTheSameAnswer:
    """``pnl_at`` filters and replays; the incremental path snapshots. The
    two must not be able to disagree."""

    @pytest.mark.parametrize("method", ["fifo", "lifo"])
    def test_pnl_at_the_mid_cutoff_matches_the_snapshot_replay(self, method):
        assert pnl_at(_events(), method, MID, MARKS) == _reports(method)[MID]
