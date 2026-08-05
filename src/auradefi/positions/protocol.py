"""The adapter contract (SPEC §5.4) and its chain-read seam (Phase 4).

``ContractReader`` is the ONLY chain-read abstraction in ``positions/``.
This domain is not in the layering gate's IO_DOMAINS, so no HTTP
client may ever appear here; a dict-backed fake satisfies the protocol
and the whole domain stays fixture-driven and offline.

The two-phase split (SPEC §5.1, LlamaFolio): ``discover()`` runs
address-blind on a background cron and emits static
``ContractDescriptor`` rows; ``resolve()`` runs per user refresh and
attaches amounts. ``ContractSet`` arrives at ``resolve()`` **partially
populated or empty**. That is the whole point of pre-filtering
(SPEC §5.2): ``restrict_to`` keeps only descriptors whose address the
user actually touched.

Layering: stdlib + ``auradefi.positions.models`` + ``auradefi.errors``.
0x addresses are lowercased at construction (DECISIONS.md).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from auradefi.positions.models import Position


@runtime_checkable
class ContractReader(Protocol):
    """A single read against a deployed contract.

    The only chain-read seam in ``positions/``: adapters call
    ``reader.call(address, fn, args)`` and never open a socket
    themselves. Dict-backed fakes satisfy this protocol in tests.
    """

    def call(
        self, address: str, fn: str, args: tuple[object, ...] = ()
    ) -> object:
        """Return the decoded result of ``fn(*args)`` at ``address``."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class DiscoveryContext:
    """What ``discover()`` gets. It does NOT know the address (SPEC §5.1)."""

    chain_id: str
    reader: ContractReader


@dataclass(frozen=True, slots=True)
class ResolveContext:
    """What ``resolve()`` gets: the user's address plus a reader.

    A 0x ``address`` is lowercased in ``__post_init__``; non-0x
    addresses (e.g. Solana base58) keep their case.
    """

    chain_id: str
    address: str
    reader: ContractReader
    block_number: int | None = None

    def __post_init__(self) -> None:
        """Lowercase a 0x ``address``; leave others untouched."""
        if self.address.startswith("0x"):
            object.__setattr__(self, "address", self.address.lower())


@dataclass(frozen=True, slots=True)
class ContractDescriptor:
    """One static contract a user could hold a position at (SPEC §5.1).

    Hashable and frozen. Descriptor sets are deduplicated and
    persisted between discovery runs. A 0x ``address`` is lowercased at
    construction; ``meta`` is a tuple of (key, value) pairs so the
    descriptor stays hashable.
    """

    adapter_id: str
    chain_id: str
    address: str
    category: str
    underlyings: tuple[str, ...] = ()
    meta: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        """Lowercase a 0x ``address``; leave others untouched."""
        if self.address.startswith("0x"):
            object.__setattr__(self, "address", self.address.lower())


@dataclass(frozen=True, slots=True)
class ContractSet:
    """A deduplicated, deterministically ordered set of descriptors.

    Holds a tuple sorted by ``(adapter_id, chain_id, address,
    category)``. Build via :meth:`empty` or :meth:`of`, never by hand.
    Arrives at ``resolve()`` partially populated or empty (SPEC §5.4).
    """

    descriptors: tuple[ContractDescriptor, ...] = ()

    @classmethod
    def empty(cls) -> ContractSet:
        """The empty set."""
        return cls()

    @classmethod
    def of(cls, *descriptors: ContractDescriptor) -> ContractSet:
        """Deduplicate and sort ``descriptors`` into a set."""
        ordered = sorted(
            set(descriptors),
            key=lambda d: (d.adapter_id, d.chain_id, d.address, d.category),
        )
        return cls(tuple(ordered))

    def __iter__(self) -> Iterator[ContractDescriptor]:
        """Iterate descriptors in sorted order."""
        return iter(self.descriptors)

    def __len__(self) -> int:
        """Number of distinct descriptors."""
        return len(self.descriptors)

    def __bool__(self) -> bool:
        """True iff non-empty."""
        return bool(self.descriptors)

    def restrict_to(self, touched: frozenset[str]) -> ContractSet:
        """Keep descriptors whose ``address`` is in
        ``{t.lower() for t in touched}``: the SPEC §5.2 interaction
        pre-filter (only contracts the user actually touched run)."""
        lowered = {address.lower() for address in touched}
        kept = tuple(d for d in self.descriptors if d.address in lowered)
        return ContractSet(kept)


@runtime_checkable
class PositionAdapter(Protocol):
    """One protocol integration (SPEC §5.4, verbatim).

    ``id`` is the DefiLlama protocol slug: the join key. A resolver
    that raises is caught, logged, and drops only its own slice.
    """

    id: str
    chains: frozenset[str]

    def discover(self, ctx: DiscoveryContext) -> ContractSet:
        """Address-blind enumeration of static contract descriptors."""
        raise NotImplementedError

    def resolve(
        self, ctx: ResolveContext, contracts: ContractSet
    ) -> list[Position]:
        """Attach amounts for ``ctx.address`` to ``contracts``."""
        raise NotImplementedError
