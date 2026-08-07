"""Contract tests for the concrete ContractReader (RELEASE_0.2.0 §4).

Every request here is served by an ``httpx.MockTransport``, so the autouse
socket guard in ``tests/conftest.py`` stays armed and no cassette is
needed: what these tests pin is the calldata this module authors, the
tuple it decodes back, and the six ways it refuses.

THE HEADLINE PIN is the binding. ``tests/style/test_layering.py`` forbids
``sources`` from importing the domain that declares ``ContractReader``, so
the reader matches the protocol by SHAPE. A test may import both sides,
and this one does: it asserts the runtime_checkable ``isinstance`` and
then asserts, over the module's own source text, that the edge the gate
forbids was not created to earn it.

THE SECOND PIN is registry coverage, and it is mechanical for a reason.
Reading the call sites by eye is how ``getExchangeRate`` goes missing:
``adapters/tokens.py`` calls ``reader.call(address, receipt.rate_fn, ())``
with a NON-LITERAL function name, so the only place the string appears is
the fourth field of a ``ReceiptToken`` declaration. The collector below
walks both shapes, and one of its assertions proves the second shape is
load-bearing by showing the first alone misses the name.

GOLDEN VECTORS. Every selector below is ``keccak256(signature)[:4]``,
derived with a standalone keccak that imports nothing from this package
and hardcoded here:

    balanceOf(address)                    70a08231
    decimals()                            313ce567
    totalSupply()                         18160ddd
    token0()                              0dfe1681
    token1()                              d21220a7
    getReserves()                         0902f1ac
    allPairsLength()                      574f2ba3
    allPairs(uint256)                     1e3dd18b
    slot0()                               3850c7bd
    positions(uint256)                    99fbab88
    getPool(address,address,uint24)       1698ee82
    tokenOfOwnerByIndex(address,uint256)  2f745c59
    getUserAccountData(address)           bf92857c
    getExchangeRate()                     e6aa216c
    getSomeNewRate()                      0f2239f7

The last one is not a real function anywhere. It is the open shape: a
name the registry has never heard of, called with no arguments, which
must resolve as ``() -> uint256`` so a host can declare a new receipt
token without editing this package. Its result word is
``0xffff...ffff``, the largest value a uint256 holds, chosen because it
decodes to ``2**256 - 1`` under uint256 and under nothing else: as
int256 it is -1, as uint128 or address or bool it does not fit at all.

Result words are packed from the block-20,450,000 integers the phase-4
goldens already pin, so this file and
``tests/golden/test_phase11_reader.py`` agree word for word:
850,000,000,000,000 LP units is ``0x305120c0f2000``, the pair's supply
850,000,000,000,000,000 is ``0xbcbce7f1b150000``, the reserves are
``0x2f4b31874000`` and ``0x3120bec57b51c100000`` at timestamp
``0x66aace70``, the V3 ticks 193320 and 195480 are ``0x2f328`` and
``0x2fb98``, token id 912345 is ``0xdebd9``, and the rETH rate 1.12 at
18 decimals is ``0xf8b0a10e4700000``.
"""

from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest

from auradefi.errors import AuradefiError, SourceError, ValidationError

# A test may import both sides of the seam; the module under test may not.
# That asymmetry IS the binding, and the source-text assertion below is the
# other half of proving it.
from auradefi.positions.protocol import ContractReader
from auradefi.sources.evm.codec.abi import selector
from auradefi.sources.evm.reader import (
    DEFAULT_RETURN_TYPES,
    SIGNATURES,
    EvmContractReader,
)
from auradefi.sources.evm.rpc import EvmRpc

REPO = Path(__file__).resolve().parents[3]
READER_MODULE = REPO / "src" / "auradefi" / "sources" / "evm" / "reader.py"
POSITIONS_ROOT = REPO / "src" / "auradefi" / "positions"

URL = "https://node.example.invalid/v1"

BLOCK = 20_450_000
BLOCK_TAG = "0x1380ad0"

VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
VITALIK_MIXED = "0xD8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
ALICE = "0x00000000000000000000000000000000000a11ce"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
V2_PAIR = "0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc"
V2_FACTORY = "0x5c69bee701ef814a2b6a3edd4b1652cb9cc5aa6f"
V3_MANAGER = "0xc36442b4a4522e871399cd717abdd847ab11fe88"
V3_FACTORY = "0x1f98431c8ad98523631ae4a59f267346ea31f984"
V3_POOL = "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8"
AAVE_POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
RETH = "0xae78736cd615f374d3085123a210448e74fc6393"

