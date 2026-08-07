"""Manual price oracle: a caller-supplied override, highest precedence
(SPEC §3.2 ``prices/oracles/manual.py``; RELEASE_0.2.0 §5).

First in the declared chain (manual, defillama, coingecko, onchain_amm),
so a host that knows what an asset was worth says so and is believed. The
chain falls through only when the higher-precedence oracle DOES NOT HAVE
the asset, so an override is present or absent and never a number the
oracles below it argue with.

PURE. No HTTP, no clock, no I/O of any kind, at import or afterwards. The
imports are ``auradefi.errors``, ``auradefi.money.fiat`` and
``auradefi.prices.store``, every one of them inside this domain.
:func:`~auradefi.prices.store.bucket_start_ms` is taken from the store so
that this oracle and the historian agree on which instants are one
instant. ``prices.inquirer`` is never imported: conformance to
``PriceOracle`` and to ``HistoricalPriceOracle`` is STRUCTURAL, the way
``defillama.py`` conforms to the first.

THE TWO MAPS NEVER BLEED, in either direction. ``marks`` answers
:meth:`ManualOracle.usd_prices` and nothing else. ``dated_marks`` answers
:meth:`ManualOracle.usd_prices_at` and nothing else. An undated override
is today's number, and answering a 2021 question with it invents a mark
the caller never stated; a 2021 override is history, and answering "what
is it worth now" with it does the same in the other direction. Both are
incomplete data defaulted instead of declared (rule #8), and the defect
this module exists to keep out. An override this oracle does not hold is
ABSENT from the answer, and the chain moves on to the next oracle.

EVERY DATED INSTANT IS FLOORED AT CONSTRUCTION, with
:func:`~auradefi.prices.store.bucket_start_ms`, so two overrides landing
in one bucket for one asset collide LOUDLY: ``ValidationError`` naming
the asset and the bucket. Flooring only at the query door would let the
second override shadow the first in silence, and which of the two won
would depend on the order the caller's mapping happened to iterate in.

NO ``unreachable_instant``. A manual oracle reaches every instant; it
simply holds nothing for most of them, and "holds nothing" is "not
listed". Declaring an instant unreachable makes
``Inquirer.usd_prices_at`` skip the oracle for that instant without
calling it, which is the opposite of what an override is for.

THE ID IS COMPARED BYTE FOR BYTE, as ``prices/store.py`` compares it.
Canonicalising it here would mean importing ``auradefi.assets.caip``, and
the caller has already spelled it as ``assets.caip.canonical_caip19``
does before it reaches any price call site. A checksummed address and its
lowercase form are two keys here, in both maps.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from auradefi.money.fiat import Money


class ManualOracle:
    """USD marks a host states outright, at the current instant and in the past.

    Structurally a ``prices.inquirer.PriceOracle`` and a
    ``prices.inquirer.HistoricalPriceOracle``; imports neither. Holds no
    transport, reads no clock, and every answer it gives came from its
    constructor.
    """

    def __init__(
        self,
        marks: Mapping[str, Money] | None = None,
        dated_marks: Mapping[tuple[str, int], Money] | None = None,
    ) -> None:
        """Validate both maps and copy each into a private dict.

        ``marks`` holds current-instant overrides keyed by the CAIP-19
        string. ``dated_marks`` holds past-instant overrides keyed by
        ``(caip19, at_ms)``, and every instant is floored with
        :func:`~auradefi.prices.store.bucket_start_ms` HERE, at
        construction, before anything is stored.

        Both maps are COPIED. A caller that mutates the mapping it passed
        changes no answer this oracle gives afterwards, which matters
        because a host builds its overrides from configuration it goes on
        editing.

        ``ManualOracle()`` is legal and holds nothing: an empty override
        set is the ordinary case, and it prices nothing at either door.

        Raises:
            ValidationError: if either argument is neither ``None`` nor a
                mapping; if a ``marks`` key is not a string; if a
                ``dated_marks`` key is not a ``(caip19, at_ms)`` pair
                whose first half is a string and whose second half is a
                non-negative integer (``bool`` refused first of all,
                being an ``int`` subclass whose ``True`` would floor as
                the instant 1); if any value is not a
                :class:`~auradefi.money.fiat.Money`; or if two
                ``dated_marks`` for one asset floor into one bucket, in
                which case the message names the asset and the bucket.
            CurrencyMismatchError: if a value's currency is not ``"USD"``,
                the rule ``inquirer._checked_usd`` enforces where the
                chain's output is checked and the same one
                ``store.put`` holds on the cache side. Admitted here, a
                EUR override is multiplied by a quantity downstream and
                the product is stamped ``"USD"``.
        """
        raise NotImplementedError

    def usd_prices(self, caip19s: Sequence[str]) -> dict[str, Money]:
        """The undated overrides among ``caip19s``, and nothing besides.

        Keys are a subset of ``caip19s``; every value is ``Money`` with
        currency ``"USD"``, checked at construction. An id with no
        undated override is absent from the result, which is what sends
        the chain on to the next oracle.

        ``dated_marks`` IS NOT CONSULTED. A mark stated for a past
        instant is not an answer to "what is it worth now".

        Raises:
            ValidationError: if ``caip19s`` is not a list or tuple, or if
                any element is not a string. A bare CAIP-19 string is
                refused by name: iterated per character it would look up
                single characters, find none of them, and return ``{}``
                as though the override were absent.
        """
        raise NotImplementedError

    def usd_prices_at(
        self, caip19s: Sequence[str], at_ms: int
    ) -> dict[str, Money]:
        """The dated overrides for ``caip19s`` in ``at_ms``'s bucket.

        ``at_ms`` is floored with
        :func:`~auradefi.prices.store.bucket_start_ms` and matched
        against the already-floored keys, so an override stated at 00:30
        answers a question asked at 00:00 (one bucket, one entry) and
        01:00 is a different bucket that the same override does not
        answer.

        Keys are a subset of ``caip19s``; every value is ``Money`` with
        currency ``"USD"``. An id with no override in that bucket is
        absent. There is no ``unreachable_instant`` here: an instant this
        oracle holds nothing for is "not listed", never "cannot reach".

        ``marks`` IS NOT CONSULTED. Today's override is not an answer to
        a question about a past instant.

        Raises:
            ValidationError: if ``caip19s`` is not a list or tuple, if
                any element is not a string, or if ``at_ms`` is not a
                non-negative integer. ``bool`` is refused, and a negative
                instant is refused by ``bucket_start_ms``, which would
                otherwise name a bucket before the epoch.
        """
        raise NotImplementedError
