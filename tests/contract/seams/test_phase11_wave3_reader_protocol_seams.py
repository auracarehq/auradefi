"""Phase 11 wave-3 seam audit: ``reader.py`` against the domain it serves.

Nothing here looks inside the reader. Every assertion crosses a boundary that
no single work order owned, because ``sources/evm/reader.py`` was written
without importing ``auradefi.positions`` and the five adapters were written
before any concrete reader existed. The two sides have never been compared
except through the wave-4 golden, which drives them with a fixture reader that
lowercases what it is given and a node reader that lowercases what it sends.

The boundaries audited here:

* the DECLARED signature. ``positions/protocol.py::ContractReader.call`` states
  parameter names, order and the ``args`` default, and the phase-11 order calls
  all three part of the contract. The reader's own test asserts them against a
  hardcoded tuple, which stays green if the protocol moves. This file compares
  the two signature objects.

* what ``isinstance`` actually proves. ``runtime_checkable`` is the declared
  proof of the structural binding. It checks that an attribute named ``call``
  exists and nothing else, so a host reader that omits the ``args`` default
  passes every check the package makes and then fails inside four adapters.
  That gap is reported as a seam finding and pinned below.

* the third-party binding. A reader written ONLY from the protocol text, with
  no lowercasing, no defaulting and no extra method, driving all six adapter
  legs. The golden's ``DictReader`` lowercases the address it is handed, which
  would hide an adapter that passes a checksummed one.

* the registry against its call sites, in BOTH directions. Names are covered by
  the reader's own gate; argument arity and returned tuple arity are not, and
  an adapter that unpacks one word too many is a defect neither file can see.

* the registry against RELEASE_0.2.0 section 4's table, transcribed here from
  the document rather than copied from the module.

* the open shape as host-extensibility. ``ReceiptToken.rate_fn`` is host data,
  so the seam is only real if a rate function nobody registered reaches the
  wire under the right selector and comes back as one word.

* the failure channel end to end: a node error inside a real reader, through
  an adapter, into ``resolve_all``'s ``AdapterFailure`` rows, with the sibling
  adapter's positions intact.

Every fixture can express the pinned behaviour and its negation, and each test
names the input that flips it.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import json
from pathlib import Path

import httpx
import pytest

from auradefi.errors import SourceError, ValidationError
from auradefi.money.quantity import Quantity
from auradefi.positions.adapters.amm.uniswap_v2 import UniswapV2Adapter
from auradefi.positions.adapters.amm.uniswap_v3 import (
    UniswapV3Adapter,
    get_sqrt_ratio_at_tick,
)
from auradefi.positions.adapters.staking.liquid import LidoAdapter, RocketPoolAdapter
from auradefi.positions.adapters.tokens import ReceiptToken, ReceiptTokenAdapter
from auradefi.positions.models import Range
from auradefi.positions.protocol import (
    ContractDescriptor,
    ContractReader,
    ContractSet,
    DiscoveryContext,
    ResolveContext,
)
from auradefi.positions.resolve import resolve_all
from auradefi.sources.evm.codec.abi import decode, encode
from auradefi.sources.evm.codec.keccak import keccak256
from auradefi.sources.evm.reader import (
    DEFAULT_RETURN_TYPES,
    SIGNATURES,
    EvmContractReader,
)
from auradefi.sources.evm.rpc import EvmRpc

REPO_ROOT = Path(__file__).resolve().parents[3]
POSITIONS = REPO_ROOT / "src" / "auradefi" / "positions"
GOLDEN_PATH = REPO_ROOT / "tests" / "golden" / "test_phase11_reader.py"

NODE_URL = "https://evm-node.invalid/rpc"
CHAIN = "eip155:1"
BLOCK = 20_450_000
VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
VAULT = "0x00000000000000000000000000000000000000aa"
REVERTER = "0x000000000000000000000000000000000000dead"
ETH_ID = "eip155:1/slip44:60"

#: RELEASE_0.2.0 section 4's call-surface table, transcribed from the document.
#: The last row is spelled "receipt `rate_fn` | none | uint256" there, and the
#: wave-3 order's adopted override keys it under the on-chain name the only
#: shipped receipt supplies. That override is asserted separately below, so
#: this transcription carries the consequence and not the spelling.
RELEASE_TABLE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
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
            "uint96",
            "address",
            "address",
            "address",
            "uint24",
            "int24",
            "int24",
            "uint128",
            "uint256",
            "uint256",
            "uint128",
            "uint128",
        ),
    ),
    "getPool": (("address", "address", "uint24"), ("address",)),
    "tokenOfOwnerByIndex": (("address", "uint256"), ("uint256",)),
    "getUserAccountData": (("address",), ("uint256",) * 6),
    "getExchangeRate": ((), ("uint256",)),
}

#: One probe value per ABI type the registry names, so a row can be encoded and
#: read back without knowing which function it belongs to.
PROBE: dict[str, object] = {"address": USDC, "bool": True}


def word(value: int) -> str:
    """One 32-byte big-endian word as 64 hex digits, two's complement."""
    return f"{value & (2**256 - 1):064x}"


