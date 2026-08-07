"""Wave-4 seam audit: the phase-11 gate against what it restates.

The gate at ``tests/golden/test_phase11_reader.py`` deliberately imports no
other golden file, so every address, integer and pinned id it needs is
restated in its own header. That is the right call for a golden, and it is
exactly what creates the seam this file audits: the SAME chain state is now
written down twice, once in the phase-4 goldens and once in the phase-11
gate, and nothing in either file compares the two. Change a reserve in one
and both stay green while they describe different blocks.

Four boundaries are audited here, none of them visible from inside one file:

1. THE TWO GOLDENS' OUTPUTS. The five adapters are run through the gate's
   cassette-backed reader and through the phase-4 goldens' own dict readers,
   and the resulting ``Position`` objects are compared as objects. This is
   the "must agree, or one of them is wrong" clause the phase restated away
   for chain state, made mechanical for the part that IS provable offline.
2. THE TWO GOLDENS' INPUTS. Every read table the gate carries is compared
   against the phase-4 table that owns the same read.
3. THE HAND-BUILT DESCRIPTORS. The gate constructs the V2 and V3
   descriptors by hand; ``discover()`` derives them from chain data. Both
   sides are run and the descriptors compared, so a hand-built one cannot
   drift from the shape discovery emits.
4. THE FIXTURE AGAINST THE SHIPPED CASSETTE HARNESS. The gate declares its
   file "a valid cassette in the committed format" and then replays it with
   a matcher of its own. What the shipped loader does with the same file is
   pinned here.

The reader-driven ``discover()`` path is the one chain-reading code path in
``positions/`` the gate does not cover, because covering it would move the
call counts the phase pins. It is covered here instead, over the same wire,
against a four-interaction recording hand-packed in this file.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from auradefi.errors import CassetteMissError, SourceError
from auradefi.positions.adapters.amm.uniswap_v2 import UniswapV2Adapter
from auradefi.positions.adapters.amm.uniswap_v3 import UniswapV3Adapter
from auradefi.positions.adapters.lending.aave import AaveV3Adapter
from auradefi.positions.adapters.staking.liquid import (
    LidoAdapter,
    RocketPoolAdapter,
)
from auradefi.positions.protocol import (
    ContractSet,
    DiscoveryContext,
    ResolveContext,
)
from auradefi.sources.evm.codec.keccak import keccak256
from auradefi.sources.evm.reader import EvmContractReader
from auradefi.sources.evm.rpc import EvmRpc
from auradefi.testing.cassettes import load

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"

FACTORY_V2 = "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f"

#: The four Uniswap V2 selectors the committed fixture does NOT carry,
#: written from the published values. Four of the fixture's ten selectors
#: (balanceOf, decimals, totalSupply, getReserves) are the same published
#: values, and every one below is checked against keccak256 in
#: :func:`test_the_hand_packed_discovery_selectors_are_the_published_ones`.
ALL_PAIRS_LENGTH = "0x574f2ba3"
ALL_PAIRS = "0x1e3dd18b"
TOKEN0 = "0x0dfe1681"
TOKEN1 = "0xd21220a7"

_MODULES: dict[str, object] = {}


def _golden(name: str) -> object:
    """One golden module, imported by path and read as data.

    Importing is what makes this a seam test: the values compared are the
    ones those files actually carry, never a copy that could drift.
    """
    if name not in _MODULES:
        path = GOLDEN_DIR / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"_seam_{name}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULES[name] = module
    return _MODULES[name]


def _gate() -> object:
    return _golden("test_phase11_reader")


def _reader() -> EvmContractReader:
    """The gate's own cassette-backed reader, one fresh matcher per call."""
    gate = _gate()
    return gate._cassette_reader(gate._matcher())


def _dict_reader_arguments(module_name: str) -> list[dict]:
    """The dict literals a golden module hands to its own fake reader.

    Read off the source with ``ast`` and evaluated against that module's
    globals, because those tables are built inside methods and are not
    reachable as attributes. Extraction rather than duplication: a copy of
    the phase-4 numbers written here would be a third statement of them.
    """
    path = GOLDEN_DIR / f"{module_name}.py"
    module = _golden(module_name)
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "DictReader":
            continue
        (argument,) = node.args
        found.append(
            eval(  # noqa: S307 - a literal from a file this repo owns
                compile(ast.Expression(argument), str(path), "eval"),
                vars(module),
            )
        )
    return found


