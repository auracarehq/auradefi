"""Phase 11 gate: five shipped adapters over a cassette-backed EVM reader.

READ THIS FIRST. The fixture this module replays,
``tests/cassettes/phase11_reader.json``, is HAND-AUTHORED and was never
recorded from a node. Every result word was packed by hand from the integers
the phase-4 goldens already pin, by a throwaway script that imports nothing
from ``auradefi``, and its ten selectors were derived from an independent
keccak256 and cross-checked against the literals the abi and reader orders
pin. So this test proves the selector derivation, the word packing, the tuple
decode, the block pin and the JSON-RPC wire path end to end, and it proves the
adapters produce byte-identical ``Position`` objects through that path and
through a dict-backed reader. It does NOT prove agreement with a real archive
node, and nothing here should be read as claiming mainnet state. Obtaining a
genuine archive-node recording at block 20,450,000, and re-pinning every
existing golden to whatever that node returns, needs network and an archive
node and is a separate human task.

WHY NO RECORDING WAS POSSIBLE HERE. ``tests/conftest.py`` blocks
``socket.connect`` for the whole suite, ``tests/cassettes/`` carries no EVM RPC
recording and the repository ships no recording script. Two of the pinned
preimages could not come from a node in any case:
``tests/golden/test_positions_aave.py`` pins the fabricated user
``0x…0a11ce`` with balances taken from SPEC section 6.3's worked example, and
``tests/golden/test_positions_liquid_staking.py`` pins a protocol-global rETH
exchange rate that no choice of holder can make a node return. Rather than
claim an agreement the loop cannot reach, this gate proves what IS provable
offline and says so.

WHY THE MATCHER IS NOT THE SHIPPED ONE. ``auradefi.testing.cassettes`` keys on
method, host, path and sorted query and never reads the request body, and it
serves the last recorded response forever once a key is exhausted. Under that
matcher every JSON-RPC POST to one node URL collapses onto a single key, which
DECISIONS.md pins deliberately ("JSON-RPC POSTs share one cassette key, so
recorded order IS the wire contract"). Positional service would make this
golden order-sensitive and tolerant of over-calling, which is the very defect
class the reader's id-matching test exists to catch, arriving through the
fixture instead of through the code. So the matcher below keys each
interaction on ``(body['method'], canonical params)``, the same
canonicalization DECISIONS.md pins for webhook bodies, and serves a matched
key any number of times with the same response. This is the one place phase 11
departs from that pin. The order-blindness is paid for by three guards: every
recorded interaction must be served at least once, any unrecorded read raises
``CassetteMissError``, and the per-run and total call counts are asserted
exactly.

THE FIXTURE IS DATA. It is committed, and this module only ever reads it. A
vector produced by running the code under test proves nothing, so nothing here
generates or rewrites it.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from auradefi.errors import CassetteMissError
from auradefi.money.quantity import Quantity
from auradefi.positions.adapters.amm.uniswap_v2 import UniswapV2Adapter
from auradefi.positions.adapters.amm.uniswap_v3 import UniswapV3Adapter
from auradefi.positions.adapters.lending.aave import AaveV3Adapter, Market
from auradefi.positions.adapters.staking.liquid import (
    LidoAdapter,
    RocketPoolAdapter,
)
from auradefi.positions.adapters.tokens import (
    erc20_balance,
    erc20_decimals,
    erc20_total_supply,
)
from auradefi.positions.models import MetaType, Range
from auradefi.positions.protocol import (
    ContractDescriptor,
    ContractReader,
    ContractSet,
    DiscoveryContext,
    ResolveContext,
)
from auradefi.sources.evm.codec.keccak import keccak256
from auradefi.sources.evm.reader import EvmContractReader
from auradefi.sources.evm.rpc import EvmRpc
from auradefi.testing.cassettes import load

CASSETTE = Path(__file__).resolve().parents[1] / "cassettes" / "phase11_reader.json"

NODE_URL = "https://evm-node.invalid/rpc"
BLOCK = 20_450_000
BLOCK_TAG = "0x1380ad0"
INTERACTION_COUNT = 19
TOTAL_CALLS = 23
PER_RUN_CALLS = (3, 5, 7, 5, 1, 2)

# Every address, id and integer this gate needs is restated here. No golden
# file imports another: a shared constant that drifts would move two files'
# expectations together and neither would notice.
CHAIN = "eip155:1"
VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
ALICE = "0x00000000000000000000000000000000000a11ce"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
V2_PAIR = "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"
V3_MANAGER = "0xc36442b4a4522e871399cd717abdd847ab11fe88"
V3_FACTORY = "0x1f98431c8ad98523631ae4a59f267346ea31f984"
V3_POOL = "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8"
POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
AWETH = "0x4d5f47fa6a74757f35c14fd3a6ef8e3c9bc514e8"
DEBT_WETH = "0xea51d7853eefb32b6ee06b1c12e6dcca88be0ffe"
AUSDC = "0x98c23e9d8f34fefb1b7bd6a91b7ff122f4e16f5c"
DEBT_USDC = "0x72e95b8931767c79ba4eee721354d6e99a61d004"
STETH = "0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
RETH = "0xae78736cd615f374d3085123a210448e74fc6393"
#: An address the fixture deliberately does not carry, so a read against it
#: must miss. It is also the reverting leg of the Multicall3 vectors below.
REVERTER = "0x000000000000000000000000000000000000dead"

USDC_ID = f"{CHAIN}/erc20:{USDC}"
WETH_ID = f"{CHAIN}/erc20:{WETH}"
ETH_ID = "eip155:1/slip44:60"

# The pinned chain state, restated. These are the SAME integers the phase-4
# goldens carry, and the same ones the fixture's words were packed from.
LP_BALANCE = 850_000_000_000_000
LP_TOTAL_SUPPLY = 850_000_000_000_000_000
RESERVES = (52_000_000_000_000, 14_500_000_000_000_000_000_000, 1_722_470_000)
NFT_TOKEN_ID = 912345
NFT_POSITION = (
    0,
    ZERO_ADDRESS,
    USDC,
    WETH,
    3000,
    193320,
    195480,
    2_000_000_000_000_000,
    0,
    0,
    125_000_000,
    40_000_000_000_000_000,
)
SLOT0 = (1322911675800610514020464994530246, 194470, 0, 1, 1, 0, True)
ACCOUNT_DATA = (
    3_584_250_000_000,
    500_000_000_000,
    2_367_400_000_000,
    8250,
    8000,
    5_812_500_000_000_000_000,
)
AWETH_BALANCE = 10_000_000_000_000_000_000
DEBT_USDC_BALANCE = 5_000_000_000
STETH_BALANCE = 12_340_000_000_000_000_000
RETH_BALANCE = 2_500_000_000_000_000_000
RETH_RATE = 1_120_000_000_000_000_000

# Derived by the pinned algorithms, restated from the orders that pin them.
# V2 pro-rata (integer floor, burn semantics):
#   850_000_000_000_000 * 52_000_000_000_000 // 850_000_000_000_000_000
V2_USDC_RAW = 52_000_000_000
V2_WETH_RAW = 14_500_000_000_000_000_000
# V3 TickMath: amount0 = ((L<<96)*(sqrtB-sqrtP)//sqrtB)//sqrtP,
#              amount1 = L*(sqrtP-sqrtA)//2**96
V3_USDC_RAW = 5_898_331_123
V3_WETH_RAW = 1_865_958_029_873_234_551
# Receipt redemption: share_raw * rate_raw // 10**18, floor. 2.5 * 1.12.
RETH_REDEEMED_RAW = 2_800_000_000_000_000_000

V2_POSITION_ID = "pos_e463a531f5d6a400"
V2_GROUP_ID = "grp_b351d79d77bc24eb"
V3_POSITION_ID = "pos_447985e390bf1d89"
V3_GROUP_ID = "grp_9b813f4a0ae43e5b"
AAVE_SUPPLY_ID = "pos_baff12a5eafb77f6"
AAVE_BORROW_ID = "pos_1bbfb302ddabf62b"
AAVE_GROUP_ID = "grp_0f89caffe413b09f"
LIDO_POSITION_ID = "pos_e61f7629709553ef"
LIDO_GROUP_ID = "grp_4051a8e6d4ae70bf"
RETH_POSITION_ID = "pos_ff2e449baab082ad"
RETH_GROUP_ID = "grp_4dcab7fe60368269"

EMPTY_KECCAK = "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


def _cassette_document() -> dict:
    """The committed cassette as raw JSON. Read only, never written.

    Also consumed by ``tests/contract/seams/test_phase11_wave*.py``, which
    import this module by path for its recorded wire. That is the seam: this
    file owns the fixture, and the seam tests re-derive the selectors, the
    calldata and the results against it from their own side.
    """
    return json.loads(CASSETTE.read_text(encoding="utf-8"))


def _fixture_triples() -> tuple[tuple[str, str, str], ...]:
    """The recorded wire as ``(to, calldata, result)``, read off the file.

    A PROJECTION of the committed JSON, never the source of it. The seam
    tests iterate this; keeping the JSON authoritative means there is one
    fixture, on disk, and no second copy in Python that could drift from it.
    """
    triples = []
    for interaction in _cassette_document()["interactions"]:
        target, _tag = interaction["request"]["body"]["params"]
        triples.append(
            (target["to"], target["data"], interaction["response"]["json"]["result"])
        )
    return tuple(triples)


#: The recorded wire, for this module's own guards and for the seam tests.
FIXTURE: tuple[tuple[str, str, str], ...] = _fixture_triples()


def _rpc_key(body: dict) -> tuple[str, str]:
    """A JSON-RPC request body as its match key.

    ``(method, canonical params)``, the canonicalization DECISIONS.md pins
    for webhook bodies. Keying on the body is the whole point: the shipped
    matcher keys on the URL, and every call here goes to one URL.
    """
    return (
        body["method"],
        json.dumps(body["params"], separators=(",", ":"), sort_keys=True),
    )


class JsonRpcMatcher:
    """Replays the committed fixture, keyed on the request BODY.

    Serves a matched key any number of times with the same response, counts
    services per key so a run's call total and the dead-entry sweep are both
    measurable, and raises ``CassetteMissError`` on anything it does not
    carry. Order is deliberately not part of the contract here.
    """

    def __init__(self, document: dict) -> None:
        self.responses: dict[tuple[str, str], dict] = {}
        self.served: Counter[tuple[str, str]] = Counter()
        for interaction in document["interactions"]:
            self.responses[_rpc_key(interaction["request"]["body"])] = (
                interaction["response"]
            )

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one POST from the fixture, or refuse it by name."""
        key = _rpc_key(json.loads(request.content))
        spec = self.responses.get(key)
        if spec is None:
            raise CassetteMissError(
                f"{key[0]} {key[1]} is not recorded in {CASSETTE.name}"
            )
        self.served[key] += 1
        return httpx.Response(
            spec["status"], headers=spec["headers"], json=spec["json"]
        )

    def transport(self) -> httpx.MockTransport:
        """A transport that answers from the fixture and opens no socket."""
        return httpx.MockTransport(self.handle)

    def total(self) -> int:
        """How many calls this matcher has served in all."""
        return sum(self.served.values())

    def dead(self) -> list[tuple[str, str]]:
        """Recorded interactions nothing has asked for."""
        return [key for key in self.responses if self.served[key] == 0]