def golden() -> object:
    """The wave-4 gate module, imported for its adapter drives and fixture."""
    spec = importlib.util.spec_from_file_location("_wave3_golden_reads", GOLDEN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Node:
    """A JSON-RPC node that answers ``eth_call`` from a ``(to, data)`` table.

    An address in ``reverting`` answers the whole envelope with an error
    member, which is what a reverting ``eth_call`` produces. Anything the
    table does not hold is a failed assertion rather than a default word, so
    a read this fixture did not anticipate can never pass as a zero.
    """

    def __init__(
        self, answers: dict[tuple[str, str], str], reverting: frozenset[str] = frozenset()
    ) -> None:
        self.answers = answers
        self.reverting = reverting
        self.posted: list[dict] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def rpc(self) -> EvmRpc:
        return EvmRpc(self.client(), NODE_URL)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.posted.append(body)
        call = body["params"][0]
        if call["to"] in self.reverting:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": "execution reverted"},
                },
            )
        key = (call["to"], call["data"])
        assert key in self.answers, f"the fixture holds no read for {key}"
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": self.answers[key]}
        )


class ProtocolOnlyReader:
    """A ContractReader written from the protocol text and nothing else.

    Declared surface only: one method, the three declared parameter names in
    the declared order, and the declared ``args`` default. It does NOT
    lowercase, does not normalise ``args`` and holds no other method, so an
    adapter leaning on any of that fails here and passes with the shipped
    reader. Unknown reads assert rather than return a default.
    """

    def __init__(self, reads: dict[tuple[str, str, tuple[object, ...]], object]) -> None:
        self._reads = reads
        self.seen: list[tuple[str, str, tuple[object, ...]]] = []

    def call(
        self, address: str, fn: str, args: tuple[object, ...] = ()
    ) -> object:
        assert address == address.lower(), (
            f"an adapter handed the reader a non-lowercase address: {address!r}"
        )
        assert isinstance(args, tuple), f"args must be a tuple, got {type(args)}"
        self.seen.append((address, fn, args))
        key = (address, fn, args)
        assert key in self._reads, f"the protocol-only reader has no read for {key}"
        return self._reads[key]


class NoDefaultReader:
    """The same reader with the ``args`` default dropped. Nothing else moves.

    It answers 1 to every read it survives, so the failure below is the
    missing default and never an empty balance short-circuiting the adapter.
    """

    def call(self, address: str, fn: str, args: tuple[object, ...]) -> object:
        return 1


