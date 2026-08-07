"""Price inquirer: the prices seam, USD at the current instant and in the
past (SPEC §3.2 prices/inquirer.py, §3.3 layer contract).

:class:`PriceOracle` is the structural seam: any object with a conforming
``usd_prices`` method is an oracle: concrete oracles (e.g.
``prices/oracles/defillama.py``) conform WITHOUT this module importing
them, which keeps the build orders independent. This module performs no
HTTP and never imports ``httpx`` or ``auradefi.prices.oracles``.

Oracle implementation contract:

* return only ids you can price, result keys are a subset of the input;
* every returned :class:`~auradefi.money.fiat.Money` has currency ``"USD"``,
  ENFORCED by :class:`Inquirer`, which raises
  :class:`~auradefi.errors.CurrencyMismatchError` on anything else rather
  than letting a non-USD quote be relabelled downstream (§5 #23);
* OPTIONAL ``usd_prices_at(caip19s, at_ms)``, the same answer as of a
  millisecond-epoch instant (:class:`HistoricalPriceOracle`). An oracle
  without it answers the current instant only, and
  :meth:`Inquirer.usd_prices_at` skips it without calling anything, so a
  0.1.x oracle keeps working and is never asked a question it cannot
  answer;
* OPTIONAL ``unreachable_instant(at_ms) -> str | None``. ``None`` means "I
  can reach that instant". A string is a stated reason a caller may show a
  user, and it makes :class:`Inquirer` skip the oracle for that instant
  WITHOUT calling ``usd_prices_at``, so an instant an oracle cannot reach
  costs zero I/O;
* OPTIONAL ``absences_at(at_ms) -> tuple[str, ...]`` for an oracle that is
  itself a chain, :class:`Inquirer` among them.
  :meth:`Inquirer.absences_at` splices whatever it returns in at that
  oracle's position, so a chain nested inside another chain still reports
  the oracles IT skipped.

Both optional members are probed with ``callable(getattr(oracle, name,
None))``, never ``isinstance`` and never ``inspect.getattr_static``, so an
oracle reached through a wrapper or a ``__getattr__`` proxy is not mistaken
for one that lacks the member (the ``embed/facade.py`` precedent, §5 #21).

:class:`Inquirer` composes oracles, first-wins:

* deduplicate the input, preserving first occurrence;
* query oracles in construction order; ask each subsequent oracle only
  for ids still unpriced; skip remaining oracles once everything priced;
* return the merged dict. Unpriced ids are ABSENT, never an error;
* a syntactically invalid CAIP-19 raises
  :class:`~auradefi.errors.CaipParseError` BEFORE any oracle call
  (validation delegates to :func:`auradefi.assets.caip.parse_caip19`).

:meth:`Inquirer.usd_prices_at` walks that same chain at an instant, under
the two extra skip rules above. :meth:`Inquirer.absences_at` is the channel
that says WHICH oracles were skipped: a ``{}`` from an oracle that was
asked and a silence from an oracle that was never asked reach the merged
result identically, and a caller that wants to tell them apart asks for the
absences. It descends into a nested chain, because an :class:`Inquirer` is
an oracle at an instant too and the channel would otherwise stop at the
outermost one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from auradefi.assets.caip import parse_caip19
from auradefi.errors import CurrencyMismatchError, ValidationError, require_int
from auradefi.money.fiat import Money

#: What a conforming ``usd_prices_at`` looks like once probed off an
#: oracle. Named so the probe helper can hand a typed callable back to the
#: walk instead of an ``object`` nothing can call.
_InstantQuery = Callable[[Sequence[str], int], dict[str, Money]]


@runtime_checkable
class PriceOracle(Protocol):
    """Structural interface: current USD prices for CAIP-19 asset ids."""

    def usd_prices(self, caip19s: Sequence[str]) -> dict[str, Money]:
        """Return USD prices for the ids this oracle can price.

        Keys are a subset of ``caip19s``; every value is ``Money`` with
        currency ``"USD"``. Ids the oracle cannot price are simply absent.
        """
        raise NotImplementedError


@runtime_checkable
class HistoricalPriceOracle(Protocol):
    """Structural interface: USD prices as of a past instant.

    A SEPARATE protocol on purpose, never a second member of
    :class:`PriceOracle`. That one is ``runtime_checkable``, so a second
    member makes ``isinstance`` false for every object carrying only
    ``usd_prices``, which is every host oracle written against 0.1.x.
    Conformance here is structural too: a phase-12 oracle matches this
    signature without importing this module.
    """

    def usd_prices_at(
        self, caip19s: Sequence[str], at_ms: int
    ) -> dict[str, Money]:
        """Return USD prices for ``caip19s`` as of ``at_ms``.

        ``at_ms`` is a millisecond-epoch integer. Keys are a subset of
        ``caip19s``; every value is ``Money`` with currency ``"USD"``. Ids
        this oracle cannot price at that instant are simply absent.
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
        from the result, never an error.
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

    def usd_prices_at(
        self, caip19s: Sequence[str], at_ms: int
    ) -> dict[str, Money]:
        """Merged first-wins USD prices for ``caip19s`` as of ``at_ms``.

        The same walk as :meth:`usd_prices`: every id is parsed up front
        (``CaipParseError`` before any oracle call), the input is
        deduplicated preserving first occurrence, oracles are queried in
        construction order, each subsequent one is asked only for the
        still-unpriced ids, the walk stops once everything is priced, and
        unpriced ids are absent from the result.

        Two rules apply here and not there. An oracle with no callable
        ``usd_prices_at`` is SKIPPED and never called, so a 0.1.x oracle
        cannot answer a 2021 question with today's number. An oracle whose
        ``unreachable_instant(at_ms)`` returns a string is SKIPPED WITHOUT
        being called, so an instant it cannot reach costs zero I/O.
        :meth:`absences_at` reports both kinds of skip.

        Raises ``ValidationError`` when ``at_ms`` is not an integer
        (``bool`` included) and ``CaipParseError`` for a malformed id.
        """
        for caip19 in caip19s:
            parse_caip19(caip19)
        # Ids first: CaipParseError subclasses ValidationError, so a caller
        # catching the parse error specifically still gets it when both
        # arguments are wrong.
        require_int(at_ms, "at_ms", ValidationError)
        pending = list(dict.fromkeys(caip19s))
        priced: dict[str, Money] = {}
        for oracle in self._oracles:
            if not pending:
                break
            query = _instant_query(oracle)
            if query is None or _stated_reason(oracle, at_ms) is not None:
                continue
            priced.update(_checked_usd(query(pending, at_ms), oracle))
            pending = [caip19 for caip19 in pending if caip19 not in priced]
        return priced

    def absences_at(self, at_ms: int) -> tuple[str, ...]:
        """One stated reason per oracle that cannot answer at ``at_ms``.

        PURE: no I/O, and neither ``usd_prices`` nor ``usd_prices_at`` is
        called on any oracle. The strings come back in construction order,
        and an oracle that can answer contributes nothing.

        An oracle with a callable ``unreachable_instant`` contributes
        whatever string it returns for ``at_ms``. Otherwise an oracle with
        no callable ``usd_prices_at`` contributes the generated sentence
        ``f"{type(oracle).__name__} has no usd_prices_at; it answers the
        current instant only"``. An oracle the walk WOULD ask contributes
        its own absences when it is a chain in its own right
        (:func:`_composed_absences`), and nothing when it is a leaf.

        Raises ``ValidationError`` when ``at_ms`` is not an integer.
        """
        require_int(at_ms, "at_ms", ValidationError)
        absences: list[str] = []
        for oracle in self._oracles:
            # The same two rules :meth:`usd_prices_at` skips on, read
            # through the same two helpers, so the list can never disagree
            # with the walk about which oracles were passed over.
            stated = _stated_reason(oracle, at_ms)
            if stated is not None:
                absences.append(stated)
            elif _instant_query(oracle) is None:
                absences.append(
                    f"{type(oracle).__name__} has no usd_prices_at; it "
                    "answers the current instant only"
                )
            else:
                absences.extend(_composed_absences(oracle, at_ms))
        return tuple(absences)


