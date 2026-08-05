"""HIFO consumption-order selector — SPEC §9 pluggable methods.

The plan is the whole contract, on the terms ``fifo``/``lifo`` state:
ordered ``(lot, take)`` pairs, no mutation, no cost math, no exception for
shortage. What HIFO adds is the ORDER, and the order is a pinned
stability guarantee — every expected plan below is a hardcoded literal
derived by hand from ``Fraction(cost_total.amount) /
Fraction(quantity_original.raw)``, descending, unpriced last, ties
oldest-first then earlier input position.

Lots are local stubs: ``select`` is structurally typed and
docs/internal/DECISIONS.md ("Duplication waiver extension") forbids the runtime
import of ``accounting.lots``/``fifo``/``lifo``. One test does import the
real ``Lot`` — locally — because a restated walk that reads the wrong
field names is exactly the failure the waiver risks.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from auradefi.accounting import hifo
from auradefi.errors import DecimalsMismatchError, ValidationError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

T1 = 1_700_000_000_000
T2 = 1_700_000_001_000
T3 = 1_700_000_002_000

WEI = 18
HUGE = 10**77


@dataclass
class StubLot:
    """A lot-shaped stub carrying the pinned field names, deliberately
    MUTABLE so "the selector mutates nothing" is a real assertion."""

    lot_id: str
    opened_at_ms: int
    quantity_original: Quantity
    quantity_remaining: Quantity
    cost_total: Money | None = None
    source_tx_id: str = "tx_stub"


class NoTouchLot:
    """Reading anything on this lot is a test failure."""

    @property
    def opened_at_ms(self) -> int:
        raise AssertionError("selector inspected a lot for a non-positive need")

    @property
    def quantity_remaining(self) -> Quantity:
        raise AssertionError("selector inspected a lot for a non-positive need")

    @property
    def cost_total(self) -> Money | None:
        raise AssertionError("selector inspected a lot for a non-positive need")


class TripwireLot:
    """Only the four fields HIFO may read exist; touching any other pinned
    field raises, which is how wave independence is proven."""

    def __init__(
        self,
        opened_at_ms: int,
        quantity_original: Quantity,
        quantity_remaining: Quantity,
        cost_total: Money | None,
    ) -> None:
        self.opened_at_ms = opened_at_ms
        self.quantity_original = quantity_original
        self.quantity_remaining = quantity_remaining
        self.cost_total = cost_total

    def __getattr__(self, name: str) -> object:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"selector read forbidden lot field {name!r}")


def lot(
    lot_id: str,
    opened_at_ms: int,
    raw: int,
    cost: str | None,
    *,
    remaining: int | None = None,
    decimals: int = 0,
) -> StubLot:
    """A lot of ``raw`` base units whose WHOLE basis is ``cost`` USD."""
    return StubLot(
        lot_id=lot_id,
        opened_at_ms=opened_at_ms,
        quantity_original=Quantity(raw, decimals),
        quantity_remaining=Quantity(raw if remaining is None else remaining, decimals),
        cost_total=None if cost is None else Money(Decimal(cost), "USD"),
    )


def units(raw: int, decimals: int = 0) -> Quantity:
    return Quantity(raw, decimals)


# ---------------------------------------------------------------- unit cost


def test_unit_cost_is_cost_total_over_original_quantity_exactly():
    # 10 USD spread over 3 base units is 10/3 — a rational no Decimal holds.
    assert hifo.unit_cost(lot("lot_a", T1, 3, "10")) == Fraction(10, 3)


def test_unit_cost_divides_by_original_not_remaining():
    # 100 USD bought 10 units and 9 are already gone; the basis per unit is
    # still 10. Unit cost is a fact of the acquisition, not of the balance,
    # and it is the same ratio LotLedger.consume prorates with.
    assert hifo.unit_cost(lot("lot_a", T1, 10, "100", remaining=1)) == Fraction(10)


def test_unit_cost_of_an_unpriced_lot_is_none():
    assert hifo.unit_cost(lot("lot_a", T1, 5, None)) is None


def test_unit_cost_survives_10_pow_77_base_units():
    # 10**30 USD over 10**77 base units -> 1/10**47, exactly.
    whale = lot("lot_whale", T1, HUGE, "1E+30", decimals=WEI)
    assert hifo.unit_cost(whale) == Fraction(1, 10**47)


def test_unit_cost_of_a_priced_zero_quantity_lot_raises_validation_error():
    # Basis per unit over zero units is undefined; a bare ZeroDivisionError
    # would escape the auradefi.errors taxonomy.
    with pytest.raises(ValidationError):
        hifo.unit_cost(lot("lot_void", T1, 0, "10"))


def test_unit_cost_of_an_unpriced_zero_quantity_lot_is_none():
    assert hifo.unit_cost(lot("lot_void", T1, 0, None)) is None


def test_unit_cost_reads_the_real_lot_field_names():
    # The waiver's real risk is a restated walk reading fields that do not
    # exist on the ledger's Lot. Imported locally so a lots.py breakage
    # cannot take this whole module down with it.
    from auradefi.accounting.lots import Lot

    real = Lot(
        lot_id="lot_real",
        opened_at_ms=T1,
        asset_id="ast_0000000000000000",
        quantity_original=Quantity(4, WEI),
        quantity_remaining=Quantity(3, WEI),
        cost_total=Money(Decimal("10"), "USD"),
        cost_remaining=Fraction(15, 2),
        source_tx_id="txn_0000000000000000",
    )

    assert hifo.unit_cost(real) == Fraction(10, 4)
    assert hifo.select([real], units(2, WEI)) == [(real, units(2, WEI))]


# --------------------------------------------------------------- the ordering


def test_hifo_takes_the_dearest_lot_not_the_oldest_or_the_newest():
    # THE divergence vector: FIFO would take the 10, LIFO the 15.
    cheapest = lot("lot_10", T1, 1, "10")
    dearest = lot("lot_20", T2, 1, "20")
    middle = lot("lot_15", T3, 1, "15")

    plan = hifo.select([cheapest, dearest, middle], units(1))

    assert plan == [(dearest, units(1))]
    assert plan[0][0] is dearest


def test_a_full_drain_runs_in_descending_unit_cost():
    cheapest = lot("lot_10", T1, 1, "10")
    dearest = lot("lot_20", T2, 1, "20")
    middle = lot("lot_15", T3, 1, "15")

    plan = hifo.select([cheapest, dearest, middle], units(3))

    assert plan == [
        (dearest, units(1)),
        (middle, units(1)),
        (cheapest, units(1)),
    ]


def test_unit_cost_ranks_the_lots_never_the_total_cost():
    # 100 USD over 100 units is 1/unit and must LOSE to 3 USD over 1 unit.
    big_total_cheap_unit = lot("lot_big", T1, 100, "100")
    small_total_dear_unit = lot("lot_small", T2, 1, "3")

    plan = hifo.select([big_total_cheap_unit, small_total_dear_unit], units(1))

    assert plan == [(small_total_dear_unit, units(1))]


def test_ten_over_three_ranks_below_three_point_three_four():
    # 10/3 = 3.333... < 3.34 = 167/50, so the 3.34 lot is the dearer one.
    three_for_ten = lot("lot_10_3", T1, 3, "10")
    one_for_334 = lot("lot_334", T2, 1, "3.34")
    assert hifo.unit_cost(three_for_ten) == Fraction(10, 3)
    assert hifo.unit_cost(one_for_334) == Fraction(167, 50)
    assert Fraction(10, 3) < Fraction(167, 50)

    plan = hifo.select([three_for_ten, one_for_334], units(1))

    assert plan == [(one_for_334, units(1))]


def test_the_key_is_exact_where_float_and_context_division_both_tie():
    # Derived by hand:
    #   exact  10/3              = 3.333... repeating
    #   exact  Decimal 28 threes = 33333333333333333333333333333 / 10**28
    # 10/3 is strictly GREATER, so the 10/3 lot goes first. But
    # float(10)/3 == float('3.33...') == 3.3333333333333335, and
    # Decimal('10')/Decimal(3) at the default 28-digit context IS the
    # other value. Both lossy keys tie, then hand the win to the older
    # lot — which is the wrong lot, silently, forever.
    exact_ten_thirds = lot("lot_exact", T2, 3, "10")
    twenty_eight_threes = lot("lot_lossy", T1, 1, "3.3333333333333333333333333333")

    assert hifo.unit_cost(exact_ten_thirds) == Fraction(10, 3)
    assert hifo.unit_cost(twenty_eight_threes) == Fraction(
        33333333333333333333333333333, 10**28
    )
    assert Fraction(10, 3) > Fraction(33333333333333333333333333333, 10**28)
    lossy_literal = Decimal("3.3333333333333333333333333333")
    assert float(Decimal("10")) / 3 == float(lossy_literal)  # floats tie
    assert Decimal("10") / Decimal(3) == lossy_literal / Decimal(1)  # so does ctx

    plan = hifo.select([exact_ten_thirds, twenty_eight_threes], units(4))

    assert plan[0][0] is exact_ten_thirds
    assert plan == [(exact_ten_thirds, units(3)), (twenty_eight_threes, units(1))]


def test_10_pow_77_lots_order_by_exact_unit_cost():
    # 10**30/10**77 vs (10**30 + 1)/10**77 — one cent-of-a-cent apart, 77
    # digits down. Exact rationals still separate them.
    plain = lot("lot_plain", T1, HUGE, "1E+30", decimals=WEI)
    dearer = lot("lot_dear", T2, HUGE, "1000000000000000000000000000001", decimals=WEI)

    plan = hifo.select([plain, dearer], units(HUGE, WEI))

    assert plan == [(dearer, units(HUGE, WEI))]


# -------------------------------------------------------------- unpriced lots


def test_unpriced_lots_sort_after_every_priced_lot():
    unpriced = lot("lot_none", T1, 1, None)
    priced = lot("lot_10", T2, 1, "10")

    plan = hifo.select([unpriced, priced], units(2))

    assert plan == [(priced, units(1)), (unpriced, units(1))]


def test_a_partial_need_never_touches_an_unpriced_lot():
    unpriced = lot("lot_none", T1, 1, None)
    priced = lot("lot_10", T2, 1, "10")

    assert hifo.select([unpriced, priced], units(1)) == [(priced, units(1))]


def test_an_unpriced_lot_loses_even_to_a_zero_cost_priced_lot():
    # A known basis of zero is still knowledge; None is not.
    unpriced = lot("lot_none", T1, 1, None)
    free_but_known = lot("lot_free", T2, 1, "0")

    assert hifo.select([unpriced, free_but_known], units(1)) == [
        (free_but_known, units(1))
    ]


def test_unpriced_lots_among_themselves_run_oldest_first():
    newer = lot("lot_new", T3, 1, None)
    older = lot("lot_old", T1, 1, None)

    plan = hifo.select([newer, older], units(2))

    assert plan == [(older, units(1)), (newer, units(1))]


# ----------------------------------------------------------------- tie-breaks


def test_equal_unit_cost_breaks_oldest_first():
    newer = lot("lot_new", T3, 2, "20")  # 10 per unit
    older = lot("lot_old", T1, 1, "10")  # 10 per unit

    plan = hifo.select([newer, older], units(3))

    assert plan == [(older, units(1)), (newer, units(2))]


def test_equal_unit_cost_and_equal_timestamp_breaks_by_earlier_input_position():
    first = lot("lot_first", T1, 1, "10")
    second = lot("lot_second", T1, 1, "10")

    plan = hifo.select([first, second], units(2))

    assert plan == [(first, units(1)), (second, units(1))]


# --------------------------------------------------------------- the walk


def test_a_need_smaller_than_a_lot_takes_a_partial_slice():
    big = lot("lot_big", T1, 10, "100")

    assert hifo.select([big], units(3)) == [(big, units(3))]


def test_the_walk_stops_the_moment_the_need_is_met():
    dearest = lot("lot_dear", T1, 5, "500")
    cheap = lot("lot_cheap", T2, 5, "5")

    assert hifo.select([dearest, cheap], units(5)) == [(dearest, units(5))]


def test_a_take_is_capped_at_remaining_not_at_original():
    mostly_spent = lot("lot_spent", T1, 10, "100", remaining=2)

    assert hifo.select([mostly_spent], units(10)) == [(mostly_spent, units(2))]


def test_drained_lots_never_appear_in_a_plan():
    drained = lot("lot_drained", T1, 5, "500", remaining=0)  # dearest, but empty
    available = lot("lot_live", T2, 5, "5")

    assert hifo.select([drained, available], units(5)) == [(available, units(5))]


def test_negative_remaining_lots_are_filtered_not_planned():
    corrupt = lot("lot_bad", T1, 5, "500", remaining=-3)
    available = lot("lot_live", T2, 2, "2")

    assert hifo.select([corrupt, available], units(2)) == [(available, units(2))]


def test_a_shortage_is_planned_not_raised():
    # DECISIONS "Shortfall semantics": pre-history is a data-quality fact.
    # The plan covers the 3 that exist; the caller books the missing 7.
    dear = lot("lot_dear", T1, 2, "20")
    cheap = lot("lot_cheap", T2, 1, "1")

    plan = hifo.select([dear, cheap], units(10))

    assert plan == [(dear, units(2)), (cheap, units(1))]
    assert sum(take.raw for _, take in plan) == 3


def test_no_lot_appears_twice_in_a_plan():
    lots = [
        lot("lot_a", T1, 4, "40"),
        lot("lot_b", T2, 4, "80"),
        lot("lot_c", T3, 4, "20"),
    ]

    plan = hifo.select(lots, units(12))

    assert len({id(entry[0]) for entry in plan}) == 3


def test_every_take_is_a_positive_quantity_at_the_needed_scale():
    lots = [lot("lot_a", T1, 3, "30", decimals=WEI), lot("lot_b", T2, 3, "60", decimals=WEI)]

    plan = hifo.select(lots, units(5, WEI))

    for _, take in plan:
        assert isinstance(take, Quantity)
        assert take.decimals == WEI
        assert take.raw > 0


def test_the_planned_lots_are_the_very_objects_passed_in():
    dear = lot("lot_dear", T1, 1, "10")

    plan = hifo.select([dear], units(1))

    assert plan[0][0] is dear


def test_hifo_reads_only_the_four_fields_it_is_allowed_to_read():
    tripwire = TripwireLot(T1, Quantity(4, 0), Quantity(4, 0), Money(Decimal("8"), "USD"))

    assert hifo.select([tripwire], units(3)) == [(tripwire, units(3))]


# ---------------------------------------------------------------- boundaries


def test_a_zero_need_plans_nothing_and_inspects_no_lot():
    assert hifo.select([NoTouchLot()], units(0)) == []


def test_a_negative_need_plans_nothing_and_inspects_no_lot():
    assert hifo.select([NoTouchLot()], units(-5)) == []


def test_no_lots_plan_nothing():
    assert hifo.select([], units(7)) == []


def test_only_drained_lots_plan_nothing():
    assert hifo.select([lot("lot_drained", T1, 5, "50", remaining=0)], units(5)) == []


def test_a_live_lot_at_another_scale_raises_decimals_mismatch():
    wrong_scale = lot("lot_usdc", T1, 5, "50", decimals=6)

    with pytest.raises(DecimalsMismatchError):
        hifo.select([wrong_scale], units(1, WEI))


def test_a_huge_need_spanning_huge_lots_is_exact():
    dearer = lot("lot_dear", T1, HUGE, "2E+30", decimals=WEI)
    cheaper = lot("lot_cheap", T2, HUGE, "1E+30", decimals=WEI)

    plan = hifo.select([dearer, cheaper], units(HUGE + 1, WEI))

    assert plan == [(dearer, units(HUGE, WEI)), (cheaper, units(1, WEI))]


# ------------------------------------------------------------------- purity


def test_select_mutates_neither_the_lots_nor_the_sequence():
    a = lot("lot_a", T1, 3, "30")
    b = lot("lot_b", T2, 3, "90")
    lots = [a, b]

    hifo.select(lots, units(4))

    assert lots == [a, b]
    assert a.quantity_remaining == Quantity(3, 0)
    assert b.quantity_remaining == Quantity(3, 0)
    assert a.cost_total == Money(Decimal("30"), "USD")


def test_select_accepts_any_sequence_not_just_a_list():
    a = lot("lot_a", T1, 1, "10")
    b = lot("lot_b", T2, 1, "20")

    assert hifo.select((a, b), units(1)) == [(b, units(1))]


def test_identical_input_yields_an_identical_plan():
    lots = [
        lot("lot_a", T1, 2, "4"),
        lot("lot_b", T1, 2, "4"),
        lot("lot_c", T2, 2, "10"),
    ]

    assert hifo.select(lots, units(5)) == hifo.select(lots, units(5))


# ------------------------------------------------- the duplication waiver


SELECTOR = (
    Path(__file__).resolve().parents[2] / "src" / "auradefi" / "accounting" / "hifo.py"
)
FORBIDDEN_SIBLINGS = {"fifo", "lifo", "lots"}


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _runtime_import_components(path: Path) -> set[str]:
    """Dotted-name components of every import NOT under ``TYPE_CHECKING``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    exempt = {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and _is_type_checking(node.test)
        for inner in ast.walk(node)
        if isinstance(inner, (ast.Import, ast.ImportFrom))
    }
    names: list[str] = []
    for node in ast.walk(tree):
        if id(node) in exempt:
            continue
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names)
    return {part for name in names for part in name.split(".") if part}


def test_the_module_imports_no_sibling_selector_at_runtime():
    # DECISIONS "Duplication waiver extension": the greedy walk is restated
    # here deliberately, because same-wave disjoint ownership forbids the
    # import. This test is what keeps the waiver from quietly rotting into
    # a real dependency.
    assert not (_runtime_import_components(SELECTOR) & FORBIDDEN_SIBLINGS)