class DictReader:
    """Dict-backed ContractReader keyed (address_lower, fn, args), no I/O.

    The shape at ``tests/golden/test_positions_uniswap.py``. Carrying the
    identical pinned integers, it is the control the cassette-backed reader
    is compared against.
    """

    def __init__(self, responses: dict[tuple[str, str, tuple], object]) -> None:
        self._responses = dict(responses)

    def call(
        self, address: str, fn: str, args: tuple[object, ...] = ()
    ) -> object:
        """Return the pinned answer, or KeyError on an unpinned read."""
        return self._responses[(address.lower(), fn, args)]


def _matcher() -> JsonRpcMatcher:
    return JsonRpcMatcher(_cassette_document())


def _cassette_reader(matcher: JsonRpcMatcher) -> EvmContractReader:
    """The reader under test, over the fixture, pinned at the block."""
    return EvmContractReader(
        EvmRpc(httpx.Client(transport=matcher.transport()), NODE_URL),
        block_number=BLOCK,
    )


TOKENS_STATE: dict[tuple[str, str, tuple], object] = {
    (V2_PAIR, "balanceOf", (VITALIK,)): LP_BALANCE,
    (V2_PAIR, "decimals", ()): 18,
    (V2_PAIR, "totalSupply", ()): LP_TOTAL_SUPPLY,
}

