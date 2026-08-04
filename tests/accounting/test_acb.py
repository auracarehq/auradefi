"""ACB: Canadian pooled average cost — SPEC §9 pluggable methods.

Two halves, both pinned in docs/DECISIONS.md ("ACB pooling"):

  * :class:`AcbPool`, the costing overlay — exact rational pool, pro-rata
    disposal, permanent poisoning on an unpriced acquisition;
  * :func:`acb.select`, FIFO's oldest-first walk restated for QUANTITY
    bookkeeping only, on the plan terms ``fifo``/``lifo`` state.

Golden values were derived by hand and hardcoded as exact ``Fraction``
literals: 45/3 = 15, 10/3 stays 10/3, and three disposals out of a 10-USD
three-unit pool sum back to exactly 10 with zero drift. A ``Decimal`` pool
cannot do that, which is the whole reason the pool is rational.

Lots are local stubs: ``select`` is structurally typed and the waiver
forbids importing ``accounting.lots``/``fifo``/``lifo`` at runtime. One
test imports the real ``Lot`` locally, because a restated walk reading the
wrong field names is exactly what the waiver risks.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from auradefi.accounting import acb
from auradefi.errors import (
    CurrencyMismatchError,
    DecimalsMismatchError,
    ValidationError,
)
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

T1 = 1_700_000_000_000
T2 = 1_700_000_001_000
T3 = 1_700_000_002_000

WEI = 18
HUGE = 10**77


def usd(amount: str) -> Money:
    return Money(Decimal(amount), "USD")


def units(raw: int, decimals: int = 0) -> Quantity:
    return Quantity(raw, decimals)


@dataclass
class StubLot:
    """A lot-shaped stub carrying the pinned field names, deliberately
    MUTABLE so "the selector mutates nothing" is a real assertion.

    No ``cost_total``: ACB never reads a per-lot basis, and a stub that
    cannot offer one proves it.
    """

    lot_id: str
    opened_at_ms: int
    quantity_remaining: Quantity
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
    other pinned field raises, which is how wave independence — and ACB's
    indifference to per-lot cost — is proven."""

    def __init__(self, opened_at_ms: int, quantity_remaining: Quantity) -> None:
        self.opened_at_ms = opened_at_ms
        self.quantity_remaining = quantity_remaining

    def __getattr__(self, name: str) -> object:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"selector read forbidden lot field {name!r}")


def lot(lot_id: str, opened_at_ms: int, raw: int, decimals: int = 0) -> StubLot:
    return StubLot(
        lot_id=lot_id,
        opened_at_ms=opened_at_ms,
        quantity_remaining=Quantity(raw, decimals),
    )


# ------------------------------------------------------------- pool identity


def test_a_fresh_pool_holds_zero_cost_zero_units_and_an_unknown_scale():
    pool = acb.AcbPool("USD")

    assert pool.currency == "USD"
    assert pool.cost == Fraction(0)
    assert pool.quantity_raw == 0
    assert pool.decimals is None
    assert pool.poisoned is False


def test_a_fresh_pool_cost_is_a_fraction_never_a_float():
    assert type(acb.AcbPool("USD").cost) is Fraction


@pytest.mark.parametrize("currency", [None, 840, b"USD"])
def test_a_non_string_currency_raises_validation_error(currency):
    with pytest.raises(ValidationError):
        acb.AcbPool(currency)


def test_a_negative_seed_quantity_raises_validation_error():
    with pytest.raises(ValidationError):
        acb.AcbPool("USD", Fraction(10), -1)


def test_a_bool_seed_quantity_raises_validation_error():
    # bool is an int subclass, and is never an amount.
    with pytest.raises(ValidationError):
        acb.AcbPool("USD", Fraction(10), True)


def test_a_negative_seed_scale_raises_validation_error():
    with pytest.raises(ValidationError):
        acb.AcbPool("USD", Fraction(0), 0, -1)


# ------------------------------------------------------------------ acquire


def test_acquire_adds_raw_units_and_pools_the_cost():
    pool = acb.AcbPool("USD")

    pool.acquire(units(1), usd("10"))
    pool.acquire(units(1), usd("20"))

    assert pool.cost == Fraction(30)
    assert pool.quantity_raw == 2


def test_the_first_acquisition_teaches_the_pool_its_scale():
    pool = acb.AcbPool("USD")

    pool.acquire(units(5, WEI), usd("10"))

    assert pool.decimals == WEI


def test_acquiring_at_another_scale_raises_decimals_mismatch():
    pool = acb.AcbPool("USD")
    pool.acquire(units(1, WEI), usd("10"))

    with pytest.raises(DecimalsMismatchError):
        pool.acquire(units(1, 6), usd("10"))


