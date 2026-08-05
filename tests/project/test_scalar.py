"""Scalar projection goldens (SPEC §6, §8): 26 pinned metrics, always.

Every expected value below was derived independently from the pinned
integer hour formula ``(initiated_at // 3_600_000) % 24`` and from
``float()`` semantics via python3, never by calling the code under test:

* ``1_700_000_000_000 // 3_600_000 % 24 == 22``
* ``(1_700_000_000 + 3600*k)*1000`` for k=0..6 -> hours 22,23,0,1,2,3,4
* ``float(Decimal('123456789012345678.123456789'))
  == 1.2345678901234568e+17`` (int back: 123456789012345680: lossy)

The float assertions here are NOT money-equality defects: the scalar
projection's wire format IS float, documented LOSSY and display-only in
the module contract. Exact Decimals stay on ``HoldingsReport``.
"""

from __future__ import annotations

import ast
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from auradefi.ledger.models import LedgerTransaction
from auradefi.money.fiat import Money
from auradefi.portfolio.models import HoldingsReport
from auradefi.project.scalar import Metric, scalar_metrics

AS_OF_MS = 1_754_000_000_000  # ms epoch (SPEC §4.4), matches the frozen clock
ADDRESS = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
CHAIN = "eip155:1"

# Pinned order: a hardcoded stability contract, not a comprehension.
PINNED_NAMES = (
    "portfolio_value_usd",
    "transaction_count",
    "tx_count_hour_00",
    "tx_count_hour_01",
    "tx_count_hour_02",
    "tx_count_hour_03",
    "tx_count_hour_04",
    "tx_count_hour_05",
    "tx_count_hour_06",
    "tx_count_hour_07",
    "tx_count_hour_08",
    "tx_count_hour_09",
    "tx_count_hour_10",
    "tx_count_hour_11",
    "tx_count_hour_12",
    "tx_count_hour_13",
    "tx_count_hour_14",
    "tx_count_hour_15",
    "tx_count_hour_16",
    "tx_count_hour_17",
    "tx_count_hour_18",
    "tx_count_hour_19",
    "tx_count_hour_20",
    "tx_count_hour_21",
    "tx_count_hour_22",
    "tx_count_hour_23",
)


def _report(total: str = "0") -> HoldingsReport:
    return HoldingsReport(
        address=ADDRESS,
        chain_id=CHAIN,
        holdings=(),
        total_value=Money(Decimal(total), "USD"),
        unpriced=(),
        as_of_ms=AS_OF_MS,
    )


def _tx(initiated_at: int, *, removed: bool = False) -> LedgerTransaction:
    return LedgerTransaction(
        id=f"txn_{initiated_at:016x}",
        chain_id=CHAIN,
        tx_hash=f"0x{initiated_at:064x}",
        account_id=ADDRESS,
        block_number=1,
        initiated_at=initiated_at,
        confirmed_at=None,
        entries=(),
        removed=removed,
    )


def _by_name(metrics: tuple[Metric, ...]) -> dict[str, float]:
    return {metric.name: metric.value for metric in metrics}


# ------------------------------------------------------------ empty input


def test_empty_input_emits_exactly_26_metrics_in_pinned_order():
    metrics = scalar_metrics(_report(), ())
    assert len(metrics) == 26
    assert tuple(metric.name for metric in metrics) == PINNED_NAMES


def test_empty_input_every_value_zero_every_at_ms_is_as_of_ms():
    metrics = scalar_metrics(_report(), ())
    assert all(metric.value == 0.0 for metric in metrics)
    assert all(metric.at_ms == AS_OF_MS for metric in metrics)
    assert all(isinstance(metric, Metric) for metric in metrics)


# ------------------------------------------------------------ hour goldens


def test_hour_golden_1_700_000_000_000_lands_in_hour_22():
    metrics = scalar_metrics(_report(), (_tx(1_700_000_000_000),))
    values = _by_name(metrics)
    assert values["tx_count_hour_22"] == 1.0
    assert values["transaction_count"] == 1.0
    zero_hours = [name for name in PINNED_NAMES[2:] if name != "tx_count_hour_22"]
    assert all(values[name] == 0.0 for name in zero_hours)


