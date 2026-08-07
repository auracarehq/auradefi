"""Historical price cache: the port, its memory backend, and the resolution
(SPEC §3.2 ``prices/store.py``; RELEASE_0.2.0 §5).

:class:`PriceStore` is the structural seam, shaped after
``ledger/port.py``: a ``runtime_checkable`` ``Protocol``, so a host binds
Redis or Postgres by matching the shape and this package grows no
dependency for it (rule #7). :class:`MemoryPriceStore` is the reference
backend, a plain dict, and the thing every other backend is compared to.

THE RESOLUTION IS PART OF THE CONTRACT, not an implementation detail.
:data:`RESOLUTION_MS` is one hour, and :func:`bucket_start_ms` floors an
instant onto it. RELEASE_0.2.0 §5 asks the question directly: a mark
asked for at 12:00:31 and a mark asked for at 12:00:59 must either be the
same cache entry or provably different ones. Here they are the SAME
entry, because both floor to 12:00:00; 12:59:59.999 and 13:00:00 are
different entries. Flooring is idempotent, which matters because the
historian floors before it calls and this store floors again.

THE KEY IS THE CAIP-19 STRING plus the bucket. It is never the ``ast_``
registry id. DECISIONS.md pins ``asset_id`` as a hash over a SET of
canonical CAIP-19s, so USDC on Ethereum and USDC on Polygon share one
``ast_`` id and would collide into one cache entry at one price. The
registry that mints those ids is also never constructed anywhere in the
shipped runtime path, so an ``ast_`` key would cache nothing at all,
least of all the long-tail tokens an on-chain oracle exists to price.
Every price call site already speaks CAIP-19 (``inquirer.usd_prices``,
``portfolio/holdings.py``, ``defillama.coin_key``), and DECISIONS.md says
of ``Part.asset_id``: "a canonical CAIP-19 string ... never the ``ast_``
registry id".

CANONICALISING THAT STRING IS THE CALLER'S JOB. It is a precondition of
the port, and this module cannot keep it as a promise: canonicalising
would mean importing ``auradefi.assets.caip``, and the imports here are
``auradefi.errors`` and ``auradefi.money.fiat`` and nothing else, so that
every oracle can import the store without pulling the asset layer in
behind it. The store therefore compares ids byte for byte. DECISIONS.md
lowercases EVM addresses when it canonicalises, so a checksummed DAI
address and its lowercase form are two entries at two prices here, and a
caller that alternates between the two spellings gets a miss each time
and makes the request it believed the cache had saved. Well-formedness is
the caller's too: the store checks that an id is a string and stops
there, so ``""`` is a usable key. Call
``assets.caip.canonical_caip19`` first. ``Inquirer.usd_prices_at`` runs
``parse_caip19`` for validation and then keys by the string it was
handed, so an id reaches this store exactly as its caller spelled it.

ONLY MARKS ARE STORED. A declared-unpriced id is never written, because
"we could not price it then" is a different claim from "it is
unpriceable", and rule #8 says incomplete data is declared, never
defaulted. A host that adds a manual override after a failed lookup must
see that override on the very next call, which a cached miss would hide.
:meth:`PriceStore.get` answers a miss with ``None``: never an error,
never a zero.

Pure by construction: no HTTP, no clock, no I/O. Imports are
``auradefi.errors`` and ``auradefi.money.fiat`` and nothing else.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from auradefi.errors import (
    CurrencyMismatchError,
    ValidationError,
    require_int,
    require_str,
)
from auradefi.money.fiat import Money

#: The declared cache resolution: one hour, in milliseconds. Imported by
#: the historian and by the oracles, so that one number answers "which
#: instants share a mark" for the whole prices domain.
RESOLUTION_MS: int = 3_600_000


def bucket_start_ms(at_ms: int) -> int:
    """The start of the :data:`RESOLUTION_MS` bucket containing ``at_ms``.

    Pure, total over the domain, and IDEMPOTENT: ``bucket_start_ms`` of a
    bucket start is that same bucket start. The historian floors an
    instant before it asks the store, and the store floors again when it
    builds the key, so a floor that moved on the second application would
    put the write and the read in different buckets.

    Args:
        at_ms: a millisecond-epoch instant (rule #3).

    Returns:
        ``(at_ms // RESOLUTION_MS) * RESOLUTION_MS``.

    Raises:
        ValidationError: if ``at_ms`` is not a real ``int`` (``bool`` is
            refused first of all, being an ``int`` subclass whose ``True``
            would otherwise floor as the instant 1), or if it is
            negative. Floor division of a negative instant names a bucket
            before the epoch, and this package prices nothing pre-1970.
    """
    require_int(at_ms, "at_ms", ValidationError)
    if at_ms < 0:
        raise ValidationError(
            "at_ms must be a non-negative millisecond-epoch instant, "
            f"got {at_ms}; flooring it would name a bucket before the epoch"
        )
    return (at_ms // RESOLUTION_MS) * RESOLUTION_MS


def _key(caip19: str, at_ms: int) -> tuple[str, int]:
    """The cache key: the CAIP-19 string as given, and its bucket.

    One builder for both sides of the store, so a read and a write of the
    same id and hour cannot disagree about which entry they mean, and so
    every id or instant refused on the way in is refused on the way out.
    The id is not rewritten here. Canonicalising it is the caller's
    precondition, stated on :class:`PriceStore`.
    """
    return (
        require_str(caip19, "caip19", ValidationError),
        bucket_start_ms(at_ms),
    )


@runtime_checkable
class PriceStore(Protocol):
    """Structural contract for historical price caches.

    Keyed by the CAIP-19 string and the :func:`bucket_start_ms` bucket of
    the instant. A host satisfies this by matching the shape (rule #7,
    rule #12): no base class to import, no registration.

    CALLER PRECONDITION: ``caip19`` arrives canonical, as
    ``assets.caip.canonical_caip19`` spells it. A backend compares the id
    it is given, and a backend that lowercased on the way in would answer
    a different set of questions from one that did not. Two spellings of
    one address are two entries in every implementation of this port.

    Both methods refuse the same ids and instants, on the read side as
    well as the write side, so a lookup can never be phrased under a key
    no write could have used and read the empty answer as a miss.
    """

    def get(self, caip19: str, at_ms: int) -> Money | None:
        """The cached USD mark for ``caip19`` in ``at_ms``'s bucket.

        Returns ``None`` when nothing was stored for that bucket. A miss
        is never an error and never a zero (rule #8): the caller decides
        whether to ask an oracle, and a caller that cannot tell a miss
        from a mark of zero would report a portfolio as worthless.

        Raises:
            ValidationError: if ``caip19`` is not a ``str``, or if
                ``at_ms`` fails :func:`bucket_start_ms`. Refusing on read
                is part of the seam and not one backend's habit: a
                backend that answered ``None`` for ``get(60, at_ms)``
                would report an unaskable question as an empty cache, and
                one that let its client raise would put a builtin outside
                the ``auradefi.errors`` taxonomy in front of the caller
                (rule #4).
        """
        raise NotImplementedError

    def put(self, caip19: str, at_ms: int, price: Money) -> None:
        """Store ``price`` as the mark for ``caip19`` in ``at_ms``'s bucket.

        Only marks are stored. A declared-unpriced id is never written
        here, so a manual override added after a failed lookup is visible
        on the next call.

        Raises:
            ValidationError: if ``caip19`` is not a ``str``, if ``price``
                is not a :class:`~auradefi.money.fiat.Money`, or if
                ``at_ms`` fails :func:`bucket_start_ms`.
            CurrencyMismatchError: if ``price.currency`` is not ``"USD"``,
                the same boundary ``inquirer._checked_usd`` enforces on
                the oracle side. A EUR mark relabelled as dollars is a
                total wrong by the FX rate with nothing raised anywhere.
        """
        raise NotImplementedError


class MemoryPriceStore:
    """Reference :class:`PriceStore`: one dict, held per instance.

    Keyed ``(caip19, bucket_start_ms(at_ms))``, with the id compared byte
    for byte and never rewritten, so the caller's canonicalisation
    precondition holds here as it holds for every other backend. All
    state is instance state, so two stores never share marks. Unbounded
    on purpose: a host that needs eviction implements the protocol
    itself.
    """

    def __init__(self) -> None:
        """Start empty."""
        self._marks: dict[tuple[str, int], Money] = {}

    def get(self, caip19: str, at_ms: int) -> Money | None:
        """The stored mark for ``caip19`` in ``at_ms``'s bucket, or ``None``.

        ``None`` means nothing was stored for that bucket, and that is the
        only thing it means: a miss is never an error and never a zero
        (rule #8). An id or an instant the store would refuse on the way
        in is refused on the way out too, through the same key builder,
        so a caller cannot ask under a key no write could ever have used
        and read the answer as "not cached".

        Raises:
            ValidationError: non-``str`` ``caip19``, or an ``at_ms``
                :func:`bucket_start_ms` refuses. Without these the dict
                lookup itself would raise ``TypeError`` for an unhashable
                id, which is a builtin escaping the taxonomy (rule #4).
        """
        return self._marks.get(_key(caip19, at_ms))

    def put(self, caip19: str, at_ms: int, price: Money) -> None:
        """Store ``price`` as the mark for ``caip19`` in ``at_ms``'s bucket.

        Every check runs before the write, so a refused mark leaves the
        store exactly as it was. A later write into one bucket replaces
        the mark already there, which is how a host's manual override
        supersedes an aggregator's number.

        Raises:
            ValidationError: non-``str`` ``caip19``, non-``Money``
                ``price``, or an ``at_ms`` :func:`bucket_start_ms`
                refuses.
            CurrencyMismatchError: ``price.currency`` is not ``"USD"``.
        """
        key = _key(caip19, at_ms)
        if not isinstance(price, Money):
            raise ValidationError(
                f"price must be a Money, got {type(price).__name__}; a bare "
                "amount stored untagged is a number in no currency"
            )
        if price.currency != "USD":
            raise CurrencyMismatchError(
                f"a stored mark must be USD, got {price.currency!r} for "
                f"{caip19!r}; the same boundary the price inquirer holds on "
                "the oracle side"
            )
        self._marks[key] = price


__all__ = [
    "RESOLUTION_MS",
    "MemoryPriceStore",
    "PriceStore",
    "bucket_start_ms",
]