V2_STATE: dict[tuple[str, str, tuple], object] = {
    (V2_PAIR, "balanceOf", (VITALIK,)): LP_BALANCE,
    (V2_PAIR, "totalSupply", ()): LP_TOTAL_SUPPLY,
    (V2_PAIR, "getReserves", ()): RESERVES,
    (USDC, "decimals", ()): 6,
    (WETH, "decimals", ()): 18,
}

V3_STATE: dict[tuple[str, str, tuple], object] = {
    (V3_MANAGER, "balanceOf", (VITALIK,)): 1,
    (V3_MANAGER, "tokenOfOwnerByIndex", (VITALIK, 0)): NFT_TOKEN_ID,
    (V3_MANAGER, "positions", (NFT_TOKEN_ID,)): NFT_POSITION,
    (V3_FACTORY, "getPool", (USDC, WETH, 3000)): V3_POOL,
    (V3_POOL, "slot0", ()): SLOT0,
    (USDC, "decimals", ()): 6,
    (WETH, "decimals", ()): 18,
}

AAVE_STATE: dict[tuple[str, str, tuple], object] = {
    (AWETH, "balanceOf", (ALICE,)): AWETH_BALANCE,
    (DEBT_WETH, "balanceOf", (ALICE,)): 0,
    (AUSDC, "balanceOf", (ALICE,)): 0,
    (DEBT_USDC, "balanceOf", (ALICE,)): DEBT_USDC_BALANCE,
    (POOL, "getUserAccountData", (ALICE,)): ACCOUNT_DATA,
}

LIDO_STATE: dict[tuple[str, str, tuple], object] = {
    (STETH, "balanceOf", (VITALIK,)): STETH_BALANCE,
}

RETH_STATE: dict[tuple[str, str, tuple], object] = {
    (RETH, "balanceOf", (VITALIK,)): RETH_BALANCE,
    (RETH, "getExchangeRate", ()): RETH_RATE,
}

#: Every one of the nineteen reads as ``(address, fn, args) -> decoded value``,
#: DECODED and unwrapped: a bare scalar where the function declares one return
#: type, a tuple where it declares several. This is the other statement of
#: what the fixture's result words mean, and the seam audit in
#: ``tests/contract/seams/test_phase11_wave2_codec_seams.py`` decodes the
#: recorded words against it.
DICT_READS: dict[tuple[str, str, tuple], object] = {
    **TOKENS_STATE,
    **V2_STATE,
    **V3_STATE,
    **AAVE_STATE,
    **LIDO_STATE,
    **RETH_STATE,
}


