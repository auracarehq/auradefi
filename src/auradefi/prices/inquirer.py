"""Price inquirer — the prices seam, Phase 1 current-USD lookups only
(SPEC §3.2 prices/inquirer.py, §3.3 layer contract).

:class:`PriceOracle` is the structural seam: any object with a conforming
``usd_prices`` method is an oracle — concrete oracles (e.g.
``prices/oracles/defillama.py``) conform WITHOUT this module importing
them, which keeps the build orders independent. This module performs no
HTTP and never imports ``httpx`` or ``auradefi.prices.oracles``.

Oracle implementation contract:

* return only ids you can price — result keys are a subset of the input;
* every returned :class:`~auradefi.money.fiat.Money` has currency ``"USD"`` —
  ENFORCED by :class:`Inquirer`, which raises
  :class:`~auradefi.errors.CurrencyMismatchError` on anything else rather
  than letting a non-USD quote be relabelled downstream (§5 #23).

:class:`Inquirer` composes oracles, first-wins:

* deduplicate the input, preserving first occurrence;
* query oracles in construction order; ask each subsequent oracle only
  for ids still unpriced; skip remaining oracles once everything priced;
* return the merged dict — unpriced ids are ABSENT, never an error;
* a syntactically invalid CAIP-19 raises
  :class:`~auradefi.errors.CaipParseError` BEFORE any oracle call
  (validation delegates to :func:`auradefi.assets.caip.parse_caip19`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from auradefi.assets.caip import parse_caip19
from auradefi.errors import CurrencyMismatchError
from auradefi.money.fiat import Money


@runtime_checkable
class PriceOracle(Protocol):
    """Structural interface: current USD prices for CAIP-19 asset ids."""

    def usd_prices(self, caip19s: Sequence[str]) -> dict[str, Money]:
        """Return USD prices for the ids this oracle can price.

        Keys are a subset of ``caip19s``; every value is ``Money`` with
        currency ``"USD"``. Ids the oracle cannot price are simply absent.
        """
        raise NotImplementedError


class Inquirer:
    """First-wins USD price aggregation over an ordered oracle sequence."""

    def __init__(self, oracles: Sequence[PriceOracle]) -> None:
        """Hold ``oracles``; query order is construction order."""
        self._oracles = tuple(oracles)

    def usd_prices(self, caip19s: Sequence[str]) -> dict[str, Money]:
        """Merged first-wins USD prices for ``caip19s``.

        Validates every id up front (``CaipParseError`` before any oracle
        call), deduplicates preserving first occurrence, then walks the
        oracles in order asking each only for the still-unpriced ids and
        stopping early once everything is priced. Unpriced ids are absent
        from the result — never an error.
        """
        for caip19 in caip19s:
            parse_caip19(caip19)
        pending = list(dict.fromkeys(caip19s))
        priced: dict[str, Money] = {}
        for oracle in self._oracles:
            if not pending:
                break
            priced.update(_checked_usd(oracle.usd_prices(pending), oracle))
            pending = [caip19 for caip19 in pending if caip19 not in priced]
        return priced


def _checked_usd(
    quotes: dict[str, Money], oracle: PriceOracle
) -> dict[str, Money]:
    """``quotes`` unchanged, or ``CurrencyMismatchError`` naming the oracle.

    The oracle contract at the top of this module says every returned
    ``Money`` has currency ``"USD"``. It was documented and never checked,
    and oracles are HOST-SUPPLIED — so a EUR quote flowed through
    ``portfolio.holdings``, which multiplied it by a quantity and stamped
    the product ``"USD"``. The result was a portfolio total wrong by the
    FX rate, labelled as dollars, with the asset absent from ``unpriced``
    and nothing raised anywhere (RELEASE_0.1.1 §5 #23).

    Checked here because this is the boundary the contract is written at:
    the ONE place every composed oracle's output passes through.
    """
    for caip19, quote in quotes.items():
        if quote.currency != "USD":
            raise CurrencyMismatchError(
                f"{type(oracle).__name__} returned {quote.currency!r} for "
                f"{caip19!r}; an oracle must return USD (see the oracle "
                "implementation contract in this module's docstring)"
            )
    return quotes