# --- words, 64 hex characters each, packed by hand ------------------------

ZERO_WORD = "0" * 64
ONE_WORD = "0" * 63 + "1"
TWO_WORD = "0" * 63 + "2"
SIX_WORD = "0" * 63 + "6"
EIGHTEEN_WORD = "0" * 62 + "12"
MAX_UINT256_WORD = "f" * 64

W_VITALIK = "000000000000000000000000d8da6bf26964af9d7eed9e03e53415d37aa96045"
W_ALICE = "00000000000000000000000000000000000000000000000000000000000a11ce"
W_USDC = "000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
W_WETH = "000000000000000000000000c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
W_V2_PAIR = "000000000000000000000000b4e16d0168e52d35cacd2c6185b44281ec28c9dc"
W_V3_POOL = "0000000000000000000000008ad599c3a0ff1de082011efddc58f1908eb6e6d8"

W_FEE_3000 = "0000000000000000000000000000000000000000000000000000000000000bb8"
W_TOKEN_ID = "00000000000000000000000000000000000000000000000000000000000debd9"
W_THREE = "0" * 63 + "3"

W_LP_BALANCE = "000000000000000000000000000000000000000000000000000305120c0f2000"
W_TOTAL_SUPPLY = "0000000000000000000000000000000000000000000000000bcbce7f1b150000"
W_RESERVE0 = "00000000000000000000000000000000000000000000000000002f4b31874000"
W_RESERVE1 = "0000000000000000000000000000000000000000000003120bec57b51c100000"
W_BLOCK_STAMP = "0000000000000000000000000000000000000000000000000000000066aace70"
W_PAIR_COUNT = "0000000000000000000000000000000000000000000000000000000000060c7b"
W_SQRT_PRICE = "00000000000000000000000000000000000041397e2a57a10fe7d84be28fb3c6"
W_TICK_CURRENT = "000000000000000000000000000000000000000000000000000000000002f7a6"
W_TICK_LOWER = "000000000000000000000000000000000000000000000000000000000002f328"
W_TICK_UPPER = "000000000000000000000000000000000000000000000000000000000002fb98"
W_LIQUIDITY = "00000000000000000000000000000000000000000000000000071afd498d0000"
W_OWED0 = "0000000000000000000000000000000000000000000000000000000007735940"
W_OWED1 = "000000000000000000000000000000000000000000000000008e1bc9bf040000"
W_COLLATERAL = "0000000000000000000000000000000000000000000000000000034285f2b280"
W_DEBT = "000000000000000000000000000000000000000000000000000000746a528800"
W_AVAILABLE = "0000000000000000000000000000000000000000000000000000022734093a00"
W_THRESHOLD = "000000000000000000000000000000000000000000000000000000000000203a"
W_LTV = "0000000000000000000000000000000000000000000000000000000000001f40"
W_HEALTH = "00000000000000000000000000000000000000000000000050aa25f43cf54000"
W_RETH_RATE = "0000000000000000000000000000000000000000000000000f8b0a10e4700000"

#: An address word carrying a byte above its twenty: dirt no address holds.
W_DIRTY_ADDRESS = "010000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

#: A uint112 word with bit 112 set, which no reserve can reach.
W_OVER_WIDE_UINT112 = "0000000000000000000000000000000000010000000000000000000000000000"

SLOT0_RESULT = (
    W_SQRT_PRICE
    + W_TICK_CURRENT
    + ZERO_WORD
    + ONE_WORD
    + ONE_WORD
    + ZERO_WORD
    + ONE_WORD
)

SLOT0_VALUE = (1322911675800610514020464994530246, 194470, 0, 1, 1, 0, True)

POSITIONS_RESULT = (
    ZERO_WORD
    + ZERO_WORD
    + W_USDC
    + W_WETH
    + W_FEE_3000
    + W_TICK_LOWER
    + W_TICK_UPPER
    + W_LIQUIDITY
    + ZERO_WORD
    + ZERO_WORD
    + W_OWED0
    + W_OWED1
)