# --------------------------------------------------------------------------
# Two hand-laid Multicall3 payloads. These are NOT part of the nineteen-read
# gate: aggregate3 has its own dynamic layout, and the wave-2 seam audit needs
# a batch vector that no work order owned. Packed word by word from the
# documented layout, never by running the codec, so the seam audit's
# encode_aggregate3/decode_aggregate3 comparison is a genuine second
# derivation. The five legs are decimals(USDC), balanceOf(WETH, vitalik),
# totalSupply(V2_PAIR), a REVERTER leg that fails, and getReserves(V2_PAIR):
# three different data lengths, so an offset table built on a fixed stride
# instead of a running total produces a plausible payload and fails there.
# --------------------------------------------------------------------------

#: aggregate3 calldata, INCLUDING its 82ad56cb selector. Head word 0x20, the
#: array length, five running-total offsets (160, 320, 512, 672, 832), then
#: each element as target, allowFailure, the constant 0x60, the data length
#: and the data right-padded to a multiple of 32.
FIVE_CALL_CALLDATA = (
    "0x82ad56cb00000000000000000000000000000000000000000000000000000000"
    "0000002000000000000000000000000000000000000000000000000000000000"
    "0000000500000000000000000000000000000000000000000000000000000000"
    "000000a000000000000000000000000000000000000000000000000000000000"
    "0000014000000000000000000000000000000000000000000000000000000000"
    "0000020000000000000000000000000000000000000000000000000000000000"
    "000002a000000000000000000000000000000000000000000000000000000000"
    "00000340000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce"
    "3606eb4800000000000000000000000000000000000000000000000000000000"
    "0000000100000000000000000000000000000000000000000000000000000000"
    "0000006000000000000000000000000000000000000000000000000000000000"
    "00000004313ce567000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000c02aaa39b223fe8d0a0e5c4f27ead908"
    "3c756cc200000000000000000000000000000000000000000000000000000000"
    "0000000100000000000000000000000000000000000000000000000000000000"
    "0000006000000000000000000000000000000000000000000000000000000000"
    "0000002470a08231000000000000000000000000d8da6bf26964af9d7eed9e03"
    "e53415d37aa96045000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000b4e16d0168e52d35cacd2c6185b44281"
    "ec28c9dc00000000000000000000000000000000000000000000000000000000"
    "0000000100000000000000000000000000000000000000000000000000000000"
    "0000006000000000000000000000000000000000000000000000000000000000"
    "0000000418160ddd000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000dead00000000000000000000000000000000000000000000000000000000"
    "0000000100000000000000000000000000000000000000000000000000000000"
    "0000006000000000000000000000000000000000000000000000000000000000"
    "00000004313ce567000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000b4e16d0168e52d35cacd2c6185b44281"
    "ec28c9dc00000000000000000000000000000000000000000000000000000000"
    "0000000100000000000000000000000000000000000000000000000000000000"
    "0000006000000000000000000000000000000000000000000000000000000000"
    "000000040902f1ac000000000000000000000000000000000000000000000000"
    "00000000"
)

#: The matching ``(bool,bytes)[]`` return, the reverting leg DECLARED as an
#: empty payload rather than defaulted to a zero word. Element inner offset is
#: 0x40 here, against the call side's 0x60.
FIVE_CALL_RESULT = (
    "0x0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000005"
    "00000000000000000000000000000000000000000000000000000000000000a0"
    "0000000000000000000000000000000000000000000000000000000000000120"
    "00000000000000000000000000000000000000000000000000000000000001a0"
    "0000000000000000000000000000000000000000000000000000000000000220"
    "0000000000000000000000000000000000000000000000000000000000000280"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000006"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000de0b6b3a7640000"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000bcbce7f1b150000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "00000000000000000000000000000000000000000000000000002f4b31874000"
    "0000000000000000000000000000000000000000000003120bec57b51c100000"
    "0000000000000000000000000000000000000000000000000000000066aace70"
)

#: The same batch with the reverting leg carrying an ``Error(string)`` body,
#: so a declared failure with returndata and one with none both have a vector.
#: Its element grows to 224 bytes, which moves the LAST offset from 640 to 768
#: and is what a fixed-stride offset table gets wrong.
FIVE_CALL_RESULT_WITH_PAYLOAD = (
    "0x0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000005"
    "00000000000000000000000000000000000000000000000000000000000000a0"
    "0000000000000000000000000000000000000000000000000000000000000120"
    "00000000000000000000000000000000000000000000000000000000000001a0"
    "0000000000000000000000000000000000000000000000000000000000000220"
    "0000000000000000000000000000000000000000000000000000000000000300"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000000000000000006"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000de0b6b3a7640000"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "0000000000000000000000000000000000000000000000000bcbce7f1b150000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000064"
    "08c379a000000000000000000000000000000000000000000000000000000000"
    "0000002000000000000000000000000000000000000000000000000000000000"
    "0000000b6e6f20646563696d616c730000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000001"
    "0000000000000000000000000000000000000000000000000000000000000040"
    "0000000000000000000000000000000000000000000000000000000000000060"
    "00000000000000000000000000000000000000000000000000002f4b31874000"
    "0000000000000000000000000000000000000000000003120bec57b51c100000"
    "0000000000000000000000000000000000000000000000000000000066aace70"
)