def test_acquiring_in_another_currency_raises_currency_mismatch():
    pool = acb.AcbPool("USD")

    with pytest.raises(CurrencyMismatchError):
        pool.acquire(units(1), Money(Decimal("10"), "EUR"))


def test_acquiring_a_negative_quantity_raises_validation_error():
    pool = acb.AcbPool("USD")

    with pytest.raises(ValidationError):
        pool.acquire(units(-1), usd("10"))


def test_acquiring_zero_units_at_zero_cost_changes_nothing():
    pool = acb.AcbPool("USD")

    pool.acquire(units(0), usd("0"))

    assert (pool.cost, pool.quantity_raw) == (Fraction(0), 0)


def test_pooled_cost_stays_exact_across_binary_unfriendly_decimals():
    pool = acb.AcbPool("USD")

    pool.acquire(units(3), usd("0.1"))
    pool.acquire(units(3), usd("0.2"))

    # 1/10 + 2/10 = 3/10 exactly. The float 0.30000000000000004 never
    # exists anywhere in this pool.
    assert pool.cost == Fraction(3, 10)


# ------------------------------------------------------------------ dispose


def test_the_pooled_average_disposal_vector():
    # 10 + 20 + 15 = 45 over 3 units, so one unit costs exactly 45/3 = 15
    # and 30 over 2 units is left behind.
    pool = acb.AcbPool("USD")
    pool.acquire(units(1), usd("10"))
    pool.acquire(units(1), usd("20"))
    pool.acquire(units(1), usd("15"))

    consumed = pool.dispose(units(1))

    assert consumed == Fraction(15)
    assert pool.cost == Fraction(30)
    assert pool.quantity_raw == 2


def test_a_non_terminating_share_stays_an_exact_rational():
    # 10 USD over 3 units: one unit costs 10/3, which no Decimal holds.
    pool = acb.AcbPool("USD")
    pool.acquire(units(3), usd("10"))

    assert pool.dispose(units(1)) == Fraction(10, 3)
    assert pool.cost == Fraction(20, 3)
    assert pool.quantity_raw == 2


def test_successive_disposals_sum_back_to_the_pool_with_zero_drift():
    pool = acb.AcbPool("USD")
    pool.acquire(units(3), usd("10"))

    consumed = [pool.dispose(units(1)) for _ in range(3)]

    assert consumed == [Fraction(10, 3), Fraction(10, 3), Fraction(10, 3)]
    assert sum(consumed) == Fraction(10)
    assert pool.cost == Fraction(0)
    assert pool.quantity_raw == 0


def test_disposing_the_whole_pool_at_once_empties_it():
    pool = acb.AcbPool("USD")
    pool.acquire(units(4), usd("7"))

    assert pool.dispose(units(4)) == Fraction(7)
    assert pool.cost == Fraction(0)
    assert pool.quantity_raw == 0


def test_a_disposal_of_part_of_a_unit_prorates_exactly():
    # 7 USD over 10**18 wei; 3*10**17 wei costs 21/10 exactly.
    pool = acb.AcbPool("USD")
    pool.acquire(units(10**18, WEI), usd("7"))

    assert pool.dispose(units(3 * 10**17, WEI)) == Fraction(21, 10)
    assert pool.cost == Fraction(49, 10)
    assert pool.quantity_raw == 7 * 10**17


def test_disposing_more_than_the_pool_holds_raises_validation_error():
    pool = acb.AcbPool("USD")
    pool.acquire(units(2), usd("10"))

    with pytest.raises(ValidationError):
        pool.dispose(units(3))


def test_an_overdraw_leaves_the_pool_untouched():
    # The engine clamps to what the pool holds and books the shortfall
    # itself, so a pool asked to overdraw has already been handed a bug.
    pool = acb.AcbPool("USD")
    pool.acquire(units(2), usd("10"))

    with pytest.raises(ValidationError):
        pool.dispose(units(3))

    assert pool.cost == Fraction(10)
    assert pool.quantity_raw == 2


def test_disposing_from_an_empty_pool_raises_validation_error():
    with pytest.raises(ValidationError):
        acb.AcbPool("USD").dispose(units(1))


def test_disposing_zero_units_from_an_empty_pool_never_divides_by_zero():
    empty = acb.AcbPool("USD")

    assert empty.dispose(units(0)) == Fraction(0)
    assert (empty.cost, empty.quantity_raw) == (Fraction(0), 0)


