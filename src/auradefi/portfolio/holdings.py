"""Holdings assembly service — the Phase 1 deliverable (SPEC §11:
"EVM balances -> holdings. Etherscan V2 + DefiLlama prices. Single-tenant,
library-only").

Assembly is transport-free: this module NEVER imports httpx — portfolio is
not an I/O domain, and the layering gate enforces it. The seams:

* source — the LOCAL :class:`BalanceSource` protocol; the typed record is
  ``auradefi.sources.evm.etherscan.BalanceRecord`` (portfolio may import
  sources). ``EtherscanV2`` conforms structurally.
* prices — ``auradefi.prices.inquirer.PriceOracle``-shaped; an ``Inquirer``
  or a bare oracle both fit.
* clock — ``auradefi.clock.Clock``; defaults to ``SystemClock()``, tests
  inject ``FrozenClock``.

Pinned algorithm for :meth:`HoldingsService.holdings` (SPEC rule #5 — the
Phase 1 golden gate in tests/golden/test_phase1_holdings.py asserts the
numbers this produces):

1. ``records = source.balances(chain_id, address)``.
2. ``price_map = prices.usd_prices([r.caip19 for r in records])`` —
   EXACTLY ONE prices call, ids in record order.
3. Each record becomes ``Holding(caip19, symbol, quantity,
   price=price_map.get(caip19), value=Money(quantity.as_decimal() *
   price.amount, "USD"))`` when priced, else ``price=None, value=None``.
   The multiplication is EXACT at any magnitude — never subject to
   context-precision rounding (a 78-digit raw survives intact; display
   quantisation is project/'s job, a later phase). Record order is
   preserved.
4. Return ``HoldingsReport.assemble(address, chain_id, holdings,
   clock.now_ms())`` — address and chain_id echoed verbatim (the SOURCE
   normalises addresses for its own requests; the report echoes the
   caller's input untouched).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol, runtime_checkable

from auradefi.clock import Clock, SystemClock
from auradefi.money.fiat import Money
from auradefi.portfolio.models import Holding, HoldingsReport
from auradefi.prices.inquirer import PriceOracle
from auradefi.sources.evm.etherscan import BalanceRecord


def _exact_product(left: Decimal, right: Decimal) -> Decimal:
    """Context-free exact product — never rounded to context precision.

    Multiplies the integer coefficients and adds the exponents, so the
    result is exact at any magnitude (rule #1: a 78-digit coefficient
    survives intact where a context multiply would round to 28 digits).
    """
    left_sign, left_digits, left_exponent = left.as_tuple()
    right_sign, right_digits, right_exponent = right.as_tuple()
    coefficient = int("".join(map(str, left_digits))) * int(
        "".join(map(str, right_digits))
    )
    digits = tuple(int(char) for char in str(coefficient))
    sign = left_sign ^ right_sign
    return Decimal((sign, digits, left_exponent + right_exponent))


@runtime_checkable
class BalanceSource(Protocol):
    """Structural seam: typed balances for one (chain × address).

    Any object with a conforming ``balances`` method is a source —
    ``EtherscanV2`` conforms WITHOUT this module importing anything from
    it beyond the ``BalanceRecord`` record type.
    """

    def balances(self, chain_id: str, address: str) -> Sequence[BalanceRecord]:
        """Return typed balance records for ``address`` on ``chain_id``."""
        raise NotImplementedError


class HoldingsService:
    """Assembles source balances and oracle prices into a HoldingsReport.

    Pure orchestration — no HTTP of its own, no persistence, no rounding.
    Collaborators are injected; the constructor performs no I/O.
    """

    def __init__(
        self,
        source: BalanceSource,
        prices: PriceOracle,
        clock: Clock | None = None,
    ) -> None:
        """Bind ``source``, ``prices`` and ``clock``.

        ``clock=None`` means ``SystemClock()``; tests inject
        ``FrozenClock`` for a deterministic ``as_of_ms``.
        """
        self._source = source
        self._prices = prices
        self._clock = clock if clock is not None else SystemClock()

    def holdings(self, chain_id: str, address: str) -> HoldingsReport:
        """All holdings of ``address`` on ``chain_id``, priced in USD.

        Implements the module-docstring pinned algorithm: one source
        call, EXACTLY ONE ``prices.usd_prices`` call with the record
        caip19 ids in record order, exact Decimal multiplication for
        each priced value (currency ``"USD"``), unpriced records kept
        with ``price=None, value=None``, record order preserved, and
        ``HoldingsReport.assemble(address, chain_id, holdings,
        clock.now_ms())`` echoing ``address``/``chain_id`` verbatim.
        """
        records = self._source.balances(chain_id, address)
        price_map = self._prices.usd_prices([record.caip19 for record in records])
        holdings: list[Holding] = []
        for record in records:
            price = price_map.get(record.caip19)
            value = (
                Money(
                    _exact_product(record.quantity.as_decimal(), price.amount),
                    # The PRICE's currency, never a hardcoded "USD". Stamping
                    # "USD" over a EUR price produced a total off by the FX
                    # rate, labelled USD, with nothing in `unpriced` and
                    # nothing raised — the caller could not tell it was
                    # wrong, which is worse than no price at all. The oracle
                    # contract says every price is USD; this stops a
                    # violation of it from being SILENT (§5 #23).
                    price.currency,
                )
                if price is not None
                else None
            )
            holdings.append(
                Holding(
                    caip19=record.caip19,
                    symbol=record.symbol,
                    quantity=record.quantity,
                    price=price,
                    value=value,
                )
            )
        return HoldingsReport.assemble(
            address, chain_id, holdings, self._clock.now_ms()
        )