def _hand_built_descriptors() -> list[object]:
    """Every ``ContractDescriptor`` the gate constructs, in source order."""
    path = GOLDEN_DIR / "test_phase11_reader.py"
    gate = _gate()
    built = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None)
        if name != "ContractDescriptor":
            continue
        built.append(
            eval(  # noqa: S307 - a literal from a file this repo owns
                compile(ast.Expression(node), str(path), "eval"), vars(gate)
            )
        )
    return built


def _phase_four_runs() -> dict[str, list]:
    """The five phase-4 golden runs, each through its own dict fixture."""
    uniswap = _golden("test_positions_uniswap")
    aave = _golden("test_positions_aave")
    liquid = _golden("test_positions_liquid_staking")
    aave_positions, _reader_log = aave._resolved()
    lido_positions, _lido_log = liquid.TestLidoBlock20450000()._positions_and_reader()
    reth_positions, _reth_log = (
        liquid.TestRocketPoolBlock20450000()._positions_and_reader()
    )
    return {
        "uniswap-v2": [uniswap.TestUniswapV2Block20450000()._position()],
        "uniswap-v3": [uniswap.TestUniswapV3Block20450000()._position()],
        "aave-v3": aave_positions,
        "lido": lido_positions,
        "rocket-pool": reth_positions,
    }


def _gate_runs() -> dict[str, list]:
    """The same five, through the gate's cassette-backed reader."""
    gate = _gate()
    return {
        "uniswap-v2": gate._run_v2(_reader()),
        "uniswap-v3": gate._run_v3(_reader()),
        "aave-v3": gate._run_aave(_reader()),
        "lido": gate._run_lido(_reader()),
        "rocket-pool": gate._run_rocket_pool(_reader()),
    }


def test_the_gate_and_the_phase_four_goldens_emit_the_same_positions() -> None:
    """Five adapters, two fixtures, one set of ``Position`` objects.

    The gate reads ABI-encoded words off a hand-authored cassette; the
    phase-4 goldens read python ints out of a dict. Neither file imports the
    other, so this equality is the only thing binding them. Flips to red the
    moment one side's chain state moves without the other's.
    """
    through_wire = _gate_runs()
    through_dicts = _phase_four_runs()
    assert sorted(through_wire) == sorted(through_dicts)
    for name, positions in through_wire.items():
        assert positions, f"{name} emitted nothing, so the comparison is empty"
        assert positions == through_dicts[name], name
    assert [len(v) for _k, v in sorted(through_wire.items())] == [2, 1, 1, 1, 1]


def test_the_cross_golden_equality_is_not_vacuous() -> None:
    """The negation control: move one integer and the two stop agreeing.

    Without this, an equality over two empty lists would look like proof.
    Each flip below is a single word of chain state.
    """
    gate = _gate()
    phase_four = _phase_four_runs()

    reserves = gate.V2_STATE | {
        (gate.V2_PAIR, "getReserves", ()): (
            gate.RESERVES[0] + 1_000_000,
            gate.RESERVES[1],
            gate.RESERVES[2],
        )
    }
    assert gate._run_v2(gate.DictReader(reserves)) != phase_four["uniswap-v2"]

    account = list(gate.ACCOUNT_DATA)
    account[5] = gate.ACCOUNT_DATA[5] // 2
    shifted = gate.AAVE_STATE | {
        (gate.POOL, "getUserAccountData", (gate.ALICE,)): tuple(account)
    }
    assert gate._run_aave(gate.DictReader(shifted)) != phase_four["aave-v3"]

    rate = gate.RETH_STATE | {(gate.RETH, "getExchangeRate", ()): 10**18}
    assert gate._run_rocket_pool(gate.DictReader(rate)) != phase_four["rocket-pool"]


def test_both_goldens_describe_the_same_block() -> None:
    """One block height, four files. A gate pinned elsewhere proves nothing."""
    gate = _gate()
    blocks = {
        name: _golden(name).BLOCK
        for name in (
            "test_positions_uniswap",
            "test_positions_aave",
            "test_positions_liquid_staking",
        )
    }
    assert set(blocks.values()) == {gate.BLOCK} == {20_450_000}
    assert hex(gate.BLOCK) == gate.BLOCK_TAG