def test_disposing_zero_units_from_a_stocked_pool_changes_nothing():
    pool = acb.AcbPool("USD")
    pool.acquire(units(2), usd("10"))

    assert pool.dispose(units(0)) == Fraction(0)
    assert (pool.cost, pool.quantity_raw) == (Fraction(10), 2)


def test_disposing_a_negative_quantity_raises_validation_error():
    pool = acb.AcbPool("USD")
    pool.acquire(units(5), usd("10"))

    with pytest.raises(ValidationError):
        pool.dispose(units(-1))


def test_disposing_at_another_scale_raises_decimals_mismatch():
    pool = acb.AcbPool("USD")
    pool.acquire(units(5, WEI), usd("10"))

    with pytest.raises(DecimalsMismatchError):
        pool.dispose(units(1, 8))


def test_the_pool_survives_10_pow_77_base_units_exactly():
    pool = acb.AcbPool("USD")
    pool.acquire(units(HUGE, WEI), usd("1E+30"))

    # 10**30 * 3*10**76 / 10**77 = 3*10**29, exactly.
    assert pool.dispose(units(3 * 10**76, WEI)) == Fraction(3 * 10**29)
    assert pool.cost == Fraction(7 * 10**29)
    assert pool.quantity_raw == 7 * 10**76


# ---------------------------------------------------------------- poisoning


def test_an_unpriced_acquisition_poisons_the_pool_cost():
    pool = acb.AcbPool("USD")
    pool.acquire(units(1), usd("10"))

    pool.acquire(units(1), None)

    assert pool.cost is None
    assert pool.poisoned is True
    assert pool.quantity_raw == 2  # units are still tracked exactly


def test_a_poisoned_pool_disposes_to_none():
    pool = acb.AcbPool("USD")
    pool.acquire(units(1), None)

    assert pool.dispose(units(1)) is None


def test_a_later_priced_acquisition_never_un_poisons_the_pool():
    # An honest unknown is not repairable by averaging known numbers into
    # it. This is the pinned decision, and it is permanent.
    pool = acb.AcbPool("USD")
    pool.acquire(units(1), None)

    pool.acquire(units(1), usd("100"))
    pool.acquire(units(1), usd("250"))

    assert pool.cost is None
    assert pool.dispose(units(1)) is None
    assert pool.dispose(units(1)) is None
    assert pool.quantity_raw == 1


def test_a_poisoned_pool_still_tracks_quantity_through_disposals():
    pool = acb.AcbPool("USD")
    pool.acquire(units(10), usd("10"))
    pool.acquire(units(5), None)

    assert pool.dispose(units(6)) is None
    assert pool.quantity_raw == 9
    assert pool.cost is None


def test_a_poisoned_pool_disposing_zero_returns_none_not_zero():
    # None is "unknown", and an unknown share of nothing is still unknown.
    pool = acb.AcbPool("USD")
    pool.acquire(units(1), None)

    assert pool.dispose(units(0)) is None


def test_a_poisoned_pool_still_rejects_an_overdraw():
    pool = acb.AcbPool("USD")
    pool.acquire(units(1), None)

    with pytest.raises(ValidationError):
        pool.dispose(units(2))


def test_emptying_a_poisoned_pool_leaves_zero_quantity_and_none_cost():
    pool = acb.AcbPool("USD")
    pool.acquire(units(3), None)

    assert pool.dispose(units(3)) is None
    assert pool.quantity_raw == 0
    assert pool.cost is None


def test_an_unpriced_acquisition_on_a_fresh_pool_poisons_it_immediately():
    pool = acb.AcbPool("USD")

    pool.acquire(units(2), None)

    assert pool.poisoned is True
    assert pool.cost is None
    assert pool.quantity_raw == 2


# ------------------------------------------------------------------- select


def test_select_walks_lots_oldest_first():
    newest = lot("lot_c", T3, 1)
    oldest = lot("lot_a", T1, 1)
    middle = lot("lot_b", T2, 1)

    plan = acb.select([newest, oldest, middle], units(3))

    assert plan == [(oldest, units(1)), (middle, units(1)), (newest, units(1))]


def test_select_takes_only_what_is_needed_from_the_oldest_lot():
    oldest = lot("lot_a", T1, 10)
    newer = lot("lot_b", T2, 10)

    assert acb.select([oldest, newer], units(4)) == [(oldest, units(4))]


def test_select_spills_into_the_next_lot_when_the_oldest_runs_out():
    oldest = lot("lot_a", T1, 3)
    newer = lot("lot_b", T2, 9)

    assert acb.select([oldest, newer], units(5)) == [
        (oldest, units(3)),
        (newer, units(2)),
    ]