#: The reverting leg's returndata: ``Error(string)`` carrying "no decimals".
#: 100 bytes, which is NOT a multiple of 32, so the padding to 128 inside the
#: element is load-bearing and a decoder trusting the declared length rather
#: than the padded span reads the next element's success word as string bytes.
REVERT_PAYLOAD = (
    "08c379a0"
    "0000000000000000000000000000000000000000000000000000000000000020"
    "000000000000000000000000000000000000000000000000000000000000000b"
    "6e6f20646563696d616c73000000000000000000000000000000000000000000"
)


class MainnetAaveV3(AaveV3Adapter):
    """Aave v3 mainnet over the WETH and USDC reserves, restated."""

    markets = (
        Market(AWETH, DEBT_WETH, ETH_ID, 18),
        Market(AUSDC, DEBT_USDC, USDC_ID, 6),
    )


def _run_tokens(reader: ContractReader) -> tuple[int, int, int]:
    return (
        erc20_balance(reader, V2_PAIR, VITALIK),
        erc20_decimals(reader, V2_PAIR),
        erc20_total_supply(reader, V2_PAIR),
    )


def _run_v2(reader: ContractReader) -> list:
    descriptor = ContractDescriptor(
        adapter_id="uniswap-v2",
        chain_id=CHAIN,
        address=V2_PAIR,
        category="amm-pair",
        underlyings=(USDC_ID, WETH_ID),
    )
    ctx = ResolveContext(
        chain_id=CHAIN, address=VITALIK, reader=reader, block_number=BLOCK
    )
    return UniswapV2Adapter().resolve(ctx, ContractSet.of(descriptor))


def _run_v3(reader: ContractReader) -> list:
    descriptor = ContractDescriptor(
        adapter_id="uniswap-v3",
        chain_id=CHAIN,
        address=V3_MANAGER,
        category="amm-nft-manager",
    )
    ctx = ResolveContext(
        chain_id=CHAIN, address=VITALIK, reader=reader, block_number=BLOCK
    )
    return UniswapV3Adapter().resolve(ctx, ContractSet.of(descriptor))


def _run_aave(reader: ContractReader) -> list:
    adapter = MainnetAaveV3()
    contracts = adapter.discover(
        DiscoveryContext(chain_id=CHAIN, reader=reader)
    )
    ctx = ResolveContext(
        chain_id=CHAIN, address=ALICE, reader=reader, block_number=BLOCK
    )
    return adapter.resolve(ctx, contracts)


def _run_receipt(adapter, reader: ContractReader) -> list:
    contracts = adapter.discover(
        DiscoveryContext(chain_id=CHAIN, reader=reader)
    )
    ctx = ResolveContext(
        chain_id=CHAIN, address=VITALIK, reader=reader, block_number=BLOCK
    )
    return adapter.resolve(ctx, contracts)


def _run_lido(reader: ContractReader) -> list:
    return _run_receipt(LidoAdapter(), reader)


def _run_rocket_pool(reader: ContractReader) -> list:
    return _run_receipt(RocketPoolAdapter(), reader)


#: The six runs, in the order their call counts are pinned.
RUNS = (
    ("tokens", _run_tokens, TOKENS_STATE),
    ("uniswap-v2", _run_v2, V2_STATE),
    ("uniswap-v3", _run_v3, V3_STATE),
    ("aave-v3", _run_aave, AAVE_STATE),
    ("lido", _run_lido, LIDO_STATE),
    ("rocket-pool", _run_rocket_pool, RETH_STATE),
)


#: The same six runs as ``(name, run, expected_reads)``, for the wave-3 seam
#: audit, which drives each one through a reader that refuses anything but a
#: lowercase address and counts what it was asked.
SIX_LEGS = tuple(
    (name, run, expected)
    for (name, run, _state), expected in zip(RUNS, PER_RUN_CALLS, strict=True)
)


def _dict_reader() -> DictReader:
    """One dict-backed reader over all nineteen decoded reads."""
    return DictReader(DICT_READS)


def _both(run, state):
    """One run twice: through the fixture and through the dict control."""
    matcher = _matcher()
    through_cassette = run(_cassette_reader(matcher))
    through_dict = run(DictReader(state))
    return through_cassette, through_dict, matcher


def _shape(position) -> tuple:
    return tuple(
        (u.meta_type, u.asset_id, u.quantity) for u in position.underlyings
    )


# pins: the committed fixture is a valid cassette in the shipped format and
#       declares in a top-level note that it was hand-authored, not recorded.
def test_the_fixture_is_a_valid_cassette_declaring_itself_hand_authored():
    assert load(CASSETTE) is not None
    document = _cassette_document()
    note = document["note"]
    assert "HAND-AUTHORED" in note
    assert "NOT RECORDED" in note
    assert "separate human task" in note


# pins: the module tells its reader, in its FIRST paragraph, that the fixture
#       is authored rather than recorded and that mainnet agreement is not
#       proven here. Deleting the disclaimer to quieten the claim goes red.
def test_the_docstring_refuses_to_claim_archive_node_agreement():
    # Whitespace-normalised: these sentences wrap, and a line break inside one
    # is not a change of meaning.
    first = " ".join(__doc__.split("\n\n")[1].split())
    assert "HAND-AUTHORED" in first
    assert "never recorded from a node" in first
    assert "does NOT prove agreement with a real archive node" in first
    assert "block 20,450,000" in first
    assert "separate human task" in first


