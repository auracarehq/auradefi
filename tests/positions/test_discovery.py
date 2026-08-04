"""Contract tests for auradefi.positions.discovery (SPEC §5.1, §5.2, §5.4).

``run_discovery`` is address-blind and per-chain; a failing adapter drops
only its own slice (``ContractSet.empty()`` + ``DiscoveryFailure`` carrying
``repr(exc)``); a foreign-adapter_id descriptor is a ``ValidationError``
recorded the same way and never leaks. The ``touched`` set is derived by
the CALLER and injected (SPEC §5.2 option 1); ``restrict_discovery``
preserves adapter keys whose sets become empty. Stubs are inline; nothing
imports positions.adapters.*.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from auradefi.positions.discovery import (
    DiscoveryFailure,
    DiscoveryOutcome,
    filter_by_interaction,
    restrict_discovery,
    run_discovery,
)
from auradefi.positions.protocol import (
    ContractDescriptor,
    ContractSet,
    DiscoveryContext,
)
from auradefi.positions.registry import AdapterRegistry

ETH = "eip155:1"
POLYGON = "eip155:137"
SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

AAVE_POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
UNIV2_PAIR = "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"
UNIV3_POOL = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"
CURVE_POOL = "0xbebc44782c7db0a1a60cb6fe97d0b483032ff1c7"


class DictReader:
    """Dict-backed fake — SPEC §5.4: satisfies ContractReader, no I/O."""

    def __init__(self, responses=None):
        self._responses = dict(responses or {})

    def call(self, address, fn, args=()):
        return self._responses[(address, fn, args)]


def descriptor(adapter_id: str, address: str, **overrides) -> ContractDescriptor:
    fields = {
        "adapter_id": adapter_id,
        "chain_id": ETH,
        "address": address,
        "category": "pool",
    }
    fields.update(overrides)
    return ContractDescriptor(**fields)


class StubAdapter:
    """Discover returns a canned ContractSet and logs the call."""

    def __init__(self, adapter_id, chains, result=None, exc=None, log=None):
        self.id = adapter_id
        self.chains = frozenset(chains)
        self._result = result if result is not None else ContractSet.empty()
        self._exc = exc
        self._log = log if log is not None else []

    def discover(self, ctx):
        self._log.append(self.id)
        if self._exc is not None:
            raise self._exc
        return self._result

    def resolve(self, ctx, contracts):
        raise NotImplementedError


def registry_of(*adapters) -> AdapterRegistry:
    registry = AdapterRegistry()
    for adapter in adapters:
        registry.register(adapter)
    return registry


def eth_ctx() -> DiscoveryContext:
    return DiscoveryContext(chain_id=ETH, reader=DictReader())


class TestDiscoveryFailure:
    def test_fields(self):
        failure = DiscoveryFailure(
            adapter_id="aave-v3", error="SourceError('explorer 500')"
        )
        assert failure.adapter_id == "aave-v3"
        assert failure.error == "SourceError('explorer 500')"

    def test_frozen(self):
        failure = DiscoveryFailure(adapter_id="aave-v3", error="boom")
        with pytest.raises(FrozenInstanceError):
            failure.error = "other"

    def test_value_equality(self):
        assert DiscoveryFailure("a", "e") == DiscoveryFailure("a", "e")
        assert DiscoveryFailure("a", "e") != DiscoveryFailure("b", "e")


class TestDiscoveryOutcome:
    def test_fields(self):
        contracts = {"aave-v3": ContractSet.empty()}
        failures = (DiscoveryFailure("aave-v3", "boom"),)
        outcome = DiscoveryOutcome(contracts=contracts, failures=failures)
        assert outcome.contracts == contracts
        assert outcome.failures == failures

    def test_frozen(self):
        outcome = DiscoveryOutcome(contracts={}, failures=())
        with pytest.raises(FrozenInstanceError):
            outcome.failures = (DiscoveryFailure("x", "y"),)


class TestRunDiscoveryHappyPath:
    def test_slices_keyed_by_adapter_id_no_failures(self):
        aave_set = ContractSet.of(descriptor("aave-v3", AAVE_POOL))
        uni_set = ContractSet.of(
            descriptor("uniswap-v2", UNIV2_PAIR, category="pair")
        )
        registry = registry_of(
            StubAdapter("aave-v3", {ETH}, result=aave_set),
            StubAdapter("uniswap-v2", {ETH}, result=uni_set),
        )
        outcome = run_discovery(registry, eth_ctx())
        assert outcome.contracts == {"aave-v3": aave_set, "uniswap-v2": uni_set}
        assert outcome.failures == ()

    def test_adapters_called_once_each_in_id_order(self):
        log: list[str] = []
        registry = registry_of(
            StubAdapter("uniswap-v2", {ETH}, log=log),
            StubAdapter("aave-v3", {ETH}, log=log),
            StubAdapter("curve", {ETH}, log=log),
        )
        run_discovery(registry, eth_ctx())
        assert log == ["aave-v3", "curve", "uniswap-v2"]

    def test_discover_receives_the_injected_ctx(self):
        seen = []

        class CtxSpy(StubAdapter):
            def discover(self, ctx):
                seen.append(ctx)
                return ContractSet.empty()

        ctx = eth_ctx()
        run_discovery(registry_of(CtxSpy("aave-v3", {ETH})), ctx)
        assert seen == [ctx]
        assert seen[0] is ctx

    def test_successful_empty_slice_is_present_without_failure(self):
        registry = registry_of(
            StubAdapter("aave-v3", {ETH}, result=ContractSet.empty())
        )
        outcome = run_discovery(registry, eth_ctx())
        assert outcome.contracts == {"aave-v3": ContractSet.empty()}
        assert outcome.failures == ()


class TestRunDiscoveryFailureIsolation:
    def test_middle_adapter_raising_source_error_drops_only_its_slice(self):
        from auradefi.errors import SourceError

        aave_set = ContractSet.of(descriptor("aave-v3", AAVE_POOL))
        uni_set = ContractSet.of(
            descriptor("uniswap-v2", UNIV2_PAIR, category="pair")
        )
        registry = registry_of(
            StubAdapter("aave-v3", {ETH}, result=aave_set),
            StubAdapter("curve", {ETH}, exc=SourceError("explorer 500")),
            StubAdapter("uniswap-v2", {ETH}, result=uni_set),
        )
        outcome = run_discovery(registry, eth_ctx())
        assert outcome.contracts["aave-v3"] == aave_set
        assert outcome.contracts["uniswap-v2"] == uni_set
        assert outcome.contracts["curve"] == ContractSet.empty()
        assert set(outcome.contracts) == {"aave-v3", "curve", "uniswap-v2"}
        # Golden: error is repr(exc), derived from python3 -c and hardcoded.
        assert outcome.failures == (
            DiscoveryFailure(
                adapter_id="curve", error="SourceError('explorer 500')"
            ),
        )
        assert "SourceError" in outcome.failures[0].error

    def test_any_exception_type_is_caught_not_propagated(self):
        registry = registry_of(
            StubAdapter("aave-v3", {ETH}, exc=KeyError("boom"))
        )
        outcome = run_discovery(registry, eth_ctx())  # must not raise
        assert outcome.contracts == {"aave-v3": ContractSet.empty()}
        assert outcome.failures == (
            DiscoveryFailure(adapter_id="aave-v3", error="KeyError('boom')"),
        )

    def test_two_failures_are_both_recorded_in_id_order(self):
        from auradefi.errors import SourceError

        registry = registry_of(
            StubAdapter("uniswap-v2", {ETH}, exc=SourceError("rate limited")),
            StubAdapter("aave-v3", {ETH}, exc=SourceError("explorer 500")),
        )
        outcome = run_discovery(registry, eth_ctx())
        assert outcome.failures == (
            DiscoveryFailure("aave-v3", "SourceError('explorer 500')"),
            DiscoveryFailure("uniswap-v2", "SourceError('rate limited')"),
        )


class TestRunDiscoveryChainFilter:
    def test_off_chain_adapters_absent_from_contracts_and_failures(self):
        log: list[str] = []
        registry = registry_of(
            StubAdapter(
                "aave-v3",
                {ETH},
                result=ContractSet.of(descriptor("aave-v3", AAVE_POOL)),
                log=log,
            ),
            StubAdapter("marinade", {SOLANA}, log=log),
            StubAdapter("quickswap", {POLYGON}, log=log),
        )
        outcome = run_discovery(registry, eth_ctx())
        assert set(outcome.contracts) == {"aave-v3"}
        assert outcome.failures == ()
        # Off-chain adapters were never even invoked (SPEC §5.1: per
        # adapter x chain).
        assert log == ["aave-v3"]

    def test_no_adapter_serves_the_chain_yields_an_empty_outcome(self):
        registry = registry_of(StubAdapter("marinade", {SOLANA}))
        outcome = run_discovery(registry, eth_ctx())
        assert outcome.contracts == {}
        assert outcome.failures == ()


class TestRunDiscoveryForeignDescriptor:
    def test_foreign_adapter_id_is_a_validation_failure_never_leaked(self):
        foreign = descriptor("evil-adapter", UNIV3_POOL)
        registry = registry_of(
            StubAdapter("uniswap-v2", {ETH}, result=ContractSet.of(foreign))
        )
        outcome = run_discovery(registry, eth_ctx())
        assert outcome.contracts == {"uniswap-v2": ContractSet.empty()}
        assert len(outcome.failures) == 1
        failure = outcome.failures[0]
        assert failure.adapter_id == "uniswap-v2"
        assert "ValidationError" in failure.error
        leaked = [
            d for s in outcome.contracts.values() for d in s
        ]
        assert foreign not in leaked

    def test_mixed_own_and_foreign_drops_the_whole_slice(self):
        own = descriptor("uniswap-v2", UNIV2_PAIR, category="pair")
        foreign = descriptor("aave-v3", AAVE_POOL)
        registry = registry_of(
            StubAdapter("uniswap-v2", {ETH}, result=ContractSet.of(own, foreign)),
            StubAdapter(
                "curve", {ETH}, result=ContractSet.of(descriptor("curve", CURVE_POOL))
            ),
        )
        outcome = run_discovery(registry, eth_ctx())
        # The offending adapter's slice is dropped in full; its neighbour
        # is untouched (drops only its own slice — SPEC §5.4).
        assert outcome.contracts["uniswap-v2"] == ContractSet.empty()
        assert outcome.contracts["curve"] == ContractSet.of(
            descriptor("curve", CURVE_POOL)
        )
        assert [f.adapter_id for f in outcome.failures] == ["uniswap-v2"]
        assert "ValidationError" in outcome.failures[0].error
        leaked = [d for s in outcome.contracts.values() for d in s]
        assert foreign not in leaked
        assert own not in leaked


class TestFilterByInteraction:
    def _contracts(self) -> ContractSet:
        return ContractSet.of(
            descriptor("uniswap-v2", UNIV2_PAIR, category="pair"),
            descriptor("uniswap-v2", UNIV3_POOL),
        )

    def test_equals_restrict_to(self):
        contracts = self._contracts()
        touched = frozenset({UNIV2_PAIR})
        assert filter_by_interaction(contracts, touched) == contracts.restrict_to(
            touched
        )

    def test_keeps_only_touched_addresses(self):
        kept = filter_by_interaction(self._contracts(), frozenset({UNIV2_PAIR}))
        assert kept == ContractSet.of(
            descriptor("uniswap-v2", UNIV2_PAIR, category="pair")
        )

    def test_mixed_case_touched_addresses_match(self):
        # Touched set comes straight from explorer history (SPEC §5.2
        # option 1) — checksummed casing must still match.
        kept = filter_by_interaction(
            self._contracts(),
            frozenset({"0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc"}),
        )
        assert kept == ContractSet.of(
            descriptor("uniswap-v2", UNIV2_PAIR, category="pair")
        )

    def test_empty_touched_empties_the_set(self):
        assert filter_by_interaction(
            self._contracts(), frozenset()
        ) == ContractSet.empty()


class TestRestrictDiscovery:
    def _by_adapter(self) -> dict[str, ContractSet]:
        return {
            "aave-v3": ContractSet.of(descriptor("aave-v3", AAVE_POOL)),
            "uniswap-v2": ContractSet.of(
                descriptor("uniswap-v2", UNIV2_PAIR, category="pair"),
                descriptor("uniswap-v2", UNIV3_POOL),
            ),
        }

    def test_applies_per_adapter_and_preserves_emptied_keys(self):
        restricted = restrict_discovery(
            self._by_adapter(), frozenset({UNIV2_PAIR})
        )
        # aave-v3's slice was emptied but the key MUST survive — §5.4:
        # resolve() receives 'partially populated or empty'.
        assert set(restricted) == {"aave-v3", "uniswap-v2"}
        assert restricted["aave-v3"] == ContractSet.empty()
        assert restricted["uniswap-v2"] == ContractSet.of(
            descriptor("uniswap-v2", UNIV2_PAIR, category="pair")
        )

    def test_empty_touched_keeps_every_key_mapped_to_empty(self):
        restricted = restrict_discovery(self._by_adapter(), frozenset())
        assert set(restricted) == {"aave-v3", "uniswap-v2"}
        assert all(s == ContractSet.empty() for s in restricted.values())

    def test_empty_mapping_yields_empty_dict(self):
        assert restrict_discovery({}, frozenset({AAVE_POOL})) == {}

    def test_input_mapping_is_not_mutated(self):
        by_adapter = self._by_adapter()
        original_uni = by_adapter["uniswap-v2"]
        restrict_discovery(by_adapter, frozenset())
        assert set(by_adapter) == {"aave-v3", "uniswap-v2"}
        assert by_adapter["uniswap-v2"] == original_uni
        assert len(by_adapter["uniswap-v2"]) == 2

    def test_returns_a_plain_dict(self):
        restricted = restrict_discovery(self._by_adapter(), frozenset())
        assert isinstance(restricted, dict)
