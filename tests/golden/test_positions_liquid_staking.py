"""Golden fixtures for the liquid staking adapters, pinned to Ethereum
block 20_450_000 (SPEC rule #5: golden fixture tests pinned to a block
height, per adapter — the clearest cause of both predecessors' deaths
was shipping none).

Every literal below is hardcoded, derived independently from the pinned
algorithms in docs/DECISIONS.md — never from the code under test:

    pos_e61f7629709553ef == "pos_" + sha256(
        "lido|eip155:1|0xae7ab96520de3a18e5e111b5eaab095312d7fe84|"
    ).hexdigest()[:16]
    grp_4051a8e6d4ae70bf == "grp_" + sha256(
        "lido|eip155:1|0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
    ).hexdigest()[:16]
    pos_ff2e449baab082ad == "pos_" + sha256(
        "rocket-pool|eip155:1|0xae78736cd615f374d3085123a210448e74fc6393|"
    ).hexdigest()[:16]
    grp_4dcab7fe60368269 == "grp_" + sha256(
        "rocket-pool|eip155:1|0xae78736cd615f374d3085123a210448e74fc6393"
    ).hexdigest()[:16]

    redemption (pinned floor): 2_500_000_000_000_000_000 rETH
        * 1_120_000_000_000_000_000 // 10**18
        == 2_800_000_000_000_000_000 exactly

stETH rebases 1:1 (identity rate), so the fake reader log must show NO
exchange-rate call for Lido — the balance already IS the ETH claim.
All outputs are RAW (price/value None): they persist and re-drill
against fresh prices without an RPC (SPEC §5.3).
"""

from __future__ import annotations

from auradefi.money.quantity import Quantity
from auradefi.positions.adapters.staking.liquid import (
    LidoAdapter,
    RocketPoolAdapter,
)
from auradefi.positions.models import (
    MetaType,
    PositionKind,
    PositionType,
    ProtocolModule,
)
from auradefi.positions.protocol import DiscoveryContext, ResolveContext

BLOCK = 20_450_000
CHAIN = "eip155:1"
HOLDER = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
ETH = "eip155:1/slip44:60"
STETH = "0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
RETH = "0xae78736cd615f374d3085123a210448e74fc6393"

STETH_BALANCE_AT_BLOCK = 12_340_000_000_000_000_000  # 12.34 stETH
RETH_BALANCE_AT_BLOCK = 2_500_000_000_000_000_000  # 2.5 rETH
RETH_RATE_AT_BLOCK = 1_120_000_000_000_000_000  # getExchangeRate: 1.12
RETH_REDEEMED_RAW = 2_800_000_000_000_000_000  # 2.5 * 1.12, exact


class RecordingReader:
    """Dict-backed ContractReader that logs every call it serves."""

    def __init__(self, responses: dict[tuple[str, str, tuple], object]) -> None:
        self._responses = dict(responses)
        self.calls: list[tuple[str, str, tuple]] = []

    def call(self, address: str, fn: str, args: tuple[object, ...] = ()) -> object:
        self.calls.append((address, fn, args))
        return self._responses[(address, fn, args)]

    def fns_called(self) -> set[str]:
        return {fn for _, fn, _ in self.calls}


def _run(adapter, reader):
    contracts = adapter.discover(DiscoveryContext(chain_id=CHAIN, reader=reader))
    ctx = ResolveContext(
        chain_id=CHAIN, address=HOLDER, reader=reader, block_number=BLOCK
    )
    return adapter.resolve(ctx, contracts)


class TestLidoBlock20450000:
    """Lido stETH at block 20_450_000: rebasing, identity rate."""

    def _positions_and_reader(self):
        reader = RecordingReader(
            {(STETH, "balanceOf", (HOLDER,)): STETH_BALANCE_AT_BLOCK}
        )
        return _run(LidoAdapter(), reader), reader

    def test_exactly_one_position_with_the_pinned_id(self):
        positions, _ = self._positions_and_reader()
        assert len(positions) == 1
        assert positions[0].id == "pos_e61f7629709553ef"
        assert positions[0].group_id == "grp_4051a8e6d4ae70bf"

    def test_app_token_staked_staked(self):
        (position,), _ = self._positions_and_reader()
        assert position.kind is PositionKind.APP_TOKEN
        assert position.position_type is PositionType.STAKED
        assert position.protocol_module is ProtocolModule.STAKED
        assert position.adapter_id == "lido"
        assert position.chain_id == CHAIN
        assert position.contract_address == STETH

    def test_supplied_eth_claim_equals_the_steth_balance(self):
        (position,), _ = self._positions_and_reader()
        (underlying,) = position.underlyings
        assert underlying.asset_id == ETH
        assert underlying.meta_type is MetaType.SUPPLIED
        assert underlying.quantity == Quantity(STETH_BALANCE_AT_BLOCK, 18)

    def test_output_is_raw_for_re_drilling(self):
        (position,), _ = self._positions_and_reader()
        assert position.underlyings[0].price is None
        assert position.underlyings[0].value is None
        assert position.value is None

    def test_no_exchange_rate_call_for_a_rebasing_receipt(self):
        _, reader = self._positions_and_reader()
        assert reader.fns_called() == {"balanceOf"}
        assert reader.calls == [(STETH, "balanceOf", (HOLDER,))]


class TestRocketPoolBlock20450000:
    """Rocket Pool rETH at block 20_450_000: redemption via getExchangeRate."""

    def _positions_and_reader(self):
        reader = RecordingReader(
            {
                (RETH, "balanceOf", (HOLDER,)): RETH_BALANCE_AT_BLOCK,
                (RETH, "getExchangeRate", ()): RETH_RATE_AT_BLOCK,
            }
        )
        return _run(RocketPoolAdapter(), reader), reader

    def test_exactly_one_position_with_the_pinned_id(self):
        positions, _ = self._positions_and_reader()
        assert len(positions) == 1
        assert positions[0].id == "pos_ff2e449baab082ad"
        assert positions[0].group_id == "grp_4dcab7fe60368269"

    def test_app_token_staked_staked(self):
        (position,), _ = self._positions_and_reader()
        assert position.kind is PositionKind.APP_TOKEN
        assert position.position_type is PositionType.STAKED
        assert position.protocol_module is ProtocolModule.STAKED
        assert position.adapter_id == "rocket-pool"
        assert position.contract_address == RETH

    def test_supplied_eth_is_the_pinned_redemption_amount(self):
        # 2.5 rETH * 1.12 == exactly 2.8 ETH — floor division, quote what
        # the user would actually get out (SPEC §4.3).
        (position,), _ = self._positions_and_reader()
        (underlying,) = position.underlyings
        assert underlying.asset_id == ETH
        assert underlying.meta_type is MetaType.SUPPLIED
        assert underlying.quantity == Quantity(RETH_REDEEMED_RAW, 18)

    def test_output_is_raw_for_re_drilling(self):
        (position,), _ = self._positions_and_reader()
        assert position.underlyings[0].price is None
        assert position.underlyings[0].value is None

    def test_exchange_rate_was_read_from_the_receipt_contract(self):
        _, reader = self._positions_and_reader()
        assert (RETH, "getExchangeRate", ()) in reader.calls
        assert reader.fns_called() == {"balanceOf", "getExchangeRate"}