def _call_sites() -> list[tuple[str, str, ast.Call]]:
    """Every ``<expr>.call(addr, "<fn>", ...)`` under ``positions/``.

    Only string-literal function names are collected, which is what makes the
    arity comparisons below mechanical. ``tokens.py`` passes ``receipt.rate_fn``
    and is covered by the host-data test instead.
    """
    sites: list[tuple[str, str, ast.Call]] = []
    for path in sorted(POSITIONS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "call":
                continue
            if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
                continue
            fn = node.args[1].value
            if isinstance(fn, str):
                sites.append((str(path.relative_to(REPO_ROOT)), fn, node))
    return sites


def test_the_declared_call_signature_is_identical_on_both_sides_of_the_seam() -> None:
    # pins: the protocol's `call` and the concrete reader's `call` are the same
    #       signature object, names, order, annotations and the args default
    #       included. Rename `fn` to `name` on either side and this goes red,
    #       which a hardcoded parameter-name assertion in one file does not.
    declared = inspect.signature(ContractReader.call)
    concrete = inspect.signature(EvmContractReader.call)
    assert concrete == declared
    assert [p.name for p in declared.parameters.values()] == [
        "self",
        "address",
        "fn",
        "args",
    ]
    assert declared.parameters["args"].default == ()

    # The names are load-bearing, so a keyword call has to reach the same wire.
    node = Node({(USDC, "0x313ce567"): "0x" + word(6)})
    reader = EvmContractReader(node.rpc())
    assert reader.call(address=USDC, fn="decimals", args=()) == 6
    assert reader.call(USDC, "decimals") == 6
    assert [body["params"][0]["data"] for body in node.posted] == [
        "0x313ce567",
        "0x313ce567",
    ]


def test_isinstance_against_the_protocol_does_not_prove_the_declared_signature() -> None:
    # SEAM FINDING, pinned as it stands today: `runtime_checkable` checks that
    # an attribute named `call` exists and checks NOTHING about its parameters,
    # so the declared proof of the structural binding does not cover the part
    # of the contract the phase-11 order calls part of the contract. A host
    # reader missing the `args` default satisfies every check the package makes
    # and then breaks every zero-argument read.
    # Flip: give NoDefaultReader the default back and the TypeError disappears.
    assert isinstance(NoDefaultReader(), ContractReader) is True
    assert isinstance(type("Nullary", (), {"call": lambda self: None})(), ContractReader)

    signature = inspect.signature(NoDefaultReader.call)
    assert signature != inspect.signature(ContractReader.call)

    descriptor = ContractDescriptor(
        adapter_id="uniswap-v2",
        chain_id=CHAIN,
        address="0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc",
        category="amm-pair",
        underlyings=(f"{CHAIN}/erc20:{USDC}", f"{CHAIN}/erc20:{VAULT}"),
    )
    ctx = ResolveContext(
        chain_id=CHAIN, address=VITALIK, reader=NoDefaultReader(), block_number=BLOCK
    )
    with pytest.raises(TypeError) as raised:
        UniswapV2Adapter().resolve(ctx, ContractSet.of(descriptor))
    # Named, so this cannot pass on some other TypeError the fake provokes.
    assert "positional argument" in str(raised.value)
    assert "args" in str(raised.value)

    # And the way a host meets it: as a dropped slice, not as an exception.
    outcome = resolve_all(
        [UniswapV2Adapter()], ctx, {"uniswap-v2": ContractSet.of(descriptor)}
    )
    assert outcome.positions == ()
    assert len(outcome.failures) == 1
    assert "TypeError" in outcome.failures[0].error
    assert "args" in outcome.failures[0].error


def test_a_protocol_only_reader_that_never_lowercases_still_serves_every_adapter() -> None:
    # pins: no adapter depends on the reader normalising what it is handed.
    #       The golden's fixture reader lowercases the address, so a checksummed
    #       address reaching the seam would pass there and fail here.
    # Flip: make one adapter pass a checksummed address and the assert inside
    #       ProtocolOnlyReader names the file that did it.
    # The fixture's own negation first: it refuses a checksummed address, so a
    # green run below means no adapter passed one, not that nobody looked.
    with pytest.raises(AssertionError):
        ProtocolOnlyReader({}).call("0x" + VITALIK[2:].upper(), "decimals")

    module = golden()
    reads = getattr(module, "DICT_READS")
    legs = getattr(module, "SIX_LEGS")
    expected_reader = getattr(module, "_dict_reader")()
    assert len(legs) == 6

    for name, run, expected_reads in legs:
        strict = ProtocolOnlyReader(dict(reads))
        assert run(strict) == run(expected_reader), name
        assert len(strict.seen) == expected_reads, name
        assert all(address == address.lower() for address, _, _ in strict.seen), name


def test_every_adapter_call_site_passes_the_arity_its_registry_row_declares() -> None:
    # pins: the registry's arg_types and the adapters' argument tuples are two
    #       statements of one fact. The reader gate checks that the NAME is
    #       known; a call site passing one word too few is refused at runtime
    #       and drops that adapter's whole slice.
    # Flip: drop `index` from the tokenOfOwnerByIndex call site, or add a type
    #       to any registry row, and this goes red naming both counts.
    checked: list[tuple[str, str, int]] = []
    for where, fn, node in _call_sites():
        assert fn in SIGNATURES, f"{where} calls {fn!r}, which the registry lacks"
        declared = len(SIGNATURES[fn][0])
        if len(node.args) == 2:
            passed = 0
        elif isinstance(node.args[2], ast.Tuple):
            passed = len(node.args[2].elts)
        else:
            continue
        assert passed == declared, (
            f"{where}:{node.lineno} calls {fn} with {passed} arguments and the "
            f"registry declares {declared}"
        )
        checked.append((where, fn, passed))

    names = {fn for _, fn, _ in checked}
    assert names >= {
        "balanceOf",
        "decimals",
        "totalSupply",
        "token0",
        "token1",
        "getReserves",
        "allPairsLength",
        "allPairs",
        "slot0",
        "positions",
        "getPool",
        "tokenOfOwnerByIndex",
        "getUserAccountData",
    }, names


def test_every_adapter_unpack_matches_the_registry_return_arity() -> None:
    # pins: the return SHAPE, which the protocol types as `object` and states
    #       nowhere. An adapter that unpacks a reader result into names is
    #       asserting a width, and the registry is the other statement of it.
    # Flip: drop uint32 from getReserves, or a word from the positions row, and
    #       the unpack that reads it goes red here instead of at runtime.
    by_line = {(where, node.lineno): fn for where, fn, node in _call_sites()}
    unpacks: list[tuple[str, str, int]] = []
    for path in sorted(POSITIONS.rglob("*.py")):
        where = str(path.relative_to(REPO_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            fn = by_line.get((where, node.value.lineno))
            if fn is None or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Tuple):
                continue
            declared = len(SIGNATURES[fn][1])
            starred = any(isinstance(e, ast.Starred) for e in target.elts)
            fixed = sum(1 for e in target.elts if not isinstance(e, ast.Starred))
            if starred:
                assert declared >= fixed, f"{where}:{node.lineno} {fn}"
            else:
                assert declared == fixed, (
                    f"{where}:{node.lineno} unpacks {fn} into {fixed} names and "
                    f"the registry returns {declared} words"
                )
            unpacks.append((where, fn, fixed))

    found = {fn for _, fn, _ in unpacks}
    assert found >= {"getReserves", "positions", "slot0"}, found


def test_the_shipped_registry_is_the_release_table_and_the_codec_can_spell_it() -> None:
    # pins: the registry as data against the document it was sourced from, and
    #       every type name in it against the codec that has to encode it. A
    #       row naming `uint112[]` or `bytes` would pass the reader's own tests
    #       until a node answered one.
    # Flip: change any width in RELEASE_TABLE and the equality goes red.
    assert dict(SIGNATURES) == RELEASE_TABLE
    assert DEFAULT_RETURN_TYPES == ("uint256",)
    assert "rate_fn" not in SIGNATURES
    assert SIGNATURES["getExchangeRate"] == ((), ("uint256",))
    assert keccak256(b"getExchangeRate()")[:4].hex() == "e6aa216c"

    for fn, (arg_types, return_types) in SIGNATURES.items():
        for name in (*arg_types, *return_types):
            value = PROBE.get(name, 1)
            blob = encode((name,), (value,))
            assert len(blob) == 32, f"{fn}:{name}"
            assert decode((name,), blob) == (value,), f"{fn}:{name}"


def test_a_host_declared_rate_fn_reaches_the_wire_without_a_registry_edit() -> None:
    # pins: the open shape as the thing it exists for. `rate_fn` is host data
    #       on ReceiptToken, so a receipt nobody shipped must resolve, under the
    #       selector of its own name, as one uint256 word, all the way to a
    #       Position quantity.
    # Flip: make the open shape return two words and the redemption arithmetic
    #       raises inside int(); refuse unknown names entirely and this goes red
    #       with a ValidationError before any HTTP.
    for adapter in (LidoAdapter(), RocketPoolAdapter()):
        for receipt in adapter.receipts[CHAIN]:
            if receipt.rate_fn is None:
                continue
            assert SIGNATURES.get(receipt.rate_fn) == ((), ("uint256",))

    rate_selector = keccak256(b"getSomeNewRate()")[:4].hex()
    assert "getSomeNewRate" not in SIGNATURES
    node = Node(
        {
            (VAULT, "0x70a08231" + word(int(VITALIK, 16))): "0x" + word(2 * 10**18),
            (VAULT, "0x" + rate_selector): "0x" + word(1_500_000_000_000_000_000),
        }
    )

    class HostVault(ReceiptTokenAdapter):
        id = "host-vault"
        chains = frozenset({CHAIN})
        receipts = {CHAIN: (ReceiptToken(VAULT, ETH_ID, 18, "getSomeNewRate"),)}

    descriptor = ContractDescriptor(
        adapter_id="host-vault",
        chain_id=CHAIN,
        address=VAULT,
        category="receipt-token",
        underlyings=(ETH_ID,),
    )
    ctx = ResolveContext(
        chain_id=CHAIN,
        address=VITALIK,
        reader=EvmContractReader(node.rpc(), block_number=BLOCK),
    )
    positions = HostVault().resolve(ctx, ContractSet.of(descriptor))
    assert len(positions) == 1
    assert positions[0].underlyings[0].quantity == Quantity(3 * 10**18, 18)
    assert [body["params"][0]["data"] for body in node.posted][1] == (
        "0x" + rate_selector
    )
    assert all(body["params"][1] == "0x1380ad0" for body in node.posted)


def test_a_rate_fn_that_would_need_arguments_is_refused_before_any_http() -> None:
    # pins: the other half of the open shape. A host naming a function that
    #       takes arguments is a startup mistake, not a guessed selector, and it
    #       is refused after the balance read and before the rate read.
    # Flip: register `balanceOf` as zero-argument and the refusal disappears.
    node = Node({(VAULT, "0x70a08231" + word(int(VITALIK, 16))): "0x" + word(5)})

    class BadVault(ReceiptTokenAdapter):
        id = "bad-vault"
        chains = frozenset({CHAIN})
        receipts = {CHAIN: (ReceiptToken(VAULT, ETH_ID, 18, "balanceOf"),)}

    descriptor = ContractDescriptor(
        adapter_id="bad-vault",
        chain_id=CHAIN,
        address=VAULT,
        category="receipt-token",
        underlyings=(ETH_ID,),
    )
    ctx = ResolveContext(
        chain_id=CHAIN, address=VITALIK, reader=EvmContractReader(node.rpc())
    )
    with pytest.raises(ValidationError) as raised:
        BadVault().resolve(ctx, ContractSet.of(descriptor))
    assert "balanceOf" in str(raised.value)
    assert len(node.posted) == 1


def test_a_reverting_read_drops_one_adapter_slice_and_leaves_its_sibling() -> None:
    # pins: the whole failure channel, source to domain. A node error inside the
    #       real reader is a SourceError, `resolve_all` files it as
    #       AdapterFailure data, the sibling adapter keeps its position, and no
    #       zero-quantity position is emitted for the adapter that failed.
    # Flip: have the reader return 0 for a reverting call and the failed adapter
    #       silently emits nothing while `failures` empties, which this catches.
    good = "0x00000000000000000000000000000000000000bb"
    node = Node(
        {(good, "0x70a08231" + word(int(VITALIK, 16))): "0x" + word(7 * 10**18)},
        reverting=frozenset({REVERTER}),
    )
    reader = EvmContractReader(node.rpc(), block_number=BLOCK)

    class GoodVault(ReceiptTokenAdapter):
        id = "good-vault"
        chains = frozenset({CHAIN})
        receipts = {CHAIN: (ReceiptToken(good, ETH_ID, 18, None),)}

    class DeadVault(ReceiptTokenAdapter):
        id = "dead-vault"
        chains = frozenset({CHAIN})
        receipts = {CHAIN: (ReceiptToken(REVERTER, ETH_ID, 18, None),)}

    def described(adapter_id: str, address: str) -> ContractSet:
        return ContractSet.of(
            ContractDescriptor(
                adapter_id=adapter_id,
                chain_id=CHAIN,
                address=address,
                category="receipt-token",
                underlyings=(ETH_ID,),
            )
        )

    ctx = ResolveContext(chain_id=CHAIN, address=VITALIK, reader=reader)
    with pytest.raises(SourceError):
        DeadVault().resolve(ctx, described("dead-vault", REVERTER))

    outcome = resolve_all(
        [GoodVault(), DeadVault()],
        ctx,
        {
            "good-vault": described("good-vault", good),
            "dead-vault": described("dead-vault", REVERTER),
        },
    )
    assert [p.adapter_id for p in outcome.positions] == ["good-vault"]
    assert outcome.positions[0].underlyings[0].quantity == Quantity(7 * 10**18, 18)
    assert [f.adapter_id for f in outcome.failures] == ["dead-vault"]
    assert "SourceError" in outcome.failures[0].error
    assert "execution reverted" in outcome.failures[0].error


def test_a_negative_int24_tick_survives_the_reader_into_the_position_range() -> None:
    # pins: the sign convention across the reader and the adapter. Every tick
    #       fixture in the repository is positive, and an int24 read as
    #       unsigned turns -201000 into 16,576,216, which is inside int24's
    #       word and outside the adapter's tick bounds. The Range below is the
    #       only place that shows it.
    # Flip: decode int24 without the sign extension and tick_lower reads
    #       16576216, in_range flips to False and the sqrt maths raises.
    manager = "0xc36442b4a4522e871399cd717abdd847ab11fe88"
    factory = "0x1f98431c8ad98523631ae4a59f267346ea31f984"
    pool = "0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8"
    weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    lower, upper, current = -201_000, -199_000, -200_000
    token_id = 7
    positions_row = "0x" + "".join(
        word(value)
        for value in (
            0,
            0,
            int(USDC, 16),
            int(weth, 16),
            3000,
            lower,
            upper,
            2_000_000_000_000_000,
            0,
            0,
            0,
            0,
        )
    )
    sqrt_price = get_sqrt_ratio_at_tick(current)
    node = Node(
        {
            (manager, "0x70a08231" + word(int(VITALIK, 16))): "0x" + word(1),
            (
                manager,
                "0x2f745c59" + word(int(VITALIK, 16)) + word(0),
            ): "0x" + word(token_id),
            (manager, "0x99fbab88" + word(token_id)): positions_row,
            (
                factory,
                "0x1698ee82" + word(int(USDC, 16)) + word(int(weth, 16)) + word(3000),
            ): "0x" + word(int(pool, 16)),
            (pool, "0x3850c7bd"): "0x"
            + "".join(word(v) for v in (sqrt_price, current, 0, 1, 1, 0, 1)),
            (USDC, "0x313ce567"): "0x" + word(6),
            (weth, "0x313ce567"): "0x" + word(18),
        }
    )
    reader = EvmContractReader(node.rpc(), block_number=BLOCK)
    adapter = UniswapV3Adapter()
    contracts = adapter.discover(DiscoveryContext(chain_id=CHAIN, reader=reader))
    ctx = ResolveContext(chain_id=CHAIN, address=VITALIK, reader=reader)
    positions = adapter.resolve(ctx, contracts)

    assert len(positions) == 1
    assert positions[0].range == Range(lower, upper, True)
    assert positions[0].range.tick_lower < 0 and positions[0].range.tick_upper < 0
    # token0 and token1 are same-typed neighbours in the twelve-word row, so
    # the decimals prove the pair did not arrive swapped.
    by_asset = {u.asset_id: u.quantity for u in positions[0].underlyings}
    assert by_asset[f"{CHAIN}/erc20:{USDC}"].decimals == 6
    assert by_asset[f"{CHAIN}/erc20:{weth}"].decimals == 18
    assert all(u.quantity.raw > 0 for u in positions[0].underlyings)


def test_the_resolve_context_block_pin_never_reaches_the_reader() -> None:
    # SEAM FINDING, characterised as it stands today: ResolveContext carries a
    # `block_number` and ContractReader.call has no block parameter, so the two
    # cannot meet. A resolve pinned at a block reads whatever the reader was
    # BUILT at, silently, with no error on either side. Nothing in src/ reads
    # ResolveContext.block_number, so today this is a latent gap and not a live
    # defect; the day a host wires the two it becomes wrong data.
    # Flip: thread the pin through and the "latest" assertion below inverts.
    assert "block_number" not in inspect.signature(ContractReader.call).parameters
    node = Node({(USDC, "0x313ce567"): "0x" + word(6)})
    reader = EvmContractReader(node.rpc())
    ctx = ResolveContext(
        chain_id=CHAIN, address=VITALIK, reader=reader, block_number=BLOCK
    )
    assert ctx.block_number == BLOCK
    assert ctx.reader.call(USDC, "decimals") == 6
    assert node.posted[0]["params"][1] == "latest"

    pinned = Node({(USDC, "0x313ce567"): "0x" + word(6)})
    EvmContractReader(pinned.rpc(), block_number=BLOCK).call(USDC, "decimals")
    assert pinned.posted[0]["params"][1] == "0x1380ad0"