POSITIONS_VALUE = (
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

ACCOUNT_DATA_RESULT = (
    W_COLLATERAL + W_DEBT + W_AVAILABLE + W_THRESHOLD + W_LTV + W_HEALTH
)

ACCOUNT_DATA_VALUE = (
    3_584_250_000_000,
    500_000_000_000,
    2_367_400_000_000,
    8250,
    8000,
    5_812_500_000_000_000_000,
)

#: The registry, restated as a literal. `SIGNATURES` must equal this
#: exactly: `rate_fn` is host data and is NOT a key, `getExchangeRate` is.
EXPECTED_SIGNATURES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "balanceOf": (("address",), ("uint256",)),
    "decimals": ((), ("uint8",)),
    "totalSupply": ((), ("uint256",)),
    "token0": ((), ("address",)),
    "token1": ((), ("address",)),
    "getReserves": ((), ("uint112", "uint112", "uint32")),
    "allPairsLength": ((), ("uint256",)),
    "allPairs": (("uint256",), ("address",)),
    "slot0": (
        (),
        ("uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"),
    ),
    "positions": (
        ("uint256",),
        (
            "uint96", "address", "address", "address", "uint24", "int24",
            "int24", "uint128", "uint256", "uint256", "uint128", "uint128",
        ),
    ),
    "getPool": (("address", "address", "uint24"), ("address",)),
    "tokenOfOwnerByIndex": (("address", "uint256"), ("uint256",)),
    "getUserAccountData": (("address",), ("uint256",) * 6),
    "getExchangeRate": ((), ("uint256",)),
}

#: Every declared row, as (fn, address, args, calldata, result, decoded).
#: The calldata and the result are hardcoded hex; the decoded value is what
#: the reader must hand back, unwrapped for a single return type.
ROWS: tuple[tuple[str, str, tuple[object, ...], str, str, object], ...] = (
    (
        "balanceOf", V2_PAIR, (VITALIK,),
        "0x70a08231" + W_VITALIK,
        "0x" + W_LP_BALANCE,
        850_000_000_000_000,
    ),
    ("decimals", USDC, (), "0x313ce567", "0x" + SIX_WORD, 6),
    (
        "totalSupply", V2_PAIR, (),
        "0x18160ddd",
        "0x" + W_TOTAL_SUPPLY,
        850_000_000_000_000_000,
    ),
    ("token0", V2_PAIR, (), "0x0dfe1681", "0x" + W_USDC, USDC),
    ("token1", V2_PAIR, (), "0xd21220a7", "0x" + W_WETH, WETH),
    (
        "getReserves", V2_PAIR, (),
        "0x0902f1ac",
        "0x" + W_RESERVE0 + W_RESERVE1 + W_BLOCK_STAMP,
        (52_000_000_000_000, 14_500_000_000_000_000_000_000, 1_722_470_000),
    ),
    (
        "allPairsLength", V2_FACTORY, (),
        "0x574f2ba3",
        "0x" + W_PAIR_COUNT,
        396_411,
    ),
    (
        "allPairs", V2_FACTORY, (3,),
        "0x1e3dd18b" + W_THREE,
        "0x" + W_V2_PAIR,
        V2_PAIR,
    ),
    ("slot0", V3_POOL, (), "0x3850c7bd", "0x" + SLOT0_RESULT, SLOT0_VALUE),
    (
        "positions", V3_MANAGER, (912345,),
        "0x99fbab88" + W_TOKEN_ID,
        "0x" + POSITIONS_RESULT,
        POSITIONS_VALUE,
    ),
    (
        "getPool", V3_FACTORY, (USDC, WETH, 3000),
        "0x1698ee82" + W_USDC + W_WETH + W_FEE_3000,
        "0x" + W_V3_POOL,
        V3_POOL,
    ),
    (
        "tokenOfOwnerByIndex", V3_MANAGER, (VITALIK, 0),
        "0x2f745c59" + W_VITALIK + ZERO_WORD,
        "0x" + W_TOKEN_ID,
        912345,
    ),
    (
        "getUserAccountData", AAVE_POOL, (ALICE,),
        "0xbf92857c" + W_ALICE,
        "0x" + ACCOUNT_DATA_RESULT,
        ACCOUNT_DATA_VALUE,
    ),
    (
        "getExchangeRate", RETH, (),
        "0xe6aa216c",
        "0x" + W_RETH_RATE,
        1_120_000_000_000_000_000,
    ),
)

#: Nothing was returned. Distinguishes "raised" from "returned None".
UNSET = object()