def test_every_read_the_gate_restates_matches_the_phase_four_table() -> None:
    """The INPUT side of the same seam, read table against read table.

    The gate's six state dicts are compared with the phase-4 tables that own
    the same reads. The V2 pair's ``token0``/``token1`` are the only entries
    phase 4 carries and the gate does not, because the V2 descriptor supplies
    that order and ``resolve`` never asks the pair for it.
    """
    gate = _gate()
    aave = _golden("test_positions_aave")
    liquid = _golden("test_positions_liquid_staking")
    v2_table, v3_table = _dict_reader_arguments("test_positions_uniswap")

    assert aave.BlockReader.RESPONSES == gate.AAVE_STATE
    assert v3_table == gate.V3_STATE
    assert {
        (liquid.STETH, "balanceOf", (liquid.HOLDER,)): liquid.STETH_BALANCE_AT_BLOCK
    } == gate.LIDO_STATE
    assert {
        (liquid.RETH, "balanceOf", (liquid.HOLDER,)): liquid.RETH_BALANCE_AT_BLOCK,
        (liquid.RETH, "getExchangeRate", ()): liquid.RETH_RATE_AT_BLOCK,
    } == gate.RETH_STATE
    assert liquid.RETH_REDEEMED_RAW == gate.RETH_REDEEMED_RAW

    shared = set(v2_table) & set(gate.V2_STATE)
    assert {key: v2_table[key] for key in shared} == {
        key: gate.V2_STATE[key] for key in shared
    }
    assert set(v2_table) - set(gate.V2_STATE) == {
        (gate.V2_PAIR, "token0", ()),
        (gate.V2_PAIR, "token1", ()),
    }
    assert set(gate.V2_STATE) - set(v2_table) == set()


def test_the_gate_restates_the_shipped_adapter_configuration() -> None:
    """Addresses the gate hardcodes against the class attributes that ship.

    The Aave subclass in the gate overrides ``markets`` and inherits
    ``pool``, so the pinned group id is derived from the SHIPPED pool
    address. The receipt tables are the shipped ones untouched.
    """
    gate = _gate()
    assert gate.POOL == AaveV3Adapter.pool
    assert gate.V3_MANAGER == UniswapV3Adapter.position_manager
    assert gate.V3_FACTORY == UniswapV3Adapter.factory_address
    assert AaveV3Adapter.markets == (), "a shipped market table would go unread"
    (steth,) = LidoAdapter.receipts["eip155:1"]
    (reth,) = RocketPoolAdapter.receipts["eip155:1"]
    assert (steth.address, steth.rate_fn) == (gate.STETH, None)
    assert (reth.address, reth.rate_fn) == (gate.RETH, "getExchangeRate")
    assert steth.underlying_caip19 == reth.underlying_caip19 == gate.ETH_ID
    assert (steth.underlying_decimals, reth.underlying_decimals) == (18, 18)


def test_the_hand_built_v3_descriptor_is_the_one_discover_emits() -> None:
    """The gate builds the V3 descriptor by hand; ``discover`` derives it.

    ``UniswapV3Adapter.discover`` touches no reader, so the two can be
    compared directly. Flips to red if the shipped category string, the
    manager address or the descriptor's field set moves under the gate.
    """
    gate = _gate()
    (hand_built,) = [
        descriptor
        for descriptor in _hand_built_descriptors()
        if descriptor.adapter_id == "uniswap-v3"
    ]
    emitted = UniswapV3Adapter().discover(
        DiscoveryContext(chain_id=gate.CHAIN, reader=gate.DictReader({}))
    )
    assert emitted == ContractSet.of(hand_built)
    assert gate._run_v3(_reader()) == UniswapV3Adapter().resolve(
        ResolveContext(
            chain_id=gate.CHAIN,
            address=gate.VITALIK,
            reader=gate.DictReader(gate.V3_STATE),
            block_number=gate.BLOCK,
        ),
        emitted,
    )


