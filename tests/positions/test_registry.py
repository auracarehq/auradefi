"""Contract tests for auradefi.positions.registry (SPEC §4.5).

Registration is explicit, not filename magic. Zapper's call-stack
``@PositionTemplate()`` decorator is the named casualty. A duplicate id is
a loud ``ConflictError`` carrying ``existing_id``, never a silent replace.
A fresh registry is EMPTY: no default shipped set (assembly is later
wiring). Stubs are inline; nothing imports positions.adapters.*.
"""

from __future__ import annotations

import pytest

from auradefi.errors import ConflictError, NotFoundError
from auradefi.positions.registry import AdapterRegistry

ETH = "eip155:1"
POLYGON = "eip155:137"
BASE = "eip155:8453"
SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"


class StubAdapter:
    """Inline stub satisfying the PositionAdapter protocol (SPEC §5.4)."""

    def __init__(self, adapter_id: str, *chains: str) -> None:
        self.id = adapter_id
        self.chains = frozenset(chains) if chains else frozenset({ETH})

    def discover(self, ctx):
        raise NotImplementedError

    def resolve(self, ctx, contracts):
        raise NotImplementedError


class TestFreshRegistryIsEmpty:
    def test_no_default_shipped_set(self):
        # Assembly is later wiring. This order stays adapter-independent.
        assert AdapterRegistry().adapters() == ()

    def test_for_chain_on_fresh_registry_is_empty(self):
        assert AdapterRegistry().for_chain(ETH) == ()

    def test_instances_are_independent(self):
        first = AdapterRegistry()
        second = AdapterRegistry()
        first.register(StubAdapter("aave-v3"))
        assert second.adapters() == ()
        with pytest.raises(NotFoundError):
            second.get("aave-v3")


class TestRegisterAndGet:
    def test_get_returns_the_registered_object_itself(self):
        registry = AdapterRegistry()
        adapter = StubAdapter("uniswap-v2", ETH, POLYGON)
        registry.register(adapter)
        assert registry.get("uniswap-v2") is adapter

    def test_duplicate_id_raises_conflict_with_existing_id(self):
        registry = AdapterRegistry()
        registry.register(StubAdapter("uniswap-v2", ETH))
        with pytest.raises(ConflictError) as excinfo:
            registry.register(StubAdapter("uniswap-v2", POLYGON))
        assert excinfo.value.existing_id == "uniswap-v2"

    def test_re_registering_the_identical_object_still_raises(self):
        # "Never silent replace" has no idempotence carve-out (SPEC §4.5).
        registry = AdapterRegistry()
        adapter = StubAdapter("aave-v3", ETH)
        registry.register(adapter)
        with pytest.raises(ConflictError) as excinfo:
            registry.register(adapter)
        assert excinfo.value.existing_id == "aave-v3"

    def test_failed_duplicate_leaves_the_original_registration_intact(self):
        registry = AdapterRegistry()
        original = StubAdapter("uniswap-v2", ETH)
        registry.register(original)
        with pytest.raises(ConflictError):
            registry.register(StubAdapter("uniswap-v2", SOLANA))
        assert registry.get("uniswap-v2") is original
        assert registry.adapters() == (original,)

    def test_conflict_error_is_an_auradefi_error_carrying_a_message(self):
        registry = AdapterRegistry()
        registry.register(StubAdapter("curve"))
        with pytest.raises(ConflictError) as excinfo:
            registry.register(StubAdapter("curve"))
        assert "curve" in str(excinfo.value)

    def test_get_unknown_id_raises_not_found(self):
        registry = AdapterRegistry()
        registry.register(StubAdapter("uniswap-v2"))
        with pytest.raises(NotFoundError):
            registry.get("nope")

    def test_get_is_exact_match_never_slug_fuzzing(self):
        registry = AdapterRegistry()
        registry.register(StubAdapter("uniswap-v2"))
        with pytest.raises(NotFoundError):
            registry.get("Uniswap-V2")


class TestAdaptersOrdering:
    def test_sorted_by_id_regardless_of_registration_order(self):
        registry = AdapterRegistry()
        uniswap = StubAdapter("uniswap-v2")
        aave = StubAdapter("aave-v3")
        curve = StubAdapter("curve")
        registry.register(uniswap)
        registry.register(curve)
        registry.register(aave)
        assert registry.adapters() == (aave, curve, uniswap)

    def test_reversed_registration_gives_the_same_tuple(self):
        forward = AdapterRegistry()
        backward = AdapterRegistry()
        ids = ["marinade", "aave-v3", "uniswap-v3", "compound-v2"]
        for adapter_id in ids:
            forward.register(StubAdapter(adapter_id))
        for adapter_id in reversed(ids):
            backward.register(StubAdapter(adapter_id))
        assert [a.id for a in forward.adapters()] == [
            "aave-v3",
            "compound-v2",
            "marinade",
            "uniswap-v3",
        ]
        assert [a.id for a in backward.adapters()] == [
            a.id for a in forward.adapters()
        ]

    def test_adapters_returns_a_tuple(self):
        registry = AdapterRegistry()
        registry.register(StubAdapter("aave-v3"))
        assert isinstance(registry.adapters(), tuple)


class TestForChain:
    def _registry(self):
        registry = AdapterRegistry()
        self.uniswap = StubAdapter("uniswap-v2", ETH, POLYGON)
        self.aave = StubAdapter("aave-v3", ETH, POLYGON, BASE)
        self.marinade = StubAdapter("marinade", SOLANA)
        registry.register(self.uniswap)
        registry.register(self.marinade)
        registry.register(self.aave)
        return registry

    def test_filters_on_chain_membership_and_sorts_by_id(self):
        registry = self._registry()
        assert registry.for_chain(ETH) == (self.aave, self.uniswap)
        assert registry.for_chain(SOLANA) == (self.marinade,)

    def test_multi_chain_adapter_appears_for_each_of_its_chains(self):
        registry = self._registry()
        assert registry.for_chain(POLYGON) == (self.aave, self.uniswap)
        assert registry.for_chain(BASE) == (self.aave,)

    def test_unknown_chain_yields_empty_tuple_not_an_error(self):
        registry = self._registry()
        assert registry.for_chain("eip155:42161") == ()

    def test_membership_is_exact_chain_id_never_family_prefix(self):
        registry = AdapterRegistry()
        registry.register(StubAdapter("aave-v3", ETH))
        assert registry.for_chain("eip155:10") == ()