class Node:
    """A scripted JSON-RPC node over ``httpx.MockTransport``.

    Each entry of ``results`` is a result STRING the node answers with, or
    a full envelope dict for the error cases. The last entry repeats. A
    node scripted with nothing refuses to be called at all, which is how
    "zero requests" is asserted as a hard failure and not as a count.
    """

    def __init__(self, *results: object) -> None:
        self.requests: list[dict] = []
        self._results = results

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        assert self._results, f"the node was not expecting a request: {body!r}"
        spec = self._results[min(len(self.requests) - 1, len(self._results) - 1)]
        if isinstance(spec, dict):
            return httpx.Response(200, json=spec)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": spec})

    @property
    def calldata(self) -> list[str]:
        return [body["params"][0]["data"] for body in self.requests]

    @property
    def targets(self) -> list[str]:
        return [body["params"][0]["to"] for body in self.requests]

    @property
    def tags(self) -> list[str]:
        return [body["params"][1] for body in self.requests]


def _reader(node: Node, block_number: int | None = BLOCK) -> EvmContractReader:
    """The reader under test, over ``node``, pinned at ``block_number``."""
    return EvmContractReader(EvmRpc(node.client(), URL), block_number=block_number)


def _literal_at(node: ast.Call, index: int) -> set[str]:
    """The string literal at positional ``index``, or nothing."""
    if len(node.args) <= index:
        return set()
    argument = node.args[index]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return {argument.value}
    return set()


def _collect(with_receipts: bool = True) -> set[str]:
    """Every on-chain function name the shipped adapters name literally.

    Two shapes, and both are needed. ``reader.call(addr, "slot0")`` puts
    the name second in a call to an attribute named ``call``.
    ``ReceiptToken(addr, caip19, 18, "getExchangeRate")`` puts it fourth in
    a construction, and that is the ONLY place the rETH rate function is
    spelled out: the read itself passes ``receipt.rate_fn``, an attribute.
    """
    names: set[str] = set()
    for path in sorted(POSITIONS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Attribute) and function.attr == "call":
                names |= _literal_at(node, 1)
            constructed = ""
            if isinstance(function, ast.Attribute):
                constructed = function.attr
            elif isinstance(function, ast.Name):
                constructed = function.id
            if with_receipts and constructed == "ReceiptToken":
                names |= _literal_at(node, 3)
    return names


THE_FOURTEEN = frozenset(
    {
        "balanceOf", "decimals", "totalSupply", "token0", "token1",
        "getReserves", "allPairsLength", "allPairs", "slot0", "positions",
        "getPool", "tokenOfOwnerByIndex", "getUserAccountData",
        "getExchangeRate",
    }
)


# --- the binding ----------------------------------------------------------


def test_the_reader_binds_the_contract_reader_protocol_structurally() -> None:
    # pins: the concrete reader satisfies the adapter seam by shape, so an
    #       adapter can be handed one without this package's layers meeting.
    node = Node("0x" + SIX_WORD)
    reader = _reader(node)
    assert isinstance(reader, ContractReader) is True
    # The control: the transport underneath has no `call`, so the check is
    # discriminating and not a protocol that accepts anything.
    assert isinstance(EvmRpc(node.client(), URL), ContractReader) is False
    assert node.requests == []


def test_the_reader_module_never_names_the_positions_package() -> None:
    # pins: the binding stays structural. No import edge is created to earn
    #       the isinstance above, at module scope, in a body, or under
    #       TYPE_CHECKING, all three of which the layering gate forbids.
    text = READER_MODULE.read_text(encoding="utf-8")
    assert "auradefi.positions" not in text
    assert "from auradefi import positions" not in text


def test_the_call_signature_matches_the_protocol_exactly() -> None:
    # pins: parameter NAMES, their order and the args default are contract,
    #       because a structural bind has nothing else to hold it. Rename
    #       `fn` to `function` and every keyword call site breaks silently.
    mine = inspect.signature(EvmContractReader.call)
    theirs = inspect.signature(ContractReader.call)
    assert list(mine.parameters) == ["self", "address", "fn", "args"]
    assert list(mine.parameters) == list(theirs.parameters)
    assert mine.parameters["args"].default == ()
    assert [p.kind for p in mine.parameters.values()] == [
        p.kind for p in theirs.parameters.values()
    ]


def test_there_is_no_per_call_block_argument() -> None:
    # pins: the block pin is fixed at CONSTRUCTION. A reader that grew a
    #       per-call block parameter would no longer match the protocol,
    #       whose `call` has no way to express one.
    assert "block" not in inspect.signature(EvmContractReader.call).parameters
    constructor = inspect.signature(EvmContractReader.__init__)
    assert list(constructor.parameters) == ["self", "rpc", "block_number"]
    assert constructor.parameters["block_number"].default is None


# --- the registry ---------------------------------------------------------


