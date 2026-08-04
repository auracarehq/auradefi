"""Pure HTTP body projections (SPEC §6.4, §7.3, §10; rules #1, #2, #10).

Every function here maps already-fetched domain objects to plain ``dict``
bodies. **PURE**: this module imports no web framework, no HTTP client and
performs no I/O — so the whole output contract is unit-testable from
fixtures, with no app, no client and no cassette (SPEC rule #11: the HTTP
API is a thin shell over an importable core).

It also does NOT import ``auradefi.portfolio`` — that domain is absent
from ``api``'s row in ``tests/style/test_layering.py``. ``holdings_wire``
is therefore duck-typed over anything shaped like
``auradefi.portfolio.models.HoldingsReport``.

Amount discipline, restated because this is the layer where it is lost:
every raw on-chain amount travels as the pinned four-field ``Quantity``
wire dict whose ``raw`` is a **string** (rule #2), and every fiat amount
travels as a tagged decimal string (rule #1). No rounding happens here;
the only float on the wire is ``Quantity``'s documented-lossy display
field.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from auradefi.chains.registry import Chain
from auradefi.errors import ValidationError
from auradefi.ledger.models import LedgerTransaction, SyncEventKind, SyncPage
from auradefi.money.decimal_json import money_to_wire, quantity_to_wire

__all__ = [
    "CAPABILITY_NAMES",
    "batch_envelope",
    "batch_error",
    "batch_result",
    "batch_warning",
    "coverage_payload",
    "holdings_wire",
    "sync_envelope",
    "transaction_wire",
]

# The per-capability coverage vocabulary (SPEC §10, rule #10). Fixed,
# ordered, and the ONLY thing a coverage row may report on.
CAPABILITY_NAMES: tuple[str, ...] = (
    "balances",
    "transactions",
    "positions",
    "prices",
    "xpub",
)


def transaction_wire(txn: LedgerTransaction) -> dict[str, Any]:
    """Project one ``LedgerTransaction`` to its HTTP body.

    Exactly eight keys, always present — a ``None`` ``block_number`` or
    ``confirmed_at`` serialises as JSON ``null``, never as an omitted key
    (a consumer must be able to tell "unconfirmed" from "field missing")::

        {"transaction_id": txn.id,
         "account_id":     txn.account_id,
         "chain_id":       txn.chain_id,
         "tx_hash":        txn.tx_hash,
         "block_number":   txn.block_number,      # int | None
         "initiated_at_ms": txn.initiated_at,     # ms epoch (SPEC §4.4)
         "confirmed_at_ms": txn.confirmed_at,     # ms epoch | None
         "entries": [{"asset_id", "direction", "quantity"}, ...]}

    Entries keep the transaction's own order. Each entry's ``quantity``
    is ``quantity_to_wire(entry.quantity)`` — the four-field dict
    ``{'raw', 'decimals', 'numeric', 'float'}`` whose ``raw`` is a
    STRING (rule #2) and whose ``numeric`` is exact with no scientific
    notation at any magnitude.

    Pure: no I/O, never raises.
    """
    return {
        "transaction_id": txn.id,
        "account_id": txn.account_id,
        "chain_id": txn.chain_id,
        "tx_hash": txn.tx_hash,
        "block_number": txn.block_number,
        "initiated_at_ms": txn.initiated_at,
        "confirmed_at_ms": txn.confirmed_at,
        "entries": [
            {
                "asset_id": entry.asset_id,
                "direction": entry.direction.value,
                "quantity": quantity_to_wire(entry.quantity),
            }
            for entry in txn.entries
        ],
    }


def sync_envelope(page: SyncPage) -> dict[str, Any]:
    """Project a ``SyncPage`` into Plaid's exact ``/crypto/sync`` envelope.

    Exactly five keys — ``added``, ``modified``, ``removed``,
    ``next_cursor``, ``has_more`` (SPEC §6.4)::

        added    = [transaction_wire(e.transaction) for ADDED events]
        removed  = [{"transaction_id", "account_id"}]  # exactly two keys
        modified = []                                  # see below
        next_cursor = page.next_cursor
        has_more    = page.has_more

    ``added`` and ``removed`` preserve the page's ascending
    last-modified order (SPEC §6.4: last-modified order, NOT transaction
    date — that is what lets a two-year-old row reappear).

    ``modified`` is ALWAYS a FRESH empty list, and nothing can ever land
    in it: ``SyncEventKind`` has exactly two members, a changed payload
    re-emits as ADDED with a bumped seq, and a reorg is REMOVED plus a
    re-ADDED. The key exists solely to keep Plaid's envelope shape
    intact for a client that iterates all three arrays. Freshness is
    part of the contract — a shared module-level ``[]`` would let one
    caller's mutation leak into the next response.

    Pure: no I/O. Raises :class:`auradefi.errors.ValidationError` on an
    event kind that is neither ADDED nor REMOVED — a backend that invents
    a third kind is refused loudly rather than having its event guessed
    into ``added``.
    """
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for event in page.events:
        # Compare by VALUE and route both kinds explicitly. SyncEventKind
        # is a StrEnum and SyncEvent is an unvalidated frozen dataclass, so
        # a third-party LedgerPort backend (rule #12 makes those first
        # class) that rebuilds `kind` from a database text column yields
        # the plain string "removed" — equal but not identical. Under a
        # catch-all `else: added` that deletion would be projected as an
        # add, and the client would keep a transaction the ledger dropped:
        # silently wrong numbers, the exact failure mode this codebase
        # exists to avoid. An unrecognised kind is refused, never guessed.
        if event.kind == SyncEventKind.REMOVED:
            removed.append(
                {
                    "transaction_id": event.transaction.id,
                    "account_id": event.transaction.account_id,
                }
            )
        elif event.kind == SyncEventKind.ADDED:
            added.append(transaction_wire(event.transaction))
        else:
            raise ValidationError(f"unknown sync event kind: {event.kind!r}")
    return {
        "added": added,
        # A fresh list, and nothing can populate it — see the docstring.
        "modified": [],
        "removed": removed,
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


def coverage_payload(
    chains: Sequence[Chain],
    capabilities: Mapping[str, frozenset[str]],
    generated_at_ms: int,
) -> dict[str, Any]:
    """Project the per-capability coverage matrix (SPEC §10, rule #10).

    Returns::

        {"generated_at_ms": generated_at_ms,
         "capabilities": list(CAPABILITY_NAMES),
         "chains": [{"chain_id", "name", "family", "native_asset",
                     "native_symbol", "native_decimals",
                     "capabilities": {name: bool, ...}}, ...]}

    Rows are sorted by ``chain_id`` ascending regardless of the input
    order. Each row's ``capabilities`` dict has EXACTLY the five
    ``CAPABILITY_NAMES`` keys, each flag being that name's membership of
    the chain's binding as normalised by :func:`_bound_capabilities`.

    The flags come ONLY from what the host bound into ``Deps``. There is
    no hardcoded family table and no prose (SPEC §12 risk 6: *"Docs lie
    — including your own. Generate the coverage matrix from live
    capability checks, never from prose."*). An unbound chain therefore
    reports all five ``False`` — an honest under-claim, which is the
    entire point of rule #10; an invented ``True`` is the failure mode
    being designed out, including for a MALFORMED binding (see
    :func:`_bound_capabilities`). Binding a name outside
    ``CAPABILITY_NAMES`` changes nothing: the row reports the five, and
    only the five.

    Pure: no I/O, never raises.
    """
    return {
        "generated_at_ms": generated_at_ms,
        "capabilities": list(CAPABILITY_NAMES),
        "chains": [
            {
                "chain_id": chain.caip2,
                "name": chain.name,
                "family": chain.family.value,
                "native_asset": chain.native_caip19,
                "native_symbol": chain.native_symbol,
                "native_decimals": chain.native_decimals,
                "capabilities": _capability_flags(
                    _bound_capabilities(capabilities.get(chain.caip2))
                ),
            }
            for chain in sorted(chains, key=lambda chain: chain.caip2)
        ],
    }


def _bound_capabilities(binding: object) -> frozenset[str]:
    """The capability names a host bound for one chain, normalised.

    Only a *collection of names* can grant a flag. A ``str`` (or
    ``bytes``) binding is treated as NO binding, because ``name in
    "no xpub support here"`` is SUBSTRING membership and would report
    ``xpub: True`` for a value asserting the exact opposite — the
    invented ``True`` that rule #10 and SPEC §12 risk 6 exist to
    prevent. Any binding that is not iterable, and any non-string member
    inside one, is dropped for the same reason: the honest answer to a
    malformed binding is five ``False``, never a hit.

    Never raises — a host's typing mistake must not 500 a coverage
    request, it must under-claim.
    """
    if binding is None or isinstance(binding, (str, bytes, bytearray)):
        return frozenset()
    if isinstance(binding, Mapping):
        # A Mapping carries its verdict in the VALUES, and `list(mapping)`
        # would throw them away and keep the keys — so a host binding this
        # endpoint's own output shape ({"balances": True, "xpub": False})
        # would report all five True, inverting an explicit deny into an
        # invented claim. Honour the values; anything not exactly True is
        # not a grant.
        return frozenset(
            name
            for name, granted in binding.items()
            if isinstance(name, str) and granted is True
        )
    try:
        members = list(binding)  # type: ignore[call-overload]
    except TypeError:
        return frozenset()
    return frozenset(name for name in members if isinstance(name, str))


def _capability_flags(bound: frozenset[str]) -> dict[str, bool]:
    """Exactly the five ``CAPABILITY_NAMES`` keys, each a real ``bool``."""
    return {name: name in bound for name in CAPABILITY_NAMES}


def batch_result(chain: str, address: str, result: Any) -> dict[str, Any]:
    """One successful batch item (SPEC §7.3, Allium partial success).

    ``{"status": "ok", "chain": chain, "address": address, "result":
    result}`` — four keys, no ``"error"`` key. ``chain`` and ``address``
    are echoed VERBATIM from the request so a caller can zip the
    response back onto its own input without reparsing.

    Pure: no I/O, never raises.
    """
    return {
        "status": "ok",
        "chain": chain,
        "address": address,
        "result": result,
    }


def batch_error(chain: str, address: str, exc: Exception) -> dict[str, Any]:
    """One failed batch item — one bad address never fails the request.

    ``{"status": "error", "chain": chain, "address": address, "error":
    {"type": type(exc).__name__, "message": str(exc)}}`` — four keys, no
    ``"result"`` key, so ``result``/``error`` are mutually exclusive and
    a client can branch on presence alone. ``chain`` and ``address`` are
    echoed verbatim.

    Pure: no I/O, never raises — the exception is DATA here, already
    caught by the caller.
    """
    return {
        "status": "error",
        "chain": chain,
        "address": address,
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }


def batch_warning(
    code: str,
    message: str,
    chain: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    """One entry for the batch ``warnings[]`` array (SPEC §7.3).

    ``{"code", "message", "chain", "address"}`` — exactly four keys,
    always present. ``chain``/``address`` are JSON ``null`` for a
    request-scoped warning, never omitted.

    Pure: no I/O, never raises.
    """
    return {
        "code": code,
        "message": message,
        "chain": chain,
        "address": address,
    }


def batch_envelope(
    items: Sequence[Mapping[str, Any]],
    warnings: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Wrap batch items and warnings: EXACTLY ``{"items", "warnings"}``.

    Both values are fresh ``list`` objects in the given order — items
    stay the same length and order as the request (SPEC §7.3), so index
    ``i`` of the response always answers index ``i`` of the request.

    Pure: no I/O, never raises.
    """
    return {"items": list(items), "warnings": list(warnings)}


def holdings_wire(report: Any) -> dict[str, Any]:
    """Project a holdings report to its HTTP body — DUCK-TYPED.

    ``report`` is anything exposing ``address``, ``chain_id``,
    ``as_of_ms``, ``total_value`` (Money-shaped), ``unpriced`` (iterable
    of CAIP-19 strings) and ``holdings`` (iterable of objects exposing
    ``caip19``, ``symbol``, ``quantity``, ``price``, ``value``).
    ``auradefi.portfolio.models.HoldingsReport`` satisfies it, but is
    deliberately NOT imported — ``portfolio`` is absent from ``api``'s
    row in the layering gate. No ``isinstance`` check exists here.

    Returns::

        {"address", "chain_id", "as_of_ms",
         "total_value": money_to_wire(report.total_value),
         "unpriced": list(report.unpriced),
         "holdings": [{"asset_id": h.caip19, "symbol": h.symbol,
                       "quantity": quantity_to_wire(h.quantity),
                       "price": _money_or_null(h.price),
                       "value": _money_or_null(h.value)}, ...]}

    ``_money_or_null`` branches on ``is None``, deliberately NOT on
    truthiness: a zero-amount Money is a real price and serialises as
    ``{"amount": "0", ...}``. Only an ABSENT price becomes ``null``.

    Holdings keep the report's order. An unpriced holding carries
    ``price``/``value`` of ``None`` — present, never omitted. NO
    rounding, and no float outside ``quantity['float']``, the one
    documented-lossy display field.

    Pure: no I/O, never raises.
    """
    return {
        "address": report.address,
        "chain_id": report.chain_id,
        "as_of_ms": report.as_of_ms,
        "total_value": money_to_wire(report.total_value),
        "unpriced": list(report.unpriced),
        "holdings": [
            {
                "asset_id": holding.caip19,
                "symbol": holding.symbol,
                "quantity": quantity_to_wire(holding.quantity),
                "price": _money_or_null(holding.price),
                "value": _money_or_null(holding.value),
            }
            for holding in report.holdings
        ],
    }


def _money_or_null(money: Any) -> dict[str, Any] | None:
    """``money_to_wire(money)``, or an explicit ``None`` for no amount."""
    return None if money is None else money_to_wire(money)