def test_seven_transaction_golden_wraps_midnight():
    transactions = tuple(
        _tx((1_700_000_000 + 3600 * k) * 1000) for k in range(7)
    )
    metrics = scalar_metrics(_report(), transactions)
    values = _by_name(metrics)
    assert values["transaction_count"] == 7.0
    hit = (
        "tx_count_hour_22",
        "tx_count_hour_23",
        "tx_count_hour_00",
        "tx_count_hour_01",
        "tx_count_hour_02",
        "tx_count_hour_03",
        "tx_count_hour_04",
    )
    assert all(values[name] == 1.0 for name in hit)
    zero_hours = [name for name in PINNED_NAMES[2:] if name not in hit]
    assert len(zero_hours) == 17
    assert all(values[name] == 0.0 for name in zero_hours)
    assert all(metric.at_ms == AS_OF_MS for metric in metrics)


def test_hour_boundary_zero_timestamp_lands_in_hour_00():
    values = _by_name(scalar_metrics(_report(), (_tx(0),)))
    assert values["tx_count_hour_00"] == 1.0


def test_hour_boundary_huge_timestamp_pinned_by_integer_formula():
    # (10**18 // 3_600_000) % 24 == 1, derived independently.
    values = _by_name(scalar_metrics(_report(), (_tx(10**18),)))
    assert values["tx_count_hour_01"] == 1.0
    assert values["transaction_count"] == 1.0


def test_two_transactions_same_hour_accumulate():
    # Both inside hour 22. Base sits 800s into its bucket
    # (1_700_000_000_000 % 3_600_000 == 800_000), so +40min keeps the
    # second at offset 3200s < 3600s: same bucket, derived via python3:
    # ((1_700_000_000_000 + 40*60*1000) // 3_600_000) % 24 == 22.
    transactions = (_tx(1_700_000_000_000), _tx(1_700_000_000_000 + 40 * 60 * 1000))
    values = _by_name(scalar_metrics(_report(), transactions))
    assert values["tx_count_hour_22"] == 2.0
    assert values["transaction_count"] == 2.0


# ------------------------------------------------------------ value goldens


def test_portfolio_value_usd_projects_the_report_total():
    metrics = scalar_metrics(_report("5025"), ())
    assert metrics[0] == Metric("portfolio_value_usd", AS_OF_MS, 5025.0)
    assert metrics[1] == Metric("transaction_count", AS_OF_MS, 0.0)


def test_float_lossiness_is_pinned_and_documented():
    # float() is LOSSY, display-only, per the module contract. The exact
    # Decimal stays on the report; this golden pins the loss itself.
    metrics = scalar_metrics(
        _report("123456789012345678.123456789"), ()
    )
    assert metrics[0].value == 1.2345678901234568e+17
    assert int(metrics[0].value) == 123456789012345680  # not ...678: lossy


# ------------------------------------------------------------ Metric shape


def test_metric_is_a_namedtuple_that_unpacks_and_equals_a_plain_tuple():
    metric = Metric("portfolio_value_usd", AS_OF_MS, 5025.0)
    name, at_ms, value = metric
    assert (name, at_ms, value) == ("portfolio_value_usd", AS_OF_MS, 5025.0)
    assert metric == ("portfolio_value_usd", AS_OF_MS, 5025.0)
    assert isinstance(metric, tuple)
    assert metric._fields == ("name", "at_ms", "value")


# ------------------------------------------------------- declared wire type


def test_every_metric_is_the_declared_str_int_float_triple():
    # The headline contract is (metric, timestamp, FLOAT). Value equality
    # cannot pin it: 1 == 1.0, and a NamedTuple with an int member still
    # compares equal to ("transaction_count", AS_OF_MS, 0.0). So an int
    # count would slip past every == assertion in this file. Pin the
    # TYPES themselves, on the populated path where counts are non-zero.
    metrics = scalar_metrics(
        _report("5025.25"), (_tx(1_700_000_000_000), _tx(0))
    )
    assert type(metrics) is tuple  # not a list, not a generator
    assert len(metrics) == 26
    for metric in metrics:
        assert type(metric) is Metric
        assert type(metric.name) is str
        assert type(metric.at_ms) is int  # exact int: not bool, not float
        assert type(metric.value) is float  # exact float: not int/Decimal


def test_zero_counts_are_float_zero_not_int_zero():
    # The empty path emits 25 counts of zero; `0 == 0.0` hides an int
    # regression, so pin all 26 value types positionally.
    metrics = scalar_metrics(_report(), ())
    assert type(metrics) is tuple
    assert [type(metric.value) for metric in metrics] == [float] * 26
    assert [type(metric.at_ms) for metric in metrics] == [int] * 26


def test_a_list_input_still_returns_a_tuple_of_float_metrics():
    # Sequence in, tuple out. The return container is part of the wire
    # contract and must not track the caller's container type.
    metrics = scalar_metrics(_report("1"), [_tx(1_700_000_000_000)])
    assert type(metrics) is tuple
    assert all(type(metric.value) is float for metric in metrics)