def test_the_registry_is_exactly_the_declared_call_surface() -> None:
    # pins: every row's argument types and return types, as data. Swap
    #       uint112 for uint256 in getReserves and the table stops matching.
    assert isinstance(SIGNATURES, Mapping)
    assert dict(SIGNATURES) == EXPECTED_SIGNATURES
    assert set(SIGNATURES) == THE_FOURTEEN


def test_the_registry_knows_every_function_the_adapters_call() -> None:
    # pins: a reader whose registry misses a shipped call site is a startup
    #       failure, and the check is mechanical over the real call sites.
    collected = _collect()
    assert collected, "the collector found no call sites, so this gate is blind"
    assert THE_FOURTEEN <= collected
    assert collected <= set(SIGNATURES)


def test_the_rate_function_is_keyed_by_its_on_chain_name() -> None:
    # pins: `rate_fn` is host data and never a key. The Rocket Pool leg
    #       reads getExchangeRate, so THAT is the name the registry holds.
    assert "rate_fn" not in SIGNATURES
    assert "getExchangeRate" in SIGNATURES
    assert SIGNATURES["getExchangeRate"] == ((), ("uint256",))
    assert selector("getExchangeRate()").hex() == "e6aa216c"


def test_the_rate_function_name_is_reachable_only_through_the_receipt_row() -> None:
    # pins: the collector's second shape is load-bearing. The read passes
    #       `receipt.rate_fn`, an attribute, so a collector that walked only
    #       reader.call() would pass while missing the one name the literal
    #       table gets wrong.
    assert "getExchangeRate" not in _collect(with_receipts=False)
    assert "getExchangeRate" in _collect()
    assert "rate_fn" not in _collect()


# --- calldata and decoding ------------------------------------------------


@pytest.mark.parametrize(
    ("fn", "address", "args", "calldata", "result", "expected"),
    ROWS,
    ids=[row[0] for row in ROWS],
)
def test_a_registry_row_posts_its_calldata_and_decodes_its_result(
    fn: str,
    address: str,
    args: tuple[object, ...],
    calldata: str,
    result: str,
    expected: object,
) -> None:
    # pins: each row's selector and argument words go on the wire byte for
    #       byte, and the declared return types decode the answer back.
    node = Node(result)
    assert _reader(node).call(address, fn, args) == expected
    assert node.calldata == [calldata]
    assert node.targets == [address]


def test_balance_of_posts_the_pinned_calldata() -> None:
    # pins: the acceptance vector, spelled out. Selector 70a08231 then the
    #       holder left-padded to a word, and nothing else.
    node = Node("0x" + W_LP_BALANCE)
    _reader(node).call(V2_PAIR, "balanceOf", (VITALIK,))
    assert node.calldata == [
        "0x70a08231"
        "000000000000000000000000d8da6bf26964af9d7eed9e03e53415d37aa96045"
    ]


def test_positions_posts_the_pinned_calldata() -> None:
    # pins: a uint256 argument occupies one word, so token id 912345 is
    #       0xdebd9 right-aligned and never a decimal string or a short word.
    node = Node("0x" + POSITIONS_RESULT)
    _reader(node).call(V3_MANAGER, "positions", (912345,))
    assert node.calldata == [
        "0x99fbab88"
        "00000000000000000000000000000000000000000000000000000000000debd9"
    ]


def test_a_single_return_type_is_unwrapped_and_a_longer_one_is_not() -> None:
    # pins: the length-1 unwrap lives here. abi.decode always returns a
    #       tuple, so a reader that forwards it hands adapters (n,) where
    #       they expect n, and int((n,)) is a TypeError inside the adapter.
    balance = _reader(Node("0x" + W_LP_BALANCE)).call(
        V2_PAIR, "balanceOf", (VITALIK,)
    )
    assert balance == 850_000_000_000_000
    assert isinstance(balance, int)
    assert not isinstance(balance, tuple)

    reserves = _reader(Node("0x" + W_RESERVE0 + W_RESERVE1 + W_BLOCK_STAMP)).call(
        V2_PAIR, "getReserves"
    )
    assert reserves == (
        52_000_000_000_000,
        14_500_000_000_000_000_000_000,
        1_722_470_000,
    )
    assert isinstance(reserves, tuple)


def test_slot0_decodes_seven_words_ending_in_a_python_bool() -> None:
    # pins: the tail of slot0 is uint16/uint16/uint16/uint8/bool, so the
    #       last word comes back as True and not as the integer 1. The V3
    #       adapter unpacks this tuple by position.
    slot0 = _reader(Node("0x" + SLOT0_RESULT)).call(V3_POOL, "slot0")
    assert slot0 == SLOT0_VALUE
    assert len(slot0) == 7
    assert slot0[-1] is True
    assert slot0[1] == 194470