def _instant_query(oracle: object) -> _InstantQuery | None:
    """``oracle.usd_prices_at`` when it is callable, else ``None``.

    ``callable(getattr(...))`` and never ``isinstance``, following
    ``embed/facade.py``'s precedent (§5 #21). Two shapes make the
    difference visible. An oracle whose ``usd_prices_at`` is an attribute
    rather than a method satisfies a ``runtime_checkable`` ``isinstance``
    and ``hasattr`` both, and the walk would then call a string and die
    with a ``TypeError`` outside the auradefi taxonomy. An oracle reached
    through a ``__getattr__`` proxy fails that ``isinstance``, because
    ``_ProtocolMeta`` looks the member up with ``inspect.getattr_static``,
    which is blind to the wrapper: probing by ``isinstance`` would demote a
    decorated oracle to a legacy one and report an absence for an oracle
    that could have answered.
    """
    query = getattr(oracle, "usd_prices_at", None)
    return query if callable(query) else None


def _stated_reason(oracle: object, at_ms: int) -> str | None:
    """The oracle's own reason for not reaching ``at_ms``, else ``None``.

    ``unreachable_instant`` is optional, and ``None`` back from it means "I
    can reach that instant", so an oracle that does not carry the member at
    all reads the same as one that answered ``None``. A string is returned
    unedited: :meth:`Inquirer.absences_at` shows it to a caller verbatim.
    """
    stated = getattr(oracle, "unreachable_instant", None)
    if not callable(stated):
        return None
    reason = stated(at_ms)
    return reason if isinstance(reason, str) else None


def _composed_absences(oracle: object, at_ms: int) -> tuple[str, ...]:
    """A composite oracle's own absences at ``at_ms``, else ``()``.

    An :class:`Inquirer` conforms to :class:`HistoricalPriceOracle`, so a
    chain can hold another chain, and a nested one used to swallow the
    channel whole. It carries a callable ``usd_prices_at`` and states no
    reason of its own, so the outer walk read it as an oracle that could
    answer while every oracle INSIDE it was skipped unreported: a legacy
    oracle at depth 2 left an unpriced id with no note against it, which is
    the reading reserved for an asset no oracle lists at all.

    Probed the way the other two optional members are, ``callable(getattr(
    ...))``, so a chain behind a wrapper still reports. Only strings are
    spliced, matching :func:`_stated_reason`: these go to a caller verbatim.
    """
    nested = getattr(oracle, "absences_at", None)
    if not callable(nested):
        return ()
    return tuple(
        reason for reason in nested(at_ms) if isinstance(reason, str)
    )


def _checked_usd(
    quotes: dict[str, Money], oracle: PriceOracle
) -> dict[str, Money]:
    """``quotes`` unchanged, or ``CurrencyMismatchError`` naming the oracle.

    The oracle contract at the top of this module says every returned
    ``Money`` has currency ``"USD"``. It was documented and never checked,
    and oracles are HOST-SUPPLIED, so a EUR quote flowed through
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
