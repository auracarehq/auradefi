"""LIFO consumption-order selector — SPEC §9 pluggable methods.

Same plan contract as FIFO (no mutation, no cost math, no exception for
shortage) with the order inverted: newest first, ties broken by the LATER
input position — the exact reverse of FIFO, which the last test pins
directly against ``auradefi.accounting.fifo``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from auradefi.accounting import fifo, lifo
from auradefi.errors import DecimalsMismatchError
from auradefi.money.quantity import Quantity

WEI = 18
ETH_1_50 = 1_500_000_000_000_000_000
ETH_2_25 = 2_250_000_000_000_000_000
ETH_0_75 = 750_000_000_000_000_000
ETH_3_00 = 3_000_000_000_000_000_000
HUGE = 10**77


@dataclass
class StubLot:
    """A lot-shaped stub carrying the pinned field names, deliberately
    MUTABLE so "the selector mutates nothing" is a real assertion."""

    lot_id: str
    opened_at_ms: int
    quantity_original: Quantity
    quantity_remaining: Quantity
    cost_total: object | None = None
    source_tx_id: str = "tx_stub"


class NoTouchLot:
    """Reading anything on this lot is a test failure."""

    @property
    def opened_at_ms(self) -> int:
        raise AssertionError("selector inspected a lot for a non-positive need")

    @property
    def quantity_remaining(self) -> Quantity:
        raise AssertionError("selector inspected a lot for a non-positive need")


class TripwireLot:
    """Only ``opened_at_ms`` and ``quantity_remaining`` exist; touching any
    other pinned field raises, which is how wave independence is proven."""

    def __init__(self, opened_at_ms: int, quantity_remaining: Quantity) -> None:
        self.opened_at_ms = opened_at_ms
        self.quantity_remaining = quantity_remaining

    def __getattr__(self, name: str) -> object:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"selector read forbidden lot field {name!r}")


def lot(lot_id: str, opened_at_ms: int, raw: int, decimals: int = 0) -> StubLot:
    quantity = Quantity(raw, decimals)
    return StubLot(
        lot_id=lot_id,
        opened_at_ms=opened_at_ms,
        quantity_original=quantity,
        quantity_remaining=quantity,
        cost_total=None,
        source_tx_id=f"tx_{lot_id}",
    )


def readable(plan: list[tuple[StubLot, Quantity]]) -> list[tuple[str, int]]:
    return [(taken_lot.lot_id, take.raw) for taken_lot, take in plan]


def abc_lots() -> tuple[StubLot, StubLot, StubLot]:
    return lot("A", 1000, 2), lot("B", 2000, 3), lot("C", 3000, 5)


def test_acceptance_vector_walks_newest_lots_first():
    a, b, c = abc_lots()

    plan = lifo.select([a, b, c], Quantity(4, 0))

    assert readable(plan) == [("C", 4)]
    assert plan == [(c, Quantity(4, 0))]
    assert plan[0][0] is c


def test_shortage_plans_the_total_held_and_never_raises():
    a, b, c = abc_lots()

    plan = lifo.select([a, b, c], Quantity(20, 0))

    assert readable(plan) == [("C", 5), ("B", 3), ("A", 2)]
    assert sum(take.raw for _, take in plan) == 10


def test_drained_lots_are_skipped():
    a, b, c = abc_lots()
    c.quantity_remaining = Quantity(0, 0)

    plan = lifo.select([a, b, c], Quantity(1, 0))

    assert readable(plan) == [("B", 1)]


def test_negative_remaining_lots_are_skipped():
    a, b, c = abc_lots()
    c.quantity_remaining = Quantity(-5, 0)

    plan = lifo.select([a, b, c], Quantity(4, 0))

    assert readable(plan) == [("B", 3), ("A", 1)]


def test_every_lot_drained_yields_an_empty_plan():
    a, b, c = abc_lots()
    for drained in (a, b, c):
        drained.quantity_remaining = Quantity(0, 0)

    assert lifo.select([a, b, c], Quantity(7, 0)) == []


def test_empty_lot_sequence_yields_an_empty_plan():
    assert lifo.select([], Quantity(9, 0)) == []


def test_opened_at_ms_tie_breaks_on_the_later_input_position():
    first, second, third = (lot(name, 5000, 1) for name in ("X", "Y", "Z"))

    plan = lifo.select([first, second, third], Quantity(2, 0))

    assert readable(plan) == [("Z", 1), ("Y", 1)]


def test_input_order_never_overrides_time_order():
    later = lot("B", 2000, 4)
    older = lot("A", 1000, 1)
    older_tied = lot("A2", 1000, 1)

    plan = lifo.select([later, older, older_tied], Quantity(6, 0))

    assert readable(plan) == [("B", 4), ("A2", 1), ("A", 1)]


def test_zero_need_returns_an_empty_plan_without_inspecting_any_lot():
    assert lifo.select([NoTouchLot(), NoTouchLot()], Quantity(0, 0)) == []


def test_negative_need_returns_an_empty_plan_without_inspecting_any_lot():
    assert lifo.select([NoTouchLot()], Quantity(-7, 0)) == []


def test_selector_mutates_no_lot_and_does_not_reorder_the_input():
    a, b, c = abc_lots()
    lots = [c, a, b]
    identities = [id(item) for item in lots]

    lifo.select(lots, Quantity(6, 0))

    assert [id(item) for item in lots] == identities
    assert a.quantity_remaining == Quantity(2, 0)
    assert b.quantity_remaining == Quantity(3, 0)
    assert c.quantity_remaining == Quantity(5, 0)
    assert (b.quantity_original, c.quantity_original) == (
        Quantity(3, 0),
        Quantity(5, 0),
    )


def test_exact_fit_consumes_every_lot_with_no_zero_takes():
    a, b, c = abc_lots()

    plan = lifo.select([a, b, c], Quantity(10, 0))

    assert readable(plan) == [("C", 5), ("B", 3), ("A", 2)]
    assert all(take.raw > 0 for _, take in plan)


def test_partial_take_stops_the_walk_at_the_first_satisfying_lot():
    a, b, c = abc_lots()

    plan = lifo.select([a, b, c], Quantity(5, 0))

    assert readable(plan) == [("C", 5)]


def test_eighteen_decimal_golden_vector():
    first = lot("L1", 1_700_000_000_000, ETH_1_50, WEI)
    second = lot("L2", 1_705_000_000_000, ETH_2_25, WEI)
    third = lot("L3", 1_710_000_000_000, ETH_0_75, WEI)

    plan = lifo.select([first, second, third], Quantity(ETH_3_00, WEI))

    assert plan == [
        (third, Quantity(750_000_000_000_000_000, WEI)),
        (second, Quantity(2_250_000_000_000_000_000, WEI)),
    ]
    assert [str(take) for _, take in plan] == ["0.75", "2.25"]


def test_huge_ten_to_the_seventy_seven_scale_quantities():
    first = lot("H1", 1, HUGE)
    second = lot("H2", 2, HUGE)

    plan = lifo.select([first, second], Quantity(HUGE + 5, 0))

    assert plan == [(second, Quantity(HUGE, 0)), (first, Quantity(5, 0))]
    assert sum(take.raw for _, take in plan) == HUGE + 5


def test_huge_shortage_plans_exactly_the_total_held():
    first = lot("H1", 1, HUGE)
    second = lot("H2", 2, HUGE)

    plan = lifo.select([first, second], Quantity(3 * HUGE, 0))

    assert sum(take.raw for _, take in plan) == 2 * HUGE


def test_decimals_mismatch_on_a_live_lot_propagates():
    mismatched = lot("A", 1000, 2, 0)

    with pytest.raises(DecimalsMismatchError):
        lifo.select([mismatched], Quantity(ETH_1_50, WEI))


def test_drained_lot_of_a_foreign_scale_is_skipped_before_any_arithmetic():
    foreign = lot("C", 3000, 0, 0)
    live = lot("B", 2000, ETH_2_25, WEI)

    plan = lifo.select([foreign, live], Quantity(ETH_1_50, WEI))

    assert plan == [(live, Quantity(ETH_1_50, WEI))]


def test_any_sequence_is_accepted_not_just_a_list():
    a, b, c = abc_lots()

    plan = lifo.select((a, b, c), Quantity(4, 0))

    assert readable(plan) == [("C", 4)]


def test_selector_reads_only_opened_at_ms_and_quantity_remaining():
    lots = [
        TripwireLot(3000, Quantity(5, 0)),
        TripwireLot(1000, Quantity(2, 0)),
        TripwireLot(2000, Quantity(3, 0)),
    ]

    plan = lifo.select(lots, Quantity(6, 0))

    assert [take.raw for _, take in plan] == [5, 1]
    assert [taken.opened_at_ms for taken, _ in plan] == [3000, 2000]


def test_full_drain_is_the_exact_reverse_of_the_fifo_plan():
    lots = [
        lot("m", 2000, 4),
        lot("k", 1000, 1),
        lot("n", 1000, 2),
        lot("q", 3000, 3),
        lot("r", 2000, 5),
        lot("z", 2500, 0),
    ]
    needed = Quantity(15, 0)

    fifo_plan = fifo.select(lots, needed)
    lifo_plan = lifo.select(lots, needed)

    assert readable(fifo_plan) == [("k", 1), ("n", 2), ("m", 4), ("r", 5), ("q", 3)]
    assert readable(lifo_plan) == [("q", 3), ("r", 5), ("m", 4), ("n", 2), ("k", 1)]
    assert lifo_plan == list(reversed(fifo_plan))


# --- structural pins: one public entry point, no runtime cross-module import


MODULE_PATH = Path(lifo.__file__)


def _is_type_checking(node: ast.If) -> bool:
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def split_imports(source: str) -> tuple[set[str], set[str]]:
    """(runtime dotted names, TYPE_CHECKING-gated dotted names)."""
    tree = ast.parse(source)
    gated_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking(node):
            for child in node.body:
                for inner in ast.walk(child):
                    gated_nodes.add(id(inner))
    runtime: set[str] = set()
    gated: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names = {base} | {f"{base}.{alias.name}" for alias in node.names if base}
        else:
            continue
        (gated if id(node) in gated_nodes else runtime).update(names)
    return runtime, gated


def test_module_exposes_exactly_one_public_definition():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    public = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    ]

    assert public == ["select"]


def test_lot_is_imported_only_under_type_checking_and_the_module_stays_pure():
    runtime, gated = split_imports(MODULE_PATH.read_text(encoding="utf-8"))

    assert "__future__.annotations" in runtime
    assert "auradefi.accounting.lots.Lot" in gated
    assert not {name for name in runtime if name.startswith("auradefi.accounting")}
    assert not {
        name
        for name in runtime
        if name.startswith("auradefi.")
        and not name.startswith(("auradefi.money", "auradefi.errors"))
    }