def test_positions_decodes_twelve_words_with_addresses_and_int24_ticks() -> None:
    # pins: the twelve-word shape the V3 adapter unpacks. Members 1, 2 and 3
    #       are lowercase 0x addresses stripped of their twelve pad bytes,
    #       and the two ticks decode from int24 words.
    decoded = _reader(Node("0x" + POSITIONS_RESULT)).call(
        V3_MANAGER, "positions", (912345,)
    )
    assert decoded == POSITIONS_VALUE
    assert len(decoded) == 12
    for member in decoded[1:4]:
        assert member.startswith("0x")
        assert len(member) == 42
        assert member == member.lower()
    assert (decoded[5], decoded[6]) == (193320, 195480)


def test_get_pool_decodes_a_lowercase_address() -> None:
    # pins: the pool address comes back lowercase, which is what keeps the
    #       pinned V3 group id grp_9b813f4a0ae43e5b intact downstream.
    pool = _reader(Node("0x" + W_V3_POOL)).call(
        V3_FACTORY, "getPool", (USDC, WETH, 3000)
    )
    assert pool == "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8"
    assert pool == pool.lower()


def test_a_mixed_case_address_posts_the_same_bytes_as_the_lowercase_form() -> None:
    # pins: casing is not wire content. A checksummed target and a
    #       checksummed argument produce byte-identical calldata and a
    #       lowercased target, so one holder does not read as two.
    lower = Node("0x" + W_LP_BALANCE)
    _reader(lower).call(V2_PAIR, "balanceOf", (VITALIK,))
    mixed = Node("0x" + W_LP_BALANCE)
    _reader(mixed).call(V2_PAIR.upper().replace("0X", "0x"), "balanceOf",
                        (VITALIK_MIXED,))
    assert mixed.calldata == lower.calldata
    assert mixed.targets == lower.targets == [V2_PAIR]


def test_the_target_address_is_lowercased_before_it_reaches_the_transport() -> None:
    # pins: the reader hands `address.lower()` to eth_call, so the lowering
    #       is not left to the transport to do on the reader's behalf.
    seen: list[str] = []

    class RecordingRpc(EvmRpc):
        def eth_call(self, to: str, data: str, block: str = "latest") -> str:
            seen.append(to)
            return super().eth_call(to, data, block)

    node = Node("0x" + W_LP_BALANCE)
    reader = EvmContractReader(
        RecordingRpc(node.client(), URL), block_number=BLOCK
    )
    reader.call(V2_PAIR.upper().replace("0X", "0x"), "balanceOf", (VITALIK,))
    assert seen == [V2_PAIR]


# --- the open shape -------------------------------------------------------


def test_an_unknown_zero_argument_function_resolves_as_one_uint256() -> None:
    # pins: the declared open shape. A host that ships a new ReceiptToken
    #       rate_fn gets a working read with no registry edit, and the word
    #       decodes as uint256 rather than as any narrower or signed type.
    assert DEFAULT_RETURN_TYPES == ("uint256",)
    assert "getSomeNewRate" not in SIGNATURES
    node = Node("0x" + MAX_UINT256_WORD)
    rate = _reader(node).call(RETH, "getSomeNewRate", ())
    assert rate == (1 << 256) - 1
    assert node.calldata == ["0x0f2239f7"]
    assert node.calldata == ["0x" + selector("getSomeNewRate()").hex()]


def test_the_open_shape_does_not_grow_the_registry() -> None:
    # pins: SIGNATURES is DATA and stays the declared fourteen. A reader
    #       that memoised resolved names would turn a shared table into
    #       mutable global state that differs by what ran first.
    before = dict(SIGNATURES)
    _reader(Node("0x" + MAX_UINT256_WORD)).call(RETH, "getSomeNewRate", ())
    assert dict(SIGNATURES) == before
    assert "getSomeNewRate" not in SIGNATURES


def test_an_unknown_function_with_arguments_is_refused_before_any_http() -> None:
    # pins: the open shape is zero-argument ONLY. With arguments the codec
    #       would have to guess types, and a guessed selector reaches a
    #       function the contract does not have.
    node = Node()
    outcome: object = UNSET
    with pytest.raises(ValidationError, match="getSomeNewRate") as raised:
        outcome = _reader(node).call(RETH, "getSomeNewRate", (1,))
    assert outcome is UNSET
    assert node.requests == []
    assert isinstance(raised.value, AuradefiError)


