"""Adapter registry — registration is explicit, not filename magic (SPEC §4.5).

Zapper's ``@PositionTemplate()`` decorator read the **call stack** to derive
identity from the file path and swallowed failures in a bare
``catch { console.error(e) }`` — a mis-named file silently produced a fetcher
with ``appId === undefined``. Here registration is declared in code and a
duplicate id is a loud ``ConflictError``, never a silent replace.

No default shipped set: ``AdapterRegistry()`` starts EMPTY. Assembling the
production adapter set is later wiring, which keeps this module independent
of every adapter order. Layering: stdlib + ``auradefi.positions.protocol``
+ ``auradefi.errors`` only.
"""

from __future__ import annotations

from auradefi.errors import ConflictError, NotFoundError
from auradefi.positions.protocol import PositionAdapter


class AdapterRegistry:
    """Mutable registry of position adapters, keyed strictly by ``adapter.id``
    (the DefiLlama protocol slug — the join key, SPEC §5.4).

    Instances are independent — registering into one never affects another.
    A fresh registry is empty; there is no default shipped set.
    """

    def __init__(self) -> None:
        """Create an EMPTY registry (assembly is later wiring)."""
        self._by_id: dict[str, PositionAdapter] = {}

    def register(self, adapter: PositionAdapter) -> None:
        """Register ``adapter`` under ``adapter.id``.

        Raises:
            ConflictError: if ``adapter.id`` is already registered — ANY
                duplicate id, even re-registering the identical object;
                never a silent replace (SPEC §4.5). Carries
                ``existing_id == adapter.id``. The existing registration
                is left untouched.
        """
        if adapter.id in self._by_id:
            raise ConflictError(
                f"adapter id {adapter.id!r} is already registered",
                existing_id=adapter.id,
            )
        self._by_id[adapter.id] = adapter

    def get(self, adapter_id: str) -> PositionAdapter:
        """Return the adapter registered under ``adapter_id``.

        Raises:
            NotFoundError: if ``adapter_id`` is not registered.
        """
        try:
            return self._by_id[adapter_id]
        except KeyError:
            raise NotFoundError(
                f"no adapter registered under {adapter_id!r}"
            ) from None

    def adapters(self) -> tuple[PositionAdapter, ...]:
        """All registered adapters as a tuple sorted by ``id``,
        regardless of registration order."""
        return tuple(
            self._by_id[adapter_id] for adapter_id in sorted(self._by_id)
        )

    def for_chain(self, chain_id: str) -> tuple[PositionAdapter, ...]:
        """The adapters with ``chain_id in adapter.chains``, sorted by
        ``id``. Unknown chains yield ``()`` — never an error."""
        return tuple(
            adapter
            for adapter in self.adapters()
            if chain_id in adapter.chains
        )
