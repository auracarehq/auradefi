"""Resolver isolation (SPEC §5.4): one bad adapter drops only its slice.

"A resolver that raises is caught, logged, and drops only its own slice"
— SPEC §5.4, verbatim. :func:`resolve_all` fans a user refresh out over
every adapter and collects per-adapter failures instead of letting one
exception kill the batch (Zapper's bare ``catch { console.error(e) }``
inverted: failures are DATA, typed and returned).

The §5.3 raw/valued split is enforced HERE, at the seam: ``resolve()``
output is RAW — chain reads only, no pricing. An adapter that returns an
underlying carrying ``price`` or ``value`` is a defect, converted to a
``ValidationError`` inside that adapter's own guard so its slice drops
and its siblings survive. Pricing happens later, in ``drill()``, purely.

Layering: stdlib + ``auradefi.positions`` + ``auradefi.errors`` only.
No I/O — the only chain-read seam is the ``ContractReader`` inside
``ResolveContext``, and this module never calls it directly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from auradefi.errors import ValidationError
from auradefi.positions.models import Position
from auradefi.positions.protocol import (
    ContractSet,
    PositionAdapter,
    ResolveContext,
)


@dataclass(frozen=True, slots=True)
class AdapterFailure:
    """One adapter's failure, isolated to its own slice (SPEC §5.4).

    ``error`` is ``repr(exc)`` of the exception the adapter raised (or
    of the ``ValidationError`` minted when it returned pre-valued
    underlyings) — a string, so outcomes stay frozen and serialisable.
    """

    adapter_id: str
    error: str


@dataclass(frozen=True, slots=True)
class ResolveOutcome:
    """Everything a refresh produced: surviving slices plus failures.

    ``positions`` concatenates the surviving adapters' RAW positions in
    adapter-id-sorted order; ``failures`` carries one
    :class:`AdapterFailure` per adapter that raised or returned valued
    underlyings. Partial success is the contract, never all-or-nothing.
    """

    positions: tuple[Position, ...]
    failures: tuple[AdapterFailure, ...]


def resolve_all(
    adapters: Sequence[PositionAdapter],
    ctx: ResolveContext,
    contracts_by_adapter: Mapping[str, ContractSet],
) -> ResolveOutcome:
    """Run every applicable adapter's ``resolve()``, isolating failures.

    Contract (SPEC §5.4, §5.3):

    * adapters run in ``id``-sorted order — deterministic output;
    * an adapter whose ``chains`` lacks ``ctx.chain_id`` is skipped
      silently (no call, no failure);
    * each adapter receives ``contracts_by_adapter.get(adapter.id,
      ContractSet.empty())`` — a set that is partially populated or
      empty is normal (SPEC §5.4);
    * each ``resolve()`` call is wrapped in ``try/except Exception``;
      a raise becomes ``AdapterFailure(adapter.id, repr(exc))`` and
      drops only that adapter's slice;
    * RAW-output enforcement: any returned underlying whose ``price``
      or ``value`` is not ``None`` raises ``ValidationError`` INSIDE
      the per-adapter guard, so it is recorded as that adapter's
      failure and siblings survive.
    """
    positions: list[Position] = []
    failures: list[AdapterFailure] = []
    for adapter in sorted(adapters, key=lambda adapter: adapter.id):
        if ctx.chain_id not in adapter.chains:
            continue
        contracts = contracts_by_adapter.get(adapter.id, ContractSet.empty())
        try:
            resolved = adapter.resolve(ctx, contracts)
            _require_raw(resolved)
        except Exception as exc:  # isolation IS the contract (SPEC §5.4)
            failures.append(AdapterFailure(adapter.id, repr(exc)))
            continue
        positions.extend(resolved)
    return ResolveOutcome(tuple(positions), tuple(failures))


def _require_raw(positions: Sequence[Position]) -> None:
    """Raise ``ValidationError`` if any underlying carries a ``price``
    or ``value`` — resolve output is RAW; pricing belongs to ``drill()``
    (SPEC §5.3)."""
    for position in positions:
        for underlying in position.underlyings:
            if underlying.price is not None or underlying.value is not None:
                raise ValidationError(
                    f"adapter {position.adapter_id!r} returned a valued "
                    f"underlying for {underlying.asset_id!r} — resolve() "
                    "output must be raw (SPEC §5.3)"
                )