def test_too_few_arguments_for_a_known_function_are_refused() -> None:
    # pins: arity is checked HERE, against the registry's arg_types, and the
    #       refusal names the function. The codec refuses a length mismatch
    #       too, but it has never heard of `balanceOf`, so the name in the
    #       message is what says the reader did its own check.
    node = Node()
    outcome: object = UNSET
    with pytest.raises(ValidationError, match="balanceOf"):
        outcome = _reader(node).call(V2_PAIR, "balanceOf", ())
    assert outcome is UNSET
    assert node.requests == []


def test_too_many_arguments_for_a_known_function_are_refused() -> None:
    # pins: the arity check is an inequality in BOTH directions. A `<`
    #       comparison lets a surplus argument fall through to the codec,
    #       which refuses it in terms of word counts and cannot name the
    #       function the caller actually got wrong.
    node = Node()
    outcome: object = UNSET
    with pytest.raises(ValidationError, match="balanceOf"):
        outcome = _reader(node).call(V2_PAIR, "balanceOf", (VITALIK, 1))
    assert outcome is UNSET
    assert node.requests == []


# --- block pinning --------------------------------------------------------


def test_a_pinned_reader_sends_the_pinned_tag_on_every_call() -> None:
    # pins: 20,450,000 travels as minimal lowercase hex on the params
    #       array's second member, on EVERY read and not only the first.
    node = Node("0x" + SIX_WORD, "0x" + EIGHTEEN_WORD)
    reader = _reader(node, block_number=BLOCK)
    assert reader.call(USDC, "decimals") == 6
    assert reader.call(WETH, "decimals") == 18
    assert node.tags == [BLOCK_TAG, BLOCK_TAG]
    assert node.tags == ["0x1380ad0", "0x1380ad0"]


def test_an_unpinned_reader_reads_latest() -> None:
    # pins: block_number=None is the string 'latest' and never 'None', '0x0'
    #       or an omitted member. Block zero is a real height, so the
    #       default cannot be expressed by falsiness.
    node = Node("0x" + SIX_WORD)
    assert _reader(node, block_number=None).call(USDC, "decimals") == 6
    assert node.tags == ["latest"]


def test_block_zero_is_a_height_and_not_the_latest_tag() -> None:
    # pins: the boundary. `if block_number:` would send 'latest' for the
    #       genesis block, answering a question nobody asked.
    node = Node("0x" + SIX_WORD)
    assert _reader(node, block_number=0).call(USDC, "decimals") == 6
    assert node.tags == ["0x0"]


def test_constructing_a_reader_performs_no_io() -> None:
    # pins: no network at construction. The node below refuses any request,
    #       so a constructor that probed the chain fails loudly.
    node = Node()
    reader = _reader(node)
    assert reader is not None
    assert node.requests == []


# --- the wire failure channel ---------------------------------------------


def test_a_node_error_reaches_the_caller_as_a_source_error() -> None:
    # pins: a reverting eth_call is a node error member, and it propagates
    #       as SourceError carrying the code and the message. Never a zero.
    node = Node(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "execution reverted"},
        }
    )
    outcome: object = UNSET
    with pytest.raises(SourceError) as raised:
        outcome = _reader(node).call(V2_PAIR, "balanceOf", (VITALIK,))
    assert outcome is UNSET
    assert "-32000" in str(raised.value)
    assert "execution reverted" in str(raised.value)
    assert isinstance(raised.value, AuradefiError)


def test_an_empty_result_where_a_word_was_expected_is_a_source_error() -> None:
    # pins: '0x' is what a call to a non-contract address returns. Reading
    #       it as zero would report an empty account as a zero balance and a
    #       missing contract as a real answer.
    node = Node("0x")
    outcome: object = UNSET
    with pytest.raises(SourceError):
        outcome = _reader(node).call(V2_PAIR, "balanceOf", (VITALIK,))
    assert outcome is UNSET


def test_a_short_result_is_a_source_error_and_not_a_left_padded_word() -> None:
    # pins: 31 bytes is not a word. Padding it up would read a truncated
    #       response as a value 256 times too small.
    node = Node("0x" + SIX_WORD[2:])
    outcome: object = UNSET
    with pytest.raises(SourceError):
        outcome = _reader(node).call(V2_PAIR, "balanceOf", (VITALIK,))
    assert outcome is UNSET


