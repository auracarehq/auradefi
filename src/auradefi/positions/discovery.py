"""Discovery phase: run adapters address-blind, per chain (SPEC §5.1, §5.2).

``run_discovery`` executes ``discover()`` for every registered adapter that
serves ``ctx.chain_id``, in id order. It does NOT know the address — that is
the whole point of the two-phase split. An adapter that raises ANY exception
is caught and drops only its own slice (SPEC §5.4): its key maps to
``ContractSet.empty()`` and a ``DiscoveryFailure`` records ``repr(exc)``.
A descriptor carrying a FOREIGN ``adapter_id`` or a ``chain_id`` other
than ``ctx.chain_id`` is a ``ValidationError`` recorded the same way —
the descriptor never leaks into the outcome.

The interaction pre-filter (SPEC §5.2, option 1): the ``touched`` set is
derived by the CALLER from explorer history and INJECTED here. This module
performs no I/O beyond the ``ctx.reader`` calls adapters make.
``restrict_discovery`` preserves adapter keys whose sets become empty —
``ContractSet`` arrives at ``resolve()`` partially populated or empty
(SPEC §5.4).

Layering: stdlib + ``auradefi.positions.protocol`` +
``auradefi.positions.registry`` + ``auradefi.errors`` only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from auradefi.errors import ValidationError
from auradefi.positions.protocol import ContractSet, DiscoveryContext
from auradefi.positions.registry import AdapterRegistry


@dataclass(frozen=True, slots=True)
class DiscoveryFailure:
    """One adapter's discovery failure: its id and ``repr(exc)``."""

    adapter_id: str
    error: str


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    """The result of one discovery run over one chain.

    ``contracts`` is keyed by adapter id and holds a key for EVERY adapter
    serving the chain — failed adapters map to ``ContractSet.empty()``.
    Adapters not serving ``ctx.chain_id`` appear nowhere, in neither
    ``contracts`` nor ``failures``.
    """

    contracts: dict[str, ContractSet]
    failures: tuple[DiscoveryFailure, ...]


def run_discovery(
    registry: AdapterRegistry, ctx: DiscoveryContext
) -> DiscoveryOutcome:
    """Run ``discover(ctx)`` for each of ``registry.for_chain(ctx.chain_id)``
    in id order and collect the slices.

    An adapter raising ANY ``Exception`` contributes
    ``ContractSet.empty()`` plus ``DiscoveryFailure(id, repr(exc))`` and
    drops only its own slice. A returned descriptor whose ``adapter_id``
    is not the adapter's own, or whose ``chain_id`` is not
    ``ctx.chain_id`` (one discovery run covers exactly one chain — the
    same address on another chain would later resolve against the wrong
    reader), is a ``ValidationError`` recorded as that adapter's
    ``DiscoveryFailure``; the entire returned slice is dropped to
    ``ContractSet.empty()`` and the foreign descriptor never leaks.
    A return that is not a ``ContractSet`` (e.g. a bare list of
    descriptors) is the same ``ValidationError`` path — ``contracts``
    holds ``ContractSet`` values only, so downstream ``restrict_to``
    never crashes on a mis-typed slice. ``run_discovery`` itself never
    raises for adapter misbehaviour.
    """
    contracts: dict[str, ContractSet] = {}
    failures: list[DiscoveryFailure] = []
    for adapter in registry.for_chain(ctx.chain_id):
        try:
            slice_ = _require_contract_set(adapter.id, adapter.discover(ctx))
            _reject_foreign_descriptors(adapter.id, ctx.chain_id, slice_)
        except Exception as exc:
            contracts[adapter.id] = ContractSet.empty()
            failures.append(
                DiscoveryFailure(adapter_id=adapter.id, error=repr(exc))
            )
        else:
            contracts[adapter.id] = slice_
    return DiscoveryOutcome(contracts=contracts, failures=tuple(failures))


def _require_contract_set(adapter_id: str, slice_: object) -> ContractSet:
    """Raise ``ValidationError`` unless ``slice_`` is a ``ContractSet`` —
    a mis-typed return must fail scoped, not crash downstream callers."""
    if not isinstance(slice_, ContractSet):
        raise ValidationError(
            f"adapter {adapter_id!r} returned "
            f"{type(slice_).__name__!r}, not ContractSet"
        )
    return slice_


def _reject_foreign_descriptors(
    adapter_id: str, chain_id: str, contracts: ContractSet
) -> None:
    """Raise ``ValidationError`` if any descriptor's ``adapter_id`` is
    not ``adapter_id`` or its ``chain_id`` is not ``chain_id`` — a
    foreign descriptor must never leak into a per-chain outcome."""
    for descriptor in contracts:
        if descriptor.adapter_id != adapter_id:
            raise ValidationError(
                f"adapter {adapter_id!r} returned a descriptor for "
                f"{descriptor.adapter_id!r} at {descriptor.address!r}"
            )
        if descriptor.chain_id != chain_id:
            raise ValidationError(
                f"adapter {adapter_id!r} returned a descriptor for chain "
                f"{descriptor.chain_id!r} at {descriptor.address!r} during "
                f"a {chain_id!r} run"
            )


def filter_by_interaction(
    contracts: ContractSet, touched: frozenset[str]
) -> ContractSet:
    """The SPEC §5.2 pre-filter: exactly
    ``contracts.restrict_to(touched)`` — keep only descriptors whose
    address the user actually touched (case-insensitive for 0x)."""
    return contracts.restrict_to(touched)


def restrict_discovery(
    by_adapter: Mapping[str, ContractSet], touched: frozenset[str]
) -> dict[str, ContractSet]:
    """Apply :func:`filter_by_interaction` per adapter, PRESERVING keys —
    an adapter whose slice becomes empty stays present, mapped to the
    empty set (SPEC §5.4: 'partially populated or empty'). ``by_adapter``
    is never mutated."""
    return {
        adapter_id: filter_by_interaction(contracts, touched)
        for adapter_id, contracts in by_adapter.items()
    }