class _WireReplay:
    """A four-interaction JSON-RPC recording, keyed on the whole call.

    Hand-packed in this file from the wire shape ``[{"to", "data"}, tag]``
    and nothing else. Every argument is part of the key, so a target, a
    selector, an argument word or the block tag that does not match the
    recording MISSES rather than being served a neighbour's answer.
    """

    def __init__(self, recorded: dict[tuple[str, str, str], str]) -> None:
        self._recorded = dict(recorded)
        self.served: list[tuple[str, str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        target, tag = body["params"]
        key = (target["to"], target["data"], tag)
        if key not in self._recorded:
            raise AssertionError(f"not recorded: {key}")
        self.served.append(key)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": self._recorded[key]},
        )

    def reader(self, block_number: int) -> EvmContractReader:
        client = httpx.Client(transport=httpx.MockTransport(self.handle))
        return EvmContractReader(
            EvmRpc(client, "https://evm-node.invalid/rpc"), block_number
        )


def _word(value: int) -> str:
    return f"{value:064x}"


def _discovery_recording(token0: str, token1: str) -> _WireReplay:
    """One pair on the factory, with its two tokens in the given order."""
    gate = _gate()
    tag = gate.BLOCK_TAG
    pair = gate.V2_PAIR
    return _WireReplay(
        {
            (FACTORY_V2, ALL_PAIRS_LENGTH, tag): "0x" + _word(1),
            (FACTORY_V2, ALL_PAIRS + _word(0), tag): "0x" + _word(int(pair, 16)),
            (pair, TOKEN0, tag): "0x" + _word(int(token0, 16)),
            (pair, TOKEN1, tag): "0x" + _word(int(token1, 16)),
        }
    )


def test_the_hand_packed_discovery_selectors_are_the_published_ones() -> None:
    """Two derivations of four selectors: the literals above and keccak256.

    The gate's fixture carries ten selectors and none of these four, so
    without this the discovery recording below could address any function
    at all and still look right.
    """
    derived = {
        signature: "0x" + keccak256(signature.encode())[:4].hex()
        for signature in ("allPairsLength()", "allPairs(uint256)", "token0()", "token1()")
    }
    assert derived == {
        "allPairsLength()": ALL_PAIRS_LENGTH,
        "allPairs(uint256)": ALL_PAIRS,
        "token0()": TOKEN0,
        "token1()": TOKEN1,
    }


def test_the_hand_built_v2_descriptor_is_what_the_wire_derives() -> None:
    """The one reader-driven ``discover`` in the package, over the wire.

    The gate hand-writes ``underlyings=(USDC, WETH)``; discovery reads
    ``token0``/``token1`` off the pair and builds the same pair of CAIP-19
    ids. Four calls, all four served, so no leg is skipped.
    """
    gate = _gate()
    (hand_built,) = [
        descriptor
        for descriptor in _hand_built_descriptors()
        if descriptor.adapter_id == "uniswap-v2"
    ]
    replay = _discovery_recording(gate.USDC, gate.WETH)
    emitted = UniswapV2Adapter().discover(
        DiscoveryContext(chain_id=gate.CHAIN, reader=replay.reader(gate.BLOCK))
    )
    assert emitted == ContractSet.of(hand_built)
    assert len(replay.served) == 4
    assert hand_built.underlyings == (gate.USDC_ID, gate.WETH_ID)


def test_the_discovery_recording_honours_the_order_it_is_given() -> None:
    """The negation control for the recording above.

    A fake that ignored the token it was asked for would make the previous
    test pass whatever the pair returns. Swap the two words on the wire and
    the descriptor's underlyings must swap with them.
    """
    gate = _gate()
    replay = _discovery_recording(gate.WETH, gate.USDC)
    (swapped,) = UniswapV2Adapter().discover(
        DiscoveryContext(chain_id=gate.CHAIN, reader=replay.reader(gate.BLOCK))
    )
    assert swapped.underlyings == (gate.WETH_ID, gate.USDC_ID)


def test_the_v2_reserves_are_paired_by_descriptor_order_alone() -> None:
    """The pairing of reserve0/reserve1 to assets is never checked at resolve.

    ``resolve`` zips ``descriptor.underlyings`` against ``(reserve0,
    reserve1)`` and does not read ``token0``/``token1``, which is why the
    gate's V2 leg costs five calls and not seven. A descriptor whose
    underlyings are in the other order therefore yields a full, plausible,
    silently wrong position. Discovery is the only thing establishing that
    order, so this pins where the trust actually sits.
    """
    gate = _gate()
    (hand_built,) = [
        descriptor
        for descriptor in _hand_built_descriptors()
        if descriptor.adapter_id == "uniswap-v2"
    ]
    reversed_descriptor = type(hand_built)(
        adapter_id=hand_built.adapter_id,
        chain_id=hand_built.chain_id,
        address=hand_built.address,
        category=hand_built.category,
        underlyings=tuple(reversed(hand_built.underlyings)),
    )
    matcher = gate._matcher()
    ctx = ResolveContext(
        chain_id=gate.CHAIN,
        address=gate.VITALIK,
        reader=gate._cassette_reader(matcher),
        block_number=gate.BLOCK,
    )
    (position,) = UniswapV2Adapter().resolve(
        ctx, ContractSet.of(reversed_descriptor)
    )
    quantities = {u.asset_id: u.quantity.raw for u in position.underlyings}
    assert quantities[gate.WETH_ID] == gate.V2_USDC_RAW
    assert quantities[gate.USDC_ID] == gate.V2_WETH_RAW
    assert position.id == gate.V2_POSITION_ID
    assert matcher.total() == 5


def test_the_shipped_loader_takes_the_file_and_cannot_replay_it() -> None:
    """What ``testing/cassettes.load`` does with the gate's fixture.

    The extra ``note`` keys and the per-interaction ``request.body`` are
    ignored, so the file IS loadable in the committed format. What the file
    is not is replayable through that harness: all nineteen POSTs share one
    key, so they are served by position, and the second read of the first
    run is answered with the first run's third word. It fails loudly, and it
    cannot be fixed by reordering, because the six runs issue twenty-three
    calls over nineteen distinct bodies. That arithmetic is why the gate
    carries a body-keyed matcher of its own.
    """
    gate = _gate()
    document = gate._cassette_document()
    assert "note" in document
    assert all("note" in item for item in document["interactions"])
    assert all("body" in item["request"] for item in document["interactions"])

    cassette = load(gate.CASSETTE)
    (responses,) = cassette._recorded.values()
    assert len(cassette._recorded) == 1
    assert len(responses) == gate.INTERACTION_COUNT

    reader = EvmContractReader(
        EvmRpc(cassette.client(), gate.NODE_URL), block_number=gate.BLOCK
    )
    with pytest.raises(SourceError) as caught:
        gate._run_tokens(reader)
    assert "decimals result did not decode" in str(caught.value)
    assert gate.TOTAL_CALLS == 23 > gate.INTERACTION_COUNT == 19


def test_the_match_key_is_the_canonical_json_decisions_pins() -> None:
    """The gate re-implements ``canonical_json`` inline; both must agree.

    DECISIONS.md pins ``canonical_json = json.dumps(obj, separators=(",",
    ":"), sort_keys=True)`` and ``webhooks/models.py`` ships it. The gate's
    ``_rpc_key`` writes the same call out by hand and says so in prose, so
    the day the shipped one grows an argument the claim silently stops being
    true. Compared over all nineteen recorded bodies, with the pinned form
    spelled out once so neither side is the only witness.
    """
    from auradefi.webhooks.models import canonical_json

    gate = _gate()
    assert canonical_json({"b": 1, "a": [2, 3]}) == '{"a":[2,3],"b":1}'
    for interaction in gate._cassette_document()["interactions"]:
        body = interaction["request"]["body"]
        method, params = gate._rpc_key(body)
        assert method == "eth_call"
        assert params == canonical_json(body["params"])


class _BodyKeyedFixture:
    """The declared matcher discipline, implemented from the words alone.

    Written from the seam text and the wire shape and nothing else: key on
    the request BODY, serve a matched key any number of times with the same
    response, raise ``CassetteMissError`` on a miss, count services per key.
    The key here is the tuple ``(method, to, data, tag)`` taken straight off
    the recording, with no JSON canonicalisation anywhere, so agreement with
    the gate is agreement between two different keying schemes.
    """

    def __init__(self, document: dict) -> None:
        self.responses: dict[tuple[str, str, str, str], dict] = {}
        self.served: dict[tuple[str, str, str, str], int] = {}
        for interaction in document["interactions"]:
            body = interaction["request"]["body"]
            target, tag = body["params"]
            key = (body["method"], target["to"], target["data"], tag)
            assert key not in self.responses, f"duplicate recording: {key}"
            self.responses[key] = interaction["response"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        target, tag = body["params"]
        key = (body["method"], target["to"], target["data"], tag)
        spec = self.responses.get(key)
        if spec is None:
            raise CassetteMissError(f"not recorded: {key}")
        self.served[key] = self.served.get(key, 0) + 1
        return httpx.Response(spec["status"], json=spec["json"])

    def reader(self, url: str, block_number: int) -> EvmContractReader:
        client = httpx.Client(transport=httpx.MockTransport(self.handle))
        return EvmContractReader(EvmRpc(client, url), block_number)

    def total(self) -> int:
        return sum(self.served.values())


def test_a_matcher_built_only_from_the_declared_discipline_serves_the_gate() -> None:
    """The gate's own matcher is not the only one its fixture can drive.

    The six runs go through an independently keyed replay of the same
    committed file: the positions must be the ones the gate pins, the per-run
    counts must be the pinned split, every recorded interaction must be
    served, and an unrecorded read must be refused by name. A matcher that
    quietly ignored part of the key would over-serve and the counts would
    move.
    """
    gate = _gate()
    fixture = _BodyKeyedFixture(gate._cassette_document())
    reader = fixture.reader(gate.NODE_URL, gate.BLOCK)

    counts = []
    before = 0
    outputs = {}
    for name, run, _state in gate.RUNS:
        outputs[name] = run(reader)
        counts.append(fixture.total() - before)
        before = fixture.total()

    assert tuple(counts) == gate.PER_RUN_CALLS == (3, 5, 7, 5, 1, 2)
    assert fixture.total() == gate.TOTAL_CALLS
    assert len(fixture.served) == len(fixture.responses) == gate.INTERACTION_COUNT
    assert outputs["tokens"] == (gate.LP_BALANCE, 18, gate.LP_TOTAL_SUPPLY)
    assert outputs["uniswap-v2"] == _phase_four_runs()["uniswap-v2"]
    assert outputs["uniswap-v3"] == _phase_four_runs()["uniswap-v3"]
    assert outputs["aave-v3"] == _phase_four_runs()["aave-v3"]
    with pytest.raises(CassetteMissError):
        reader.call(gate.REVERTER, "decimals", ())


def test_the_independent_matcher_is_blind_to_nothing_that_identifies_a_read():
    """The negation control for the replay above, one flip per key member.

    Each of these must miss: a read at the right target with the wrong
    selector, the right selector at the wrong target, and the right call at
    the wrong block. A fake that ignored any one of them would have made the
    previous test pass for the wrong reason.
    """
    gate = _gate()
    fixture = _BodyKeyedFixture(gate._cassette_document())

    at_block = fixture.reader(gate.NODE_URL, gate.BLOCK)
    assert at_block.call(gate.V2_PAIR, "decimals", ()) == 18
    with pytest.raises(CassetteMissError):
        at_block.call(gate.V2_PAIR, "token0", ())
    with pytest.raises(CassetteMissError):
        at_block.call(gate.STETH, "decimals", ())
    with pytest.raises(CassetteMissError):
        fixture.reader(gate.NODE_URL, gate.BLOCK + 1).call(
            gate.V2_PAIR, "decimals", ()
        )


def test_the_recorded_url_and_method_are_the_ones_the_gate_posts() -> None:
    """The gate's matcher reads only the body, so nothing else is checked.

    ``request.url`` and ``request.method`` in the fixture are what the
    shipped loader keys on and what the gate's matcher ignores entirely, so
    the two could disagree with the reader's endpoint and no test in the
    gate would notice. Compared here against the request httpx actually
    builds.
    """
    gate = _gate()
    document = gate._cassette_document()
    seen: list[httpx.Request] = []

    def probe(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + _word(6)}
        )

    reader = EvmContractReader(
        EvmRpc(httpx.Client(transport=httpx.MockTransport(probe)), gate.NODE_URL),
        block_number=gate.BLOCK,
    )
    assert reader.call(gate.USDC, "decimals", ()) == 6
    (request,) = seen
    assert {item["request"]["url"] for item in document["interactions"]} == {
        str(request.url)
    }
    assert {item["request"]["method"] for item in document["interactions"]} == {
        request.method
    }
    assert str(request.url) == gate.NODE_URL