# ------------------------------------------------------------ purity


def test_module_ast_imports_only_stdlib_and_the_two_model_modules():
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "auradefi"
        / "project"
        / "scalar.py"
    )
    allowed_internal = {"auradefi.portfolio.models", "auradefi.ledger.models"}
    offenders = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in sys.stdlib_module_names:
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level != 0:
                offenders.append(f"relative import: {base!r}")
            elif base.split(".")[0] in sys.stdlib_module_names:
                continue
            elif base not in allowed_internal:
                offenders.append(base)
    assert not offenders, (
        "project/scalar.py is PURE (SPEC §3.3): only stdlib, "
        f"auradefi.portfolio.models, auradefi.ledger.models: {offenders}"
    )


def test_inputs_are_not_mutated():
    transactions = [_tx(1_700_000_000_000), _tx(0)]
    snapshot = list(transactions)
    report = _report("5025")
    scalar_metrics(report, transactions)
    assert transactions == snapshot
    assert report == _report("5025")


def test_removed_transactions_are_counted_when_passed():
    # The contract counts EXACTLY what it is given; the CALLER filters
    # removed rows. A removed=True row passed in IS counted.
    values = _by_name(
        scalar_metrics(_report(), (_tx(1_700_000_000_000, removed=True),))
    )
    assert values["transaction_count"] == 1.0
    assert values["tx_count_hour_22"] == 1.0


def test_projection_is_deterministic_and_list_equals_tuple():
    transactions = [_tx(1_700_000_000_000), _tx(0), _tx(10**18)]
    report = _report("5025.25")
    first = scalar_metrics(report, transactions)
    assert first == scalar_metrics(report, transactions)
    assert first == scalar_metrics(report, tuple(transactions))


# ------------------------------------------------------------ boundaries


def test_every_hour_saturates_over_24_consecutive_hourly_transactions():
    # k=0..23 from 1_700_000_000_000 walks hours 22,23,0,1,…,21: the
    # full ring exactly once, derived independently via python3.
    transactions = tuple(
        _tx((1_700_000_000 + 3600 * k) * 1000) for k in range(24)
    )
    values = _by_name(scalar_metrics(_report(), transactions))
    assert values["transaction_count"] == 24.0
    assert all(values[name] == 1.0 for name in PINNED_NAMES[2:])


def test_negative_timestamps_wrap_without_raising():
    # Python floor-division makes the pinned formula total over negatives:
    # (-1 // 3_600_000) % 24 == 23; (-86_400_000 // 3_600_000) % 24 == 0;
    # (-3_600_001 // 3_600_000) % 24 == 22. Never raises (module contract).
    values = _by_name(
        scalar_metrics(_report(), (_tx(-1), _tx(-86_400_000), _tx(-3_600_001)))
    )
    assert values["transaction_count"] == 3.0
    assert values["tx_count_hour_23"] == 1.0
    assert values["tx_count_hour_00"] == 1.0
    assert values["tx_count_hour_22"] == 1.0


def test_negative_portfolio_value_projects_signed_not_clamped():
    # A borrow-heavy portfolio is net negative (SPEC §4.3 sign convention);
    # the projection must carry the sign, never clamp at zero.
    metrics = scalar_metrics(_report("-4200.5"), ())
    assert metrics[0] == Metric("portfolio_value_usd", AS_OF_MS, -4200.5)


def test_huge_value_at_10_pow_77_scale():
    # float(Decimal(10**77)) == 1e+77 exactly, derived independently.
    metrics = scalar_metrics(_report(str(10**77)), ())
    assert metrics[0].value == 1e77


def test_as_of_ms_is_carried_verbatim_including_zero():
    zero_report = HoldingsReport(
        address=ADDRESS,
        chain_id=CHAIN,
        holdings=(),
        total_value=Money(Decimal("1"), "USD"),
        unpriced=(),
        as_of_ms=0,
    )
    metrics = scalar_metrics(zero_report, (_tx(1_700_000_000_000),))
    assert len(metrics) == 26
    assert all(metric.at_ms == 0 for metric in metrics)


# ------------------------------------------------------------ immutability


def test_metric_fields_cannot_be_reassigned():
    metric = Metric("portfolio_value_usd", AS_OF_MS, 5025.0)
    with pytest.raises(AttributeError):
        metric.value = 1.0  # type: ignore[misc]
    with pytest.raises(AttributeError):
        metric.name = "other"  # type: ignore[misc]
