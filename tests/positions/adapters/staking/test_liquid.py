"""Contract tests for the Lido + Rocket Pool liquid-staking adapters
(SPEC §5.4, §4.3; rule #5's golden fixtures live in
tests/golden/test_positions_liquid_staking.py, pinned to block
20_450_000).

The point these tests prove is the fork economics: each subclass body is
class attributes ONLY, zero methods, at most 15 source lines, exactly
Zapper's production Uniswap V2 shape ("15 lines and zero methods",
SPEC §5.4). All behaviour is inherited from ``ReceiptTokenAdapter``.
"""

from __future__ import annotations

import inspect

import pytest

from auradefi.positions.adapters.staking.liquid import (
    LidoAdapter,
    RocketPoolAdapter,
)
from auradefi.positions.adapters.tokens import ReceiptToken, ReceiptTokenAdapter
from auradefi.positions.models import PositionType, ProtocolModule
from auradefi.positions.protocol import PositionAdapter

ETH = "eip155:1/slip44:60"
STETH = "0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
RETH = "0xae78736cd615f374d3085123a210448e74fc6393"

ADAPTERS = [LidoAdapter, RocketPoolAdapter]


class TestLidoDeclaration:
    def test_id_is_the_defillama_slug(self):
        assert LidoAdapter.id == "lido"

    def test_chains(self):
        assert LidoAdapter.chains == frozenset({"eip155:1"})

    def test_receipts_exactly_steth_rebasing(self):
        # stETH rebases 1:1 with ETH -> identity rate, rate_fn None.
        assert set(LidoAdapter.receipts) == {"eip155:1"}
        assert LidoAdapter.receipts["eip155:1"] == (
            ReceiptToken(STETH, ETH, 18, None),
        )

    def test_no_exchange_rate_function(self):
        (receipt,) = LidoAdapter.receipts["eip155:1"]
        assert receipt.rate_fn is None


class TestRocketPoolDeclaration:
    def test_id_is_the_defillama_slug(self):
        assert RocketPoolAdapter.id == "rocket-pool"

    def test_chains(self):
        assert RocketPoolAdapter.chains == frozenset({"eip155:1"})

    def test_receipts_exactly_reth_with_exchange_rate(self):
        assert set(RocketPoolAdapter.receipts) == {"eip155:1"}
        assert RocketPoolAdapter.receipts["eip155:1"] == (
            ReceiptToken(RETH, ETH, 18, "getExchangeRate"),
        )


class TestForkEconomics:
    """SPEC §5.4: the subclass is data; the fork helper is the code."""

    @pytest.mark.parametrize("cls", ADAPTERS)
    def test_subclasses_the_fork_helper_base(self, cls):
        assert issubclass(cls, ReceiptTokenAdapter)

    @pytest.mark.parametrize("cls", ADAPTERS)
    def test_instance_satisfies_position_adapter_protocol(self, cls):
        assert isinstance(cls(), PositionAdapter)

    @pytest.mark.parametrize("cls", ADAPTERS)
    def test_body_is_class_attributes_only_no_methods(self, cls):
        defined = {
            name: value
            for name, value in vars(cls).items()
            if not name.startswith("__")
        }
        for name, value in defined.items():
            assert not callable(value), f"{cls.__name__}.{name} is a method"
            assert not isinstance(value, (staticmethod, classmethod, property)), (
                f"{cls.__name__}.{name} is a descriptor-wrapped method"
            )

    @pytest.mark.parametrize("cls", ADAPTERS)
    def test_discover_and_resolve_are_inherited_not_redefined(self, cls):
        assert "discover" not in vars(cls)
        assert "resolve" not in vars(cls)
        assert cls.discover is ReceiptTokenAdapter.discover
        assert cls.resolve is ReceiptTokenAdapter.resolve

    @pytest.mark.parametrize("cls", ADAPTERS)
    def test_body_is_at_most_15_source_lines(self, cls):
        source_lines = inspect.getsource(cls).rstrip().splitlines()
        assert len(source_lines) <= 15, (
            f"{cls.__name__} is {len(source_lines)} lines: the whole point "
            "is a 15-line, zero-method integration (SPEC §5.4)"
        )

    @pytest.mark.parametrize("cls", ADAPTERS)
    def test_axes_inherited_staked_by_staked(self, cls):
        # Neither subclass overrides the default classification axes.
        assert "position_type" not in vars(cls)
        assert "protocol_module" not in vars(cls)
        assert cls.position_type is PositionType.STAKED
        assert cls.protocol_module is ProtocolModule.STAKED
