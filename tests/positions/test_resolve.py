"""Contract tests for auradefi.positions.resolve (SPEC §5.4, §5.3).

"A resolver that raises is caught, logged, and drops only its own
slice" — SPEC §5.4 verbatim. Failures are typed data
(``AdapterFailure``), never a dead batch. The §5.3 raw/valued split is
enforced at this seam: resolve output is RAW, so an adapter returning a
pre-valued underlying is converted to a ``ValidationError`` recorded as
that adapter's own failure while its siblings survive.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from auradefi.errors import SourceError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.positions.models import (
    MetaType,
    Position,
    PositionKind,
    PositionType,
    ProtocolModule,
    Underlying,
    group_id_for,
    position_id,
)
from auradefi.positions.protocol import (
    ContractDescriptor,
    ContractSet,
    ResolveContext,
)
from auradefi.positions.resolve import (
    AdapterFailure,
    ResolveOutcome,
    resolve_all,
)

CHAIN = "eip155:1"
USER = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
AAVE_POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
COMPOUND_COMET = "0xc3d688b66703497daa19211eedff47f25384cdc3"
UNIV2_PAIR = "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"
WETH = "eip155:1/erc20:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


class NullReader:
    """Satisfies ContractReader structurally; explodes if actually read."""

    def call(self, address, fn, args=()):
        raise AssertionError("resolve_all must never read the chain itself")


def make_ctx() -> ResolveContext:
    return ResolveContext(chain_id=CHAIN, address=USER, reader=NullReader())


def raw_position(
    adapter_id: str, contract: str, *underlyings: Underlying
) -> Position:
    if not underlyings:
        underlyings = (
            Underlying(WETH, Quantity(2 * 10**18, 18), MetaType.SUPPLIED),
        )
    return Position(
        id=position_id(adapter_id, CHAIN, contract),
        adapter_id=adapter_id,
        chain_id=CHAIN,
        contract_address=contract,
        kind=PositionKind.CONTRACT_POSITION,
        position_type=PositionType.DEPOSIT,
        protocol_module=ProtocolModule.LENDING,
        group_id=group_id_for(adapter_id, CHAIN, contract),
        underlyings=tuple(underlyings),
    )


class StubAdapter:
    """Scripted PositionAdapter: returns, or raises, and records calls."""

    def __init__(self, adapter_id, chains=(CHAIN,), result=(), exc=None, log=None):
        self.id = adapter_id
        self.chains = frozenset(chains)
        self._result = list(result)
        self._exc = exc
        self._log = log
        self.seen_contracts: list[ContractSet] = []

    def discover(self, ctx):
        raise AssertionError("resolve_all must never call discover()")

    def resolve(self, ctx, contracts):
        self.seen_contracts.append(contracts)
        if self._log is not None:
            self._log.append(self.id)
        if self._exc is not None:
            raise self._exc
        return list(self._result)


POS_AAVE = raw_position("aave-v3", AAVE_POOL)
POS_UNI = raw_position("uniswap-v2", UNIV2_PAIR)

PREVALUED = Underlying(
    USDC,
    Quantity(5_000_000_000, 6),
    MetaType.SUPPLIED,
    price=Money(Decimal("0.999839"), "USD"),
    value=Money(Decimal("4999.195"), "USD"),
)


class TestFailureIsolation:
    def test_middle_raiser_drops_only_its_own_slice(self):
        # SPEC §5.4: the other two adapters' slices survive intact.
        good_a = StubAdapter("aave-v3", result=[POS_AAVE])
        bad = StubAdapter("compound-v3", exc=SourceError("rpc exploded"))
        good_c = StubAdapter("uniswap-v2", result=[POS_UNI])
        outcome = resolve_all([good_c, bad, good_a], make_ctx(), {})
        assert outcome.positions == (POS_AAVE, POS_UNI)
        assert len(outcome.failures) == 1
        assert outcome.failures[0].adapter_id == "compound-v3"
        assert "SourceError" in outcome.failures[0].error

    def test_failure_error_is_the_exception_repr(self):
        bad = StubAdapter("compound-v3", exc=SourceError("rpc exploded"))
        outcome = resolve_all([bad], make_ctx(), {})
        assert outcome.failures == (
            AdapterFailure("compound-v3", "SourceError('rpc exploded')"),
        )

    def test_prevalued_underlying_becomes_a_validation_failure(self):
        # §5.3: resolve output is RAW — pricing belongs to drill(), purely.
        good_a = StubAdapter("aave-v3", result=[POS_AAVE])
        leaky = StubAdapter(
            "compound-v3", result=[raw_position("compound-v3", COMPOUND_COMET, PREVALUED)]
        )
        good_c = StubAdapter("uniswap-v2", result=[POS_UNI])
        outcome = resolve_all([leaky, good_c, good_a], make_ctx(), {})
        assert outcome.positions == (POS_AAVE, POS_UNI)
        assert len(outcome.failures) == 1
        assert outcome.failures[0].adapter_id == "compound-v3"
        assert "ValidationError" in outcome.failures[0].error

    def test_all_adapters_failing_yields_only_failures(self):
        bad_a = StubAdapter("aave-v3", exc=SourceError("a"))
        bad_b = StubAdapter("compound-v3", exc=RuntimeError("b"))
        outcome = resolve_all([bad_b, bad_a], make_ctx(), {})
        assert outcome.positions == ()
        assert [f.adapter_id for f in outcome.failures] == [
            "aave-v3",
            "compound-v3",
        ]


class TestDispatch:
    def test_adapters_run_in_id_sorted_order(self):
        log: list[str] = []
        adapters = [
            StubAdapter("uniswap-v2", result=[POS_UNI], log=log),
            StubAdapter("compound-v3", log=log),
            StubAdapter("aave-v3", result=[POS_AAVE], log=log),
        ]
        outcome = resolve_all(adapters, make_ctx(), {})
        assert log == ["aave-v3", "compound-v3", "uniswap-v2"]
        assert outcome.positions == (POS_AAVE, POS_UNI)

    def test_adapter_without_the_chain_is_skipped_silently(self):
        polygon_only = StubAdapter("aave-v3", chains=("eip155:137",))
        mainnet = StubAdapter("uniswap-v2", result=[POS_UNI])
        outcome = resolve_all([polygon_only, mainnet], make_ctx(), {})
        assert polygon_only.seen_contracts == []  # never called
        assert outcome.positions == (POS_UNI,)
        assert outcome.failures == ()

    def test_absent_adapter_id_gets_an_empty_contract_set(self):
        # SPEC §5.4: ContractSet arrives partially populated OR EMPTY.
        adapter = StubAdapter("aave-v3")
        resolve_all([adapter], make_ctx(), {})
        assert len(adapter.seen_contracts) == 1
        received = adapter.seen_contracts[0]
        assert isinstance(received, ContractSet)
        assert len(received) == 0

    def test_present_adapter_receives_exactly_its_own_set(self):
        descriptor = ContractDescriptor(
            adapter_id="aave-v3", chain_id=CHAIN, address=AAVE_POOL, category="pool"
        )
        own_set = ContractSet.of(descriptor)
        aave = StubAdapter("aave-v3")
        uni = StubAdapter("uniswap-v2")
        resolve_all([aave, uni], make_ctx(), {"aave-v3": own_set})
        assert aave.seen_contracts == [own_set]
        assert uni.seen_contracts == [ContractSet.empty()]

    def test_no_adapters_yields_the_empty_outcome(self):
        assert resolve_all([], make_ctx(), {}) == ResolveOutcome((), ())

    def test_raw_positions_pass_through_by_identity(self):
        # resolve_all wraps and orders; it never rebuilds a position.
        adapter = StubAdapter("aave-v3", result=[POS_AAVE])
        outcome = resolve_all([adapter], make_ctx(), {})
        assert outcome.positions[0] is POS_AAVE


class TestShapes:
    def test_adapter_failure_is_frozen(self):
        failure = AdapterFailure("aave-v3", "SourceError('boom')")
        with pytest.raises(FrozenInstanceError):
            failure.error = "rewritten"

    def test_resolve_outcome_is_frozen(self):
        outcome = ResolveOutcome((), ())
        with pytest.raises(FrozenInstanceError):
            outcome.positions = (POS_AAVE,)

    def test_failure_fields(self):
        failure = AdapterFailure("aave-v3", "SourceError('boom')")
        assert failure.adapter_id == "aave-v3"
        assert failure.error == "SourceError('boom')"