def test_select_breaks_equal_timestamps_by_earlier_input_position():
    first = lot("lot_first", T1, 1)
    second = lot("lot_second", T1, 1)

    plan = acb.select([first, second], units(2))

    assert plan == [(first, units(1)), (second, units(1))]


def test_select_reads_only_timestamp_and_remaining_never_a_lot_cost():
    # Under ACB the pool is the costing overlay; lots carry quantity truth
    # only. Reading any other field on this lot is an immediate failure.
    tripwire = TripwireLot(T1, Quantity(4, 0))

    assert acb.select([tripwire], units(3)) == [(tripwire, units(3))]


def test_select_skips_drained_lots():
    drained = lot("lot_drained", T1, 0)
    available = lot("lot_live", T2, 4)

    assert acb.select([drained, available], units(4)) == [(available, units(4))]


def test_select_skips_negative_remaining_lots():
    corrupt = StubLot("lot_bad", T1, Quantity(-3, 0))
    available = lot("lot_live", T2, 2)

    assert acb.select([corrupt, available], units(2)) == [(available, units(2))]


def test_select_caps_a_take_at_the_lot_remaining():
    only = lot("lot_a", T1, 2)

    assert acb.select([only], units(9)) == [(only, units(2))]


def test_select_plans_a_shortage_rather_than_raising():
    older = lot("lot_a", T1, 2)
    newer = lot("lot_b", T2, 1)

    plan = acb.select([older, newer], units(10))

    assert plan == [(older, units(2)), (newer, units(1))]
    assert sum(take.raw for _, take in plan) == 3


def test_select_plans_nothing_for_a_zero_need_and_inspects_no_lot():
    assert acb.select([NoTouchLot()], units(0)) == []


def test_select_plans_nothing_for_a_negative_need_and_inspects_no_lot():
    assert acb.select([NoTouchLot()], units(-5)) == []


def test_select_plans_nothing_for_no_lots():
    assert acb.select([], units(5)) == []


def test_select_plans_nothing_when_every_lot_is_drained():
    assert acb.select([lot("lot_drained", T1, 0)], units(5)) == []


def test_select_raises_decimals_mismatch_for_a_live_lot_at_another_scale():
    wrong_scale = lot("lot_usdc", T1, 5, 6)

    with pytest.raises(DecimalsMismatchError):
        acb.select([wrong_scale], units(1, WEI))


def test_select_takes_are_positive_quantities_at_the_needed_scale():
    plan = acb.select([lot("lot_a", T1, 3, WEI), lot("lot_b", T2, 3, WEI)], units(5, WEI))

    for _, take in plan:
        assert isinstance(take, Quantity)
        assert take.decimals == WEI
        assert take.raw > 0


def test_select_handles_10_pow_77_base_units():
    older = lot("lot_a", T1, HUGE, WEI)
    newer = lot("lot_b", T2, HUGE, WEI)

    assert acb.select([older, newer], units(HUGE + 1, WEI)) == [
        (older, units(HUGE, WEI)),
        (newer, units(1, WEI)),
    ]


def test_select_mutates_neither_the_lots_nor_the_sequence():
    older = lot("lot_a", T1, 3)
    newer = lot("lot_b", T2, 3)
    lots = [older, newer]

    acb.select(lots, units(4))

    assert lots == [older, newer]
    assert older.quantity_remaining == Quantity(3, 0)
    assert newer.quantity_remaining == Quantity(3, 0)


def test_select_accepts_any_sequence_not_just_a_list():
    older = lot("lot_a", T1, 1)
    newer = lot("lot_b", T2, 1)

    assert acb.select((newer, older), units(1)) == [(older, units(1))]


def test_the_planned_lots_are_the_very_objects_passed_in():
    older = lot("lot_a", T1, 1)

    assert acb.select([older], units(1))[0][0] is older


def test_select_reads_the_real_lot_field_names():
    # Imported locally so a lots.py breakage cannot take this module down.
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

    assert acb.select([real], units(2, WEI)) == [(real, units(2, WEI))]


# ------------------------------------------------- the duplication waiver


SELECTOR = (
    Path(__file__).resolve().parents[2] / "src" / "auradefi" / "accounting" / "acb.py"
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
    # DECISIONS "Duplication waiver extension": acb.select restates FIFO's
    # oldest-first walk on purpose. This test is what keeps that waiver
    # from quietly rotting into a real dependency.
    assert not (_runtime_import_components(SELECTOR) & FORBIDDEN_SIBLINGS)