def test_a_result_of_the_wrong_word_count_is_a_source_error() -> None:
    # pins: getReserves declares three words, so two is malformed. Decoding
    #       what arrived would pair reserve0 with reserve1 as the timestamp.
    node = Node("0x" + W_RESERVE0 + W_RESERVE1)
    outcome: object = UNSET
    with pytest.raises(SourceError):
        outcome = _reader(node).call(V2_PAIR, "getReserves")
    assert outcome is UNSET


def test_an_address_word_with_dirty_high_bytes_is_a_source_error() -> None:
    # pins: an address is the low twenty bytes of a word, and dirt above
    #       them means the word is not an address. Truncating to the low
    #       twenty would invent a plausible token address from garbage.
    node = Node("0x" + W_DIRTY_ADDRESS)
    outcome: object = UNSET
    with pytest.raises(SourceError):
        outcome = _reader(node).call(V2_PAIR, "token0")
    assert outcome is UNSET


def test_a_bool_word_that_is_not_zero_or_one_is_a_source_error() -> None:
    # pins: slot0's last word is a bool. A word of 2 is malformed, and
    #       `bool(2)` would read it as an unlocked pool.
    result = (
        W_SQRT_PRICE
        + W_TICK_CURRENT
        + ZERO_WORD
        + ONE_WORD
        + ONE_WORD
        + ZERO_WORD
        + TWO_WORD
    )
    node = Node("0x" + result)
    outcome: object = UNSET
    with pytest.raises(SourceError):
        outcome = _reader(node).call(V3_POOL, "slot0")
    assert outcome is UNSET


def test_an_integer_too_wide_for_its_type_is_a_source_error() -> None:
    # pins: a uint112 reserve carrying a bit above 112 is malformed. Read
    #       as a uint256 it is a reserve no pair holds, and the pro rata
    #       share computed from it would be silently wrong.
    node = Node("0x" + W_OVER_WIDE_UINT112 + W_RESERVE1 + W_BLOCK_STAMP)
    outcome: object = UNSET
    with pytest.raises(SourceError):
        outcome = _reader(node).call(V2_PAIR, "getReserves")
    assert outcome is UNSET


def test_a_result_that_is_not_hex_is_a_source_error() -> None:
    # pins: the result is wire data, so bytes.fromhex on it is guarded. The
    #       payload below is exactly 64 characters, so the length check
    #       cannot catch it and only the guard can: unguarded, fromhex
    #       raises ValueError, which escapes the SourceError promise and is
    #       invisible to anyone writing `except SourceError`.
    node = Node("0x" + "z" * 64)
    outcome: object = UNSET
    with pytest.raises(SourceError):
        outcome = _reader(node).call(V2_PAIR, "balanceOf", (VITALIK,))
    assert outcome is UNSET


def test_a_result_without_the_0x_prefix_is_a_source_error() -> None:
    # pins: the prefix is checked, never assumed. This payload is 66 hex
    #       characters with no prefix, so slicing [2:] off it drops the
    #       leading byte and leaves a perfectly well formed word: an
    #       implementation that assumes the prefix RETURNS 850000000000000
    #       here, having silently accepted a malformed response.
    node = Node("ff" + W_LP_BALANCE)
    outcome: object = UNSET
    with pytest.raises(SourceError):
        outcome = _reader(node).call(V2_PAIR, "balanceOf", (VITALIK,))
    assert outcome is UNSET
    assert outcome != 850_000_000_000_000


def test_every_refusal_on_this_path_is_an_auradefi_error() -> None:
    # pins: the taxonomy. Nothing on the read path escapes as a bare
    #       ValueError, TypeError or AttributeError, which is what makes
    #       resolve.py's per-adapter catch able to file a failure as data.
    cases: tuple[tuple[Node, str, str, tuple[object, ...]], ...] = (
        (Node("0x"), V2_PAIR, "balanceOf", (VITALIK,)),
        (Node("0x" + SIX_WORD[2:]), V2_PAIR, "balanceOf", (VITALIK,)),
        (Node("0x" + W_DIRTY_ADDRESS), V2_PAIR, "token0", ()),
        (Node("0x" + "z" * 64), V2_PAIR, "balanceOf", (VITALIK,)),
        (Node("ff" + W_LP_BALANCE), V2_PAIR, "balanceOf", (VITALIK,)),
        (Node(), V2_PAIR, "balanceOf", ()),
        (Node(), RETH, "getSomeNewRate", (1,)),
    )
    for node, address, fn, args in cases:
        outcome: object = UNSET
        with pytest.raises(AuradefiError):
            outcome = _reader(node).call(address, fn, args)
        assert outcome is UNSET, f"{fn} returned {outcome!r} instead of raising"
