"""Scalar projection: ``(metric, timestamp, float)`` triples (SPEC §6, §8).

For hosts whose metrics pipeline is scalar-only. Emits at minimum
``portfolio_value_usd`` and ``transaction_count``, plus activity cadence
(transactions per UTC hour-of-day). Timing is a signal in its own right
and costs nothing to derive (SPEC §8).

PURE (SPEC §3.3): this module imports ONLY the stdlib,
``auradefi.portfolio.models.HoldingsReport`` and
``auradefi.ledger.models.LedgerTransaction``, no I/O, no DB, no
framework. It never raises: every input combination projects to the same
fixed 26-metric shape.

The ``float`` values are documented LOSSY and display-only. Exact money
stays ``Decimal`` everywhere else in the system (SPEC rule #1); a scalar
pipeline is the one consumer that gets floats, on purpose, at the edge.

USD is a PRECONDITION, not a conversion. ``portfolio_value_usd`` projects
``report.total_value.amount`` verbatim and never reads ``.currency``.
USD denomination is a system-wide invariant (SPEC §6.3:
``unofficial_currency_code`` cannot express most crypto assets, so
everything is priced in USD), enforced upstream by
``HoldingsReport.assemble``, which raises ``CurrencyMismatchError`` on a
non-USD holding. Re-checking it here would break purity,
``auradefi.errors`` is not an importable module from this layer, so the
rule is stated instead: a report NOT built by ``assemble``, carrying a
non-USD ``total_value``, ships its bare amount under the ``_usd`` name.
Satisfying the precondition is the caller's job.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from auradefi.ledger.models import LedgerTransaction
from auradefi.portfolio.models import HoldingsReport


class Metric(NamedTuple):
    """One scalar sample: ``(name, ms-epoch timestamp, float value)``.

    A ``NamedTuple`` so it unpacks and compares equal to a plain tuple:
    hosts feed it to any ``(str, int, float)``-shaped pipeline unchanged.
    """

    name: str
    at_ms: int
    value: float


def scalar_metrics(
    report: HoldingsReport,
    transactions: Sequence[LedgerTransaction],
) -> tuple[Metric, ...]:
    """Project a report + transactions to EXACTLY 26 metrics, pinned order.

    ASSUMES ``report.total_value.currency == 'USD'``: the caller's
    precondition, guaranteed for any report built by
    ``HoldingsReport.assemble`` (SPEC §6.3). It is NOT re-checked and NOT
    converted: a hand-built report denominated otherwise ships its bare
    amount under the ``portfolio_value_usd`` name. Nothing here can
    signal that, because this function never raises.

    Counts EXACTLY the transactions given: the caller filters removed
    rows first; a ``removed=True`` transaction that is passed in IS
    counted. Inputs are never mutated. Every ``at_ms`` equals
    ``report.as_of_ms``. Never raises.

    Pinned emission (SPEC §6/§8):

    * ``[0]`` ``Metric('portfolio_value_usd', as_of_ms,
      float(report.total_value.amount))``: the amount only, currency
      unread (see the precondition above). ``float()`` is LOSSY,
      display-only; the exact ``Decimal`` lives on the report.
    * ``[1]`` ``Metric('transaction_count', as_of_ms,
      float(len(transactions)))``.
    * ``[2..25]`` one per UTC hour, ``f'tx_count_hour_{h:02d}'`` for
      ``h`` 0..23 ascending; value = float count of transactions with
      ``(initiated_at // 3_600_000) % 24 == h``: pinned integer formula,
      no ``datetime``. Hours with no transactions emit ``0.0``.
    """
    hour_counts = [0] * 24
    for transaction in transactions:
        hour_counts[(transaction.initiated_at // 3_600_000) % 24] += 1
    at_ms = report.as_of_ms
    return (
        Metric("portfolio_value_usd", at_ms, float(report.total_value.amount)),
        Metric("transaction_count", at_ms, float(len(transactions))),
        *(
            Metric(f"tx_count_hour_{hour:02d}", at_ms, float(hour_counts[hour]))
            for hour in range(24)
        ),
    )