# pins: the committed fixture is data read from disk, never regenerated by
#       the test run, so a vector cannot be produced by the code under test.
def test_the_fixture_is_committed_data_and_nothing_here_writes_it():
    assert CASSETTE.exists()
    # Over the AST, not over the raw text: a substring scan for the names of
    # writing calls would match the scan's own needles and always fail.
    called: set[str] = set()
    for node in ast.walk(ast.parse(Path(__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Attribute):
                called.add(function.attr)
            elif isinstance(function, ast.Name):
                called.add(function.id)
    writers = called & {"write_text", "write_bytes", "dump", "open", "Recorder"}
    assert not writers, f"this module must not call {sorted(writers)}"


# pins: the fixture holds exactly nineteen reads and no two share a key, so
#       positional replay could not paper over a duplicate.
def test_the_fixture_holds_nineteen_distinct_interactions():
    document = _cassette_document()
    assert len(document["interactions"]) == INTERACTION_COUNT
    keys = [
        _rpc_key(interaction["request"]["body"])
        for interaction in document["interactions"]
    ]
    assert len(set(keys)) == INTERACTION_COUNT
    assert set(_matcher().responses) == set(keys)


# pins: the block pin 20,450,000 is in the wire bytes of every recorded call,
#       not only in a Python constant the reader could ignore.
def test_every_recorded_body_carries_the_pinned_block_tag():
    document = _cassette_document()
    tags = []
    for interaction in document["interactions"]:
        body = interaction["request"]["body"]
        assert body["method"] == "eth_call"
        assert body["jsonrpc"] == "2.0"
        target, tag = body["params"]
        assert set(target) == {"to", "data"}
        assert target["to"] == target["to"].lower()
        tags.append(tag)
    assert tags == [BLOCK_TAG] * INTERACTION_COUNT
    assert hex(BLOCK) == BLOCK_TAG


# pins: keccak256 of the empty string is the published vector, named in the
#       phase Done-when, so a swap to hashlib's sha3_256 goes red here.
def test_keccak_of_the_empty_string_is_the_published_vector():
    assert keccak256(b"").hex() == EMPTY_KECCAK


# pins: the concrete reader satisfies the adapter seam structurally, which is
#       the binding the layering gate forbids proving by import.
def test_the_cassette_backed_reader_is_a_contract_reader():
    reader = _cassette_reader(_matcher())
    assert isinstance(reader, ContractReader)
    assert isinstance(reader, EvmContractReader)


# pins: the three ERC-20 helpers read the pinned pair state through the wire
#       path and issue exactly one eth_call each.
def test_tokens_helpers_read_the_pinned_pair_state():
    through_cassette, through_dict, matcher = _both(_run_tokens, TOKENS_STATE)
    assert through_cassette == through_dict
    assert through_cassette == (LP_BALANCE, 18, LP_TOTAL_SUPPLY)
    assert through_cassette[0] == 850_000_000_000_000
    assert through_cassette[2] == 850_000_000_000_000_000
    assert matcher.total() == 3


# pins: the V2 adapter's output through the fixture is the SAME object as
#       through a dict reader carrying the identical integers.
def test_uniswap_v2_agrees_with_the_dict_reader():
    through_cassette, through_dict, _ = _both(_run_v2, V2_STATE)
    assert through_cassette == through_dict


# pins: the V2 pro-rata at the pinned block, ids and quantities verbatim, so
#       the equality above cannot pass by both readers being wrong together.
def test_uniswap_v2_pinned_position():
    through_cassette, _, matcher = _both(_run_v2, V2_STATE)
    (position,) = through_cassette
    assert position.id == V2_POSITION_ID
    assert position.group_id == V2_GROUP_ID
    assert _shape(position) == (
        (MetaType.SUPPLIED, USDC_ID, Quantity(V2_USDC_RAW, 6)),
        (MetaType.SUPPLIED, WETH_ID, Quantity(V2_WETH_RAW, 18)),
    )
    for underlying in position.underlyings:
        assert underlying.price is None
        assert underlying.value is None
    assert position.value is None
    assert matcher.total() == 5


# pins: the V3 adapter's output through the fixture is the SAME object as
#       through a dict reader carrying the identical integers.
def test_uniswap_v3_agrees_with_the_dict_reader():
    through_cassette, through_dict, _ = _both(_run_v3, V3_STATE)
    assert through_cassette == through_dict


# pins: an address word decodes to LOWERCASE hex AT THE READER BOUNDARY.
#       This has to be asserted on the decoded value directly. The order for
#       this phase says the pinned V3 group id proves it, because getPool's
#       result feeds group_id_for, and that is not so: group_id_for runs its
#       group key through _lower_0x (positions/models.py:249), so an uppercase
#       decode still yields grp_9b813f4a0ae43e5b and every other consumer of a
#       decoded address re-lowercases too. Mutating abi._decode_word to emit
#       uppercase left the whole gate green until this test existed.
def test_a_returned_address_decodes_to_lowercase():
    reader = _cassette_reader(_matcher())
    pool = reader.call(V3_FACTORY, "getPool", (USDC, WETH, 3000))
    assert pool == V3_POOL
    assert pool == pool.lower()
    manager_position = reader.call(V3_MANAGER, "positions", (NFT_TOKEN_ID,))
    assert manager_position[2] == USDC
    assert manager_position[3] == WETH


# pins: the V3 TickMath amounts, the tick range and the pool-derived group id
#       at the pinned block.
def test_uniswap_v3_pinned_position():
    through_cassette, _, matcher = _both(_run_v3, V3_STATE)
    (position,) = through_cassette
    assert position.id == V3_POSITION_ID
    assert position.group_id == V3_GROUP_ID
    assert position.range == Range(193320, 195480, True)
    assert _shape(position) == (
        (MetaType.SUPPLIED, USDC_ID, Quantity(V3_USDC_RAW, 6)),
        (MetaType.SUPPLIED, WETH_ID, Quantity(V3_WETH_RAW, 18)),
        (MetaType.CLAIMABLE, USDC_ID, Quantity(125_000_000, 6)),
        (MetaType.CLAIMABLE, WETH_ID, Quantity(40_000_000_000_000_000, 18)),
    )
    for underlying in position.underlyings:
        assert underlying.price is None
        assert underlying.value is None
    assert matcher.total() == 7


# pins: the Aave discover-then-resolve pass through the fixture is the SAME
#       object as through a dict reader carrying the identical integers.
def test_aave_agrees_with_the_dict_reader():
    through_cassette, through_dict, _ = _both(_run_aave, AAVE_STATE)
    assert through_cassette == through_dict


# pins: the two Aave positions at the pinned block, supply before borrow,
#       both on the Pool's group, with the zero-balance reserves dropped.
def test_aave_pinned_positions():
    through_cassette, _, matcher = _both(_run_aave, AAVE_STATE)
    assert [p.id for p in through_cassette] == [AAVE_SUPPLY_ID, AAVE_BORROW_ID]
    assert {p.group_id for p in through_cassette} == {AAVE_GROUP_ID}
    supply, borrow = through_cassette
    assert _shape(supply) == (
        (MetaType.SUPPLIED, ETH_ID, Quantity(AWETH_BALANCE, 18)),
    )
    assert _shape(borrow) == (
        (MetaType.BORROWED, USDC_ID, Quantity(DEBT_USDC_BALANCE, 6)),
    )
    assert matcher.total() == 5


# pins: the Aave risk scaling, health_factor at 18 decimals and ltv at 4,
#       carried as exact Decimals on the FIRST position only.
def test_aave_group_info_scaling_on_the_first_position_only():
    through_cassette, _, matcher = _both(_run_aave, AAVE_STATE)
    infos = [p.group_info for p in through_cassette if p.group_info is not None]
    assert len(infos) == 1
    assert through_cassette[0].group_info is not None
    (info,) = infos
    assert isinstance(info.health_factor, Decimal)
    assert info.health_factor == Decimal("5.8125")
    assert isinstance(info.ltv, Decimal)
    assert info.ltv == Decimal("0.8")
    assert info.liquidation_price is None
    # Exactly one getUserAccountData on the wire, found by its selector in the
    # served calldata: the Pool is the risk unit and is read once per refresh.
    account_reads = [key for key in matcher.served if "bf92857c" in key[1]]
    assert len(account_reads) == 1
    assert matcher.served[account_reads[0]] == 1


# pins: Lido's output through the fixture is the SAME object as through a
#       dict reader carrying the identical integers.
def test_lido_agrees_with_the_dict_reader():
    through_cassette, through_dict, _ = _both(_run_lido, LIDO_STATE)
    assert through_cassette == through_dict


# pins: a rebasing 1:1 receipt takes the identity rate, so the balance IS the
#       claim and NO rate call goes out. A second call here is the defect.
def test_lido_pinned_position_makes_no_rate_call():
    through_cassette, _, matcher = _both(_run_lido, LIDO_STATE)
    (position,) = through_cassette
    assert position.id == LIDO_POSITION_ID
    assert position.group_id == LIDO_GROUP_ID
    assert _shape(position) == (
        (MetaType.SUPPLIED, ETH_ID, Quantity(STETH_BALANCE, 18)),
    )
    assert matcher.total() == 1


# pins: Rocket Pool's output through the fixture is the SAME object as
#       through a dict reader carrying the identical integers.
def test_rocket_pool_agrees_with_the_dict_reader():
    through_cassette, through_dict, _ = _both(_run_rocket_pool, RETH_STATE)
    assert through_cassette == through_dict


# pins: the pinned floor redemption share_raw * rate_raw // 10**18, reached
#       by a second call that is the rate read on the receipt contract.
def test_rocket_pool_pinned_redemption_reads_the_rate():
    through_cassette, _, matcher = _both(_run_rocket_pool, RETH_STATE)
    (position,) = through_cassette
    assert position.id == RETH_POSITION_ID
    assert position.group_id == RETH_GROUP_ID
    assert _shape(position) == (
        (MetaType.SUPPLIED, ETH_ID, Quantity(RETH_REDEEMED_RAW, 18)),
    )
    assert RETH_BALANCE * RETH_RATE // 10**18 == RETH_REDEEMED_RAW
    assert matcher.total() == 2
    # The second call is the rate read, found by getExchangeRate's selector.
    rate_calls = [key for key in matcher.served if "e6aa216c" in key[1]]
    assert len(rate_calls) == 1
    assert matcher.served[rate_calls[0]] == 1


# pins: an unrecorded read is refused by name and never served by a
#       neighbouring entry, which is what positional replay would do.
def test_an_unrecorded_read_raises_cassette_miss():
    matcher = _matcher()
    reader = _cassette_reader(matcher)
    with pytest.raises(CassetteMissError) as caught:
        reader.call(REVERTER, "decimals", ())
    assert "phase11_reader.json" in str(caught.value)
    assert matcher.total() == 0


# pins: across all six runs the reader issues exactly 23 calls with the pinned
#       per-run split, so an extra read anywhere moves a number here.
def test_the_six_runs_issue_exactly_the_pinned_call_counts():
    matcher = _matcher()
    reader = _cassette_reader(matcher)
    counts = []
    before = 0
    for _name, run, _state in RUNS:
        run(reader)
        counts.append(matcher.total() - before)
        before = matcher.total()
    assert tuple(counts) == PER_RUN_CALLS
    assert sum(counts) == TOTAL_CALLS
    assert matcher.total() == TOTAL_CALLS


# pins: DICT_READS states the decoded meaning of every recorded read, one for
#       one, and it is the table the six runs actually resolve against. An
#       entry with no recorded call, or a call with no entry, is the drift.
def test_the_decoded_read_table_covers_the_fixture_exactly():
    assert len(DICT_READS) == INTERACTION_COUNT
    assert len(FIXTURE) == INTERACTION_COUNT
    assert {address for address, _fn, _args in DICT_READS} == {
        to for to, _data, _result in FIXTURE
    }
    # A single-return function stores a bare scalar and a multi-return one a
    # tuple: that asymmetry IS the reader's length-1 unwrap, seen from data.
    assert DICT_READS[(V2_PAIR, "decimals", ())] == 18
    assert DICT_READS[(V2_PAIR, "getReserves", ())] == RESERVES
    assert DICT_READS[(V3_POOL, "slot0", ())][6] is True
    assert DICT_READS[(V3_MANAGER, "positions", (NFT_TOKEN_ID,))][1] == ZERO_ADDRESS
    # Six legs, one per run, carrying the counts the gate pins.
    assert [expected for _n, _r, expected in SIX_LEGS] == list(PER_RUN_CALLS)
    assert _dict_reader().call(V2_PAIR, "decimals", ()) == 18


# pins: the two hand-laid Multicall3 payloads keep the layout the seam audit
#       re-derives: the aggregate3 selector is INCLUDED in the calldata, the
#       declared element offsets are running totals and not a fixed stride,
#       and the reverting leg's Error(string) body is 100 bytes.
def test_the_hand_laid_multicall_payloads_are_well_formed():
    assert FIVE_CALL_CALLDATA.startswith("0x82ad56cb")
    assert (len(FIVE_CALL_CALLDATA) - 2 - 8) % 64 == 0
    # Offsets 160, 320, 512, 672, 832: the gap after the second element is 192
    # rather than 160, because its calldata is 36 bytes and not 4.
    body = FIVE_CALL_CALLDATA[2 + 8:]
    offsets = [int(body[64 * (2 + i):64 * (3 + i)], 16) for i in range(5)]
    assert offsets == [160, 320, 512, 672, 832]
    for payload in (FIVE_CALL_RESULT, FIVE_CALL_RESULT_WITH_PAYLOAD):
        assert (len(payload) - 2) % 64 == 0
        assert int(payload[2:66], 16) == 0x20
        assert int(payload[66:130], 16) == 5
    # The Error(string) leg is the ONLY difference between the two payloads,
    # and it moves the last offset because its element grew.
    plain_last = int(FIVE_CALL_RESULT[130 + 64 * 4:130 + 64 * 5], 16)
    payload_last = int(
        FIVE_CALL_RESULT_WITH_PAYLOAD[130 + 64 * 4:130 + 64 * 5], 16
    )
    assert (plain_last, payload_last) == (640, 768)
    assert REVERT_PAYLOAD.startswith("08c379a0")
    assert len(REVERT_PAYLOAD) // 2 == 100
    assert bytes.fromhex(REVERT_PAYLOAD[-64:]).rstrip(b"\x00") == b"no decimals"
    assert REVERT_PAYLOAD in FIVE_CALL_RESULT_WITH_PAYLOAD


# pins: no recorded interaction is dead weight. Every one of the nineteen was
#       asked for, which is what proves the reader's calldata is byte-for-byte
#       what was authored: a wrong selector or a wrong word would miss instead.
def test_every_recorded_interaction_is_served_at_least_once():
    matcher = _matcher()
    reader = _cassette_reader(matcher)
    for _name, run, _state in RUNS:
        run(reader)
    assert matcher.dead() == []
    assert len(matcher.served) == INTERACTION_COUNT
