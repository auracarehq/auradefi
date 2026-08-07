"""A documented exception promise must hold for the WRONG TYPE, not only
the wrong value.

MOTIVATING FINDING (0.2.0 phase 11, `src/auradefi/sources/evm/rpc.py:349`,
adversarial, major). `EvmRpc` states "Every failure raises
`auradefi.errors.SourceError` and nothing else", and the first fix bought
that promise at the json-encoding door one statement before the socket. Every
caller argument that is CONSUMED earlier still leaked a builtin straight past
it: `eth_call(None, "0x")` died on `to.lower()` with `AttributeError`, and
`batch([("eth_call",)])` died on the tuple unpack with `ValueError`, both
upstream of the door. rpc.py now refuses those two on entry, and states the
rule this gate holds the rest of the tree to:

    whatever touches a caller's argument first is what refuses it.

WHY PARTIAL COVERAGE IS WORSE THAN NONE, and why this gate is behavioural.
Before that fix every wrong-typed argument leaked, uniformly; a caller
writing `except SourceError` at least learned quickly that it was not
enough. After it, some arguments are refused and some are not, which is the
state that ships: the gap is invisible until the one unrefused argument shows
up in production. The defect is therefore not a syntax a regex can see. It is
the ABSENCE of an `isinstance` in front of an operation that presumes a type:

  * an attribute access   `address.lower()`, `caip19.partition("/")`
  * a comparison          `from_block < 0`, `page_size < 1`
  * a builtin that types  `hex(block_number)`, `len(args)`

Each of those reads as ordinary correct Python. A static gate demanding an
`isinstance` before every one of them would fire on hundreds of internal call
sites whose argument was typed by a dataclass or a route model two frames up,
get suppressed, and protect nothing. So this gate does not read the source.
It CALLS the public entry points with a hostile argument and asserts that
nothing but an `auradefi.errors.AuradefiError` comes out. A leak is a leak,
with no judgement required, and the case table below is curated by hand
rather than reflected, so every row carries the argument it is about.

Offline by construction (profile rule 9): every transport is an
`httpx.MockTransport` answering from memory, so no case can reach a socket
even when its argument survives to the send.

Deliberately NOT covered. Entry points whose arguments arrive already typed
from a boundary that coerces them: `LedgerBackend.list_events(limit=…)` is
reached through `api/routes/sync.py`'s `limit: int | None`, which FastAPI
parses before the handler runs, and `embed/sync.py`'s `page_size` / `budget`
come from an orchestrator call site, not from a caller's arbitrary object.
Those guards have the same `value < 1` shape and are correct where they
stand. Nothing here can tell a coerced boundary from an open one; the table
names the open ones.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import httpx
import pytest

from auradefi.errors import AuradefiError
from auradefi.prices.oracles.defillama import DefiLlamaOracle
from auradefi.sources.bitcoin.esplora import Esplora
from auradefi.sources.evm import logs
from auradefi.sources.evm import txfetch
from auradefi.sources.evm.codec import abi, aggregate3
from auradefi.sources.evm.multicall import Call, Multicall3
from auradefi.sources.evm.reader import EvmContractReader
from auradefi.sources.evm.rpc import EvmRpc
from auradefi.sources.evm.etherscan import EtherscanV2
from auradefi.sources.evm.source import EtherscanSource
from auradefi.sources.solana.rpc import SolanaRpc

#: A well-formed answer for whichever shape the case under test expects. No
#: case is about the response, so one body per family is enough; a case whose
#: argument reaches the wire is already past the door this gate watches.
_BODIES: dict[str, object] = {
    "rpc": {"jsonrpc": "2.0", "id": 1, "result": "0x1"},
    "etherscan": {"status": "1", "message": "OK", "result": []},
    "llama": {"coins": {}},
    "esplora": {"chain_stats": {"funded_txo_sum": 1, "spent_txo_sum": 0,
                                "tx_count": 1}},
    # One well-formed signature row, so the page-length comparison that ends
    # get_signatures is reached rather than short-circuited by a shape check.
    "solana": {
        "jsonrpc": "2.0",
        "id": 1,
        "result": [{"signature": "s", "slot": 1, "blockTime": 1, "err": None}],
    },
}

#: The system program, a base58 address `chains.solana.validate_address`
#: accepts, so a case about `limit` is not diverted into the address guard.
_SOLANA_SYSTEM = "11111111111111111111111111111111"


def _client(kind: str) -> httpx.Client:
    """An offline client answering every request with ``_BODIES[kind]``."""
    body = _BODIES[kind]
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    )


def _rpc() -> EvmRpc:
    """An `EvmRpc` on a mock transport, at a url that parses."""
    return EvmRpc(_client("rpc"), "https://node.invalid/v1")


def escaped(call: Callable[[], object]) -> str | None:
    """The dotted name of the non-domain exception ``call`` leaked, else None.

    A return, and an `AuradefiError` of any class, are both fine: this gate
    asks only that the package's own taxonomy is what a caller sees. Which
    class within it belongs to the module's own docstring and to the tests
    that mirror it, not here.
    """
    try:
        call()
    except AuradefiError:
        return None
    except BaseException as exc:  # noqa: BLE001 - the leak IS the finding
        return f"{type(exc).__module__}.{type(exc).__name__}: {exc}"
    return None


# Each row is (label, the call, the argument it is about). The label is what
# a failure prints, so it names the entry point and the hostile argument.
_HOSTILE: list[tuple[str, Callable[[], object]]] = [
    # logs.scan_logs promises, verbatim: "ValidationError: before any HTTP, on
    # from_block > to_block, a negative from_block, chunk_blocks <= 0". Every
    # one of those three guards is a comparison against an int literal, so a
    # str or a None argument never reaches the promise it is named in.
    ("logs.scan_logs(from_block='0')", lambda: logs.scan_logs(
        _rpc(), from_block="0", to_block=10)),
    ("logs.scan_logs(to_block=None)", lambda: logs.scan_logs(
        _rpc(), from_block=0, to_block=None)),
    ("logs.scan_logs(chunk_blocks='5')", lambda: logs.scan_logs(
        _rpc(), from_block=0, to_block=1, chunk_blocks="5")),
    # A float clears all three comparisons and dies further in, at block_tag's
    # hex(): the guard was written, and still does not cover the type.
    ("logs.scan_logs(from_block=1.5)", lambda: logs.scan_logs(
        _rpc(), from_block=1.5, to_block=2.5)),
    # aggregate3 refuses its `calls` on entry and forwards `block_number`
    # untouched to block_tag, whose hex() is the first thing to read it.
    ("Multicall3.aggregate3(block_number='x')", lambda: Multicall3(
        _rpc()).aggregate3([Call("0x" + "ab" * 20, b"")], block_number="x")),
    ("Multicall3.aggregate3(block_number=1.0)", lambda: Multicall3(
        _rpc()).aggregate3([Call("0x" + "ab" * 20, b"")], block_number=1.0)),
    # The reader's block pin is taken at construction, which performs no I/O
    # and so cannot refuse it; the leak surfaces one method later.
    ("EvmContractReader(block_number='x').call", lambda: EvmContractReader(
        _rpc(), "x").call("0x" + "ab" * 20, "decimals")),
    # "ValidationError: … on a known name called with the wrong number of
    # arguments" is a len() over the caller's tuple.
    ("EvmContractReader.call(args=None)", lambda: EvmContractReader(
        _rpc()).call("0x" + "ab" * 20, "decimals", None)),
    # The motivating finding's own shape, in the sibling aggregator: the
    # address is CONSUMED by .lower() on the second statement of the method.
    ("EtherscanV2.balances(address=None)", lambda: EtherscanV2(
        _client("etherscan")).balances("eip155:1", None)),
    ("EtherscanV2.balances(address=123)", lambda: EtherscanV2(
        _client("etherscan")).balances("eip155:1", 123)),
    # coin_key partitions each id before anything is fetched, and usd_prices
    # iterates the sequence it was handed.
    ("DefiLlamaOracle.usd_prices([None])", lambda: DefiLlamaOracle(
        _client("llama")).usd_prices([None])),
    ("DefiLlamaOracle.usd_prices(None)", lambda: DefiLlamaOracle(
        _client("llama")).usd_prices(None)),
    ("DefiLlamaOracle.usd_prices([b'x'])", lambda: DefiLlamaOracle(
        _client("llama")).usd_prices([b"x"])),
    # _require_valid_page_size exists precisely to stop a page size that can
    # never terminate the walk, and compares before it knows the type.
    ("txfetch.fetch_txlist(page_size=None)", lambda: txfetch.fetch_txlist(
        _client("etherscan"), chain_id=1, address="0xab", api_key=None,
        page_size=None)),
    ("txfetch.fetch_txlist(page_size='10')", lambda: txfetch.fetch_txlist(
        _client("etherscan"), chain_id=1, address="0xab", api_key=None,
        page_size="10")),
    # The worst of the set: `limit` is forwarded into the payload untouched
    # and first read by the page-length comparison that ends the walk, so a
    # node has already answered a real request before the promise breaks.
    ("SolanaRpc.get_signatures(limit=None)", lambda: SolanaRpc(
        _client("solana")).get_signatures(_SOLANA_SYSTEM, limit=None)),
]

# The arguments the tree ALREADY refuses, kept as a blind-detector guard. If
# `escaped` ever stops seeing exceptions, or an import here goes stale, these
# go green vacuously and the table above would too.
_ALREADY_REFUSED: list[tuple[str, Callable[[], object]]] = [
    ("EvmRpc.eth_call(to=None)", lambda: _rpc().eth_call(None, "0x")),
    ("EvmRpc.batch([('eth_call',)])", lambda: _rpc().batch([("eth_call",)])),
    ("EvmRpc.batch('ab')", lambda: _rpc().batch("ab")),
    ("EvmRpc.eth_call(data=b'')", lambda: _rpc().eth_call("0x" + "ab" * 20, b"")),
    ("Call(target=None)", lambda: Call(None, b"")),
    ("Multicall3.aggregate3(None)", lambda: Multicall3(_rpc()).aggregate3(None)),
]


@pytest.mark.parametrize("label,call", _ALREADY_REFUSED, ids=lambda v: v)
def test_the_arguments_the_tree_already_refuses_still_raise_a_domain_error(
    label: str, call: Callable[[], object]
) -> None:
    """Blind-detector guard: these must RAISE, inside the taxonomy."""
    with pytest.raises(AuradefiError):
        call()
    assert escaped(call) is None, f"{label} should already be refused"


def test_no_public_entry_point_leaks_a_builtin_for_a_wrong_typed_argument() -> None:
    """See the module docstring's motivating finding (`EvmRpc.eth_call`)."""
    leaks = [
        f"{label} -> {leak}"
        for label, call in _HOSTILE
        if (leak := escaped(call)) is not None
    ]
    assert len(_HOSTILE) >= 12, (
        "the hostile-argument table has shrunk; a case removed because it "
        "was inconvenient is a promise quietly withdrawn"
    )
    assert not leaks, (
        "a public entry point documents a domain exception for an argument "
        "and then presumes that argument's type, so the WRONG TYPE escapes "
        "the promise while the wrong value is refused. Refuse it where it is "
        "first touched, as sources/evm/rpc.py does for `to` and its batch "
        "pairs:\n  " + "\n  ".join(leaks)
    )


# ---------------------------------------------------------------------------
# THE HOST'S URL IS AN ARGUMENT TOO
#
# SECOND MOTIVATING FINDING (0.2.0 phase 11, `src/auradefi/sources/evm/rpc.py
# :386`, adversarial, major). The first widening of `_post`'s handler carried
# a blanket `TypeError` arm. A later review showed that arm also swallowed
# this module's OWN internal `TypeError`s and had it narrowed to
# `(httpx.HTTPError, httpx.InvalidURL, ValueError)`. Nobody walked what the
# removed arm had been catching legitimately, and one real case went with it:
# httpx raises a bare `TypeError` from `_urlparse` for a `url` that is not a
# `str`, so `EvmRpc(client, os.environ.get("NODE_URL"))` with the variable
# unset leaks `builtins.TypeError: Invalid type for url` past a docstring
# reading "Every failure raises SourceError and nothing else".
#
# THE CLASS, and why it is wider than the arm that was narrowed. A URL is
# host CONFIGURATION: it arrives from an environment variable, a settings
# file or a CLI flag, every one of which can hand over `None`, an `int` or
# `bytes` without a type error at the call site. It is then stored on the
# instance by a constructor that documents "no I/O", and first read many
# frames later inside httpx. So it is an entry-point argument with an unusually
# long fuse, and the rule the first finding wrote down covers it already:
# whatever touches a caller's argument first is what refuses it.
#
# Two mechanisms, both in the tree today. `EvmRpc`, `SolanaRpc`, `EtherscanV2`
# and `txfetch.fetch_page` store the string untouched and die at the send.
# `Esplora` and `DefiLlamaOracle` call `base_url.rstrip("/")` in `__init__`
# and die there, on an `AttributeError` for `None` and a `TypeError` for
# `bytes`. Both mechanisms break the same promise, so both are cases here.
#
# WHY THIS IS BEHAVIOURAL and not a widening of
# `test_transport_doors_catch_every_httpx_root.py`. That gate is static, and
# its docstring already refuses to demand a blanket `except TypeError` at
# every door, for the reason the narrowing happened: such an arm dresses an
# internal defect up as a node failure. The repair for this class is a type
# check on the configuration where it is consumed, which no exception tuple
# can express. Calling the door and looking at what comes out states the
# requirement without prescribing the fix.
# ---------------------------------------------------------------------------

#: Bad urls a host reaches by ordinary accident: an unset environment
#: variable, a port or an id left as a number, a byte string from a config
#: reader that never decoded.
_BAD_URLS: tuple[object, ...] = (None, 123, b"https://node.invalid/v1")


def _url_cases() -> list[tuple[str, Callable[[], object]]]:
    """One row per (transport door, bad url), labelled by both."""
    doors: list[tuple[str, Callable[[object], object]]] = [
        ("EvmRpc(url).eth_call",
         lambda url: EvmRpc(_client("rpc"), url).eth_call("0x" + "ab" * 20, "0x")),
        ("SolanaRpc(url).get_signatures",
         lambda url: SolanaRpc(_client("solana"), url).get_signatures(_SOLANA_SYSTEM)),
        ("EtherscanV2(base_url).balances",
         lambda url: EtherscanV2(_client("etherscan"), base_url=url).balances(
             "eip155:1", "0x" + "ab" * 20)),
        ("EtherscanSource(base_url).balances",
         lambda url: EtherscanSource(_client("etherscan"), base_url=url).balances(
             "eip155:1", "0x" + "ab" * 20)),
        ("txfetch.fetch_page(base_url)",
         lambda url: txfetch.fetch_page(
             _client("etherscan"), chain_id=1, address="0xab", base_url=url)),
        ("Esplora(base_url).address_stats",
         lambda url: Esplora(_client("esplora"), base_url=url).address_stats("bc1q")),
        ("DefiLlamaOracle(base_url).usd_prices",
         lambda url: DefiLlamaOracle(_client("llama"), base_url=url).usd_prices(
             ["eip155:1/slip44:60"])),
    ]
    return [
        (f"{label} url={bad!r}", lambda door=door, bad=bad: door(bad))
        for label, door in doors
        for bad in _BAD_URLS
    ]


#: Every transport door in the tree, against every bad url shape.
_HOSTILE_URLS = _url_cases()


def test_no_transport_door_leaks_a_builtin_for_a_wrong_typed_url() -> None:
    """See the SECOND motivating finding above (`EvmRpc._post`'s narrowing)."""
    leaks = [
        f"{label} -> {leak}"
        for label, call in _HOSTILE_URLS
        if (leak := escaped(call)) is not None
    ]
    # Seven doors times three url shapes. A shrunken table is a promise
    # quietly withdrawn, and a door dropped from the list is a door unwatched.
    assert len(_HOSTILE_URLS) >= 21, (
        f"only {len(_HOSTILE_URLS)} url cases: the table has been trimmed"
    )
    assert not leaks, (
        "a source's url is host configuration, and a host reaches these by "
        "accident (an unset environment variable is `None`). The module "
        "promises SourceError and nothing else, then hands over a bare "
        "builtin from httpx or from `base_url.rstrip`. Refuse a non-str url "
        "where it is consumed:\n  " + "\n  ".join(leaks)
    )


class SourceErrorLike(AuradefiError):
    """A stand-in domain error for the scratch reconstruction below.

    Subclassing the package root rather than importing `SourceError` keeps
    the proof about the taxonomy boundary this gate checks, which is
    `AuradefiError`, and not about one class inside it.
    """


def test_the_gate_fires_on_the_motivating_defect_and_clears_when_guarded() -> None:
    """`EvmRpc.eth_call`'s `to`, before and after, as scratch functions.

    Reconstructed here rather than by editing source: the point is that
    `escaped` distinguishes the two, not that rpc.py currently passes.
    """

    class Before:
        def eth_call(self, to: object) -> str:
            return to.lower()  # type: ignore[attr-defined]

    class After:
        def eth_call(self, to: object) -> str:
            if not isinstance(to, str):
                raise SourceErrorLike(f"eth_call needs a string target: {to!r}")
            return to.lower()

    leak = escaped(lambda: Before().eth_call(None))
    assert leak is not None and leak.startswith("builtins.AttributeError"), leak
    assert escaped(lambda: After().eth_call(None)) is None
    # And the guard must not swallow the legitimate argument.
    assert After().eth_call("0xAB") == "0xab"


def test_the_url_gate_fires_on_the_narrowed_door_and_clears_when_typed() -> None:
    """`EvmRpc(client, None)`, before and after, as scratch classes.

    The `before` class is the narrowed door verbatim: it catches the three
    httpx roots the static gate demands, and `httpx.Client.post` still raises
    a bare `TypeError` from its url parsing, which none of the three covers.
    Reconstructed here so the proof survives whatever repair rpc.py ships.
    """
    class Before:
        def __init__(self, client: httpx.Client, url: object) -> None:
            self._client, self._url = client, url

        def post(self) -> object:
            try:
                return self._client.post(self._url, json={}).json()
            except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
                raise SourceErrorLike(f"request failed: {exc!r}") from exc

    class After(Before):
        def __init__(self, client: httpx.Client, url: object) -> None:
            if not isinstance(url, str):
                raise SourceErrorLike(f"url must be a string: {url!r}")
            super().__init__(client, url)

    leak = escaped(lambda: Before(_client("rpc"), None).post())
    assert leak is not None and leak.startswith("builtins.TypeError"), leak
    assert escaped(lambda: After(_client("rpc"), None).post()) is None
    assert escaped(lambda: After(_client("rpc"), 123).post()) is None
    # And a url that parses must still reach the transport untouched.
    assert After(_client("rpc"), "https://node.invalid/v1").post() == _BODIES["rpc"]


# ---------------------------------------------------------------------------
# THE COLLECTION ARGUMENT IS AN ARGUMENT TOO
#
# THIRD MOTIVATING FINDING (0.2.0 phase 11, `sources/evm/multicall.py:184`,
# adversarial, major). `Multicall3.aggregate3` counted its `calls` and unpacked
# three fields off every element without checking either, so `aggregate3(None)`
# left as `TypeError: object of type 'NoneType' has no len()` and a plain
# `(target, flag, data)` triple as `AttributeError: 'tuple' object has no
# attribute 'target'`, both past a docstring reading "Only classes from
# `auradefi.errors` are raised". `_check_calls` now refuses both on entry.
#
# THE CLASS, and why it needs rows the two tables above do not have. Those
# tables are about a SCALAR whose type is presumed: a url, a block number, a
# page size. A collection argument breaks in three further ways that a scalar
# cannot, and a row per door is the only way to see them:
#
#   * `None` or an `int` dies on `len()` or on `for`, before any element
#   * a GENERATOR satisfies `Iterable` and dies on `len()`, or worse survives
#     it and is silently empty on the second pass
#   * a `str` satisfies `Sequence[T]` STRUCTURALLY for every T, so it clears an
#     `isinstance(x, Sequence)` guard and dies one frame later on the element.
#     This is why `rpc.py`'s `_is_pair_like` excludes str and bytes by name,
#     and why `_check_calls` copied that exclusion.
#
# WHY THE CODEC IS THE PLACE THE SWEEP LANDED. `codec/abi.py` states the
# strongest promise in the package, verbatim: "Every malformed input raises
# ValidationError, a value of the wrong Python type included ... neither can
# translate a ValueError from an unguarded unpack or a TypeError from `str`
# where bytes were due." It then writes the guard correctly ONE level down, at
# `_call_element`, whose own docstring says "Every field is read out and
# checked, never unpacked hopefully" - and leaves the ARRAY that feeds it, and
# the `types`/`values` pair its two general doors count, unchecked. That is the
# sibling asymmetry the sweep exists to catch: the rule is written down, the
# worked example sits in the same file, and the door above it is open.
#
# Zero transport here on purpose: these doors are pure, so a leak cannot be
# excused as a wire failure. There is nothing to blame but the argument.
# ---------------------------------------------------------------------------

#: The four shapes a collection argument arrives wrong in. `"ab"` is the
#: interesting one: it is a genuine `Sequence[str]` and passes any guard that
#: forgets to exclude it, which is the trap `_is_pair_like` names.
_BAD_COLLECTIONS: tuple[tuple[str, object], ...] = (
    ("None", None),
    ("int", 5),
    ("str", "ab"),
)


def _collection_cases() -> list[tuple[str, Callable[[], object]]]:
    """One row per (pure door, bad collection), plus the generator rows.

    The generator is built inside each lambda rather than in the table: one
    shared generator would be consumed by the first row that iterated it and
    every later row would see an empty sequence, which is the very confusion
    these rows exist to refuse.
    """
    doors: list[tuple[str, Callable[[object], object]]] = [
        ("abi.encode(types)", lambda c: abi.encode(c, [])),
        ("abi.encode(values)", lambda c: abi.encode(["uint256"], c)),
        ("abi.decode(types)", lambda c: abi.decode(c, b"\x00" * 32)),
        ("aggregate3.encode_aggregate3(calls)", lambda c: aggregate3.encode_aggregate3(c)),
        ("abi.function_signature(arg_types)",
         lambda c: abi.function_signature("f", c)),
    ]
    rows = [
        (f"{label} = {shape}", lambda door=door, bad=bad: door(bad))
        for label, door in doors
        for shape, bad in _BAD_COLLECTIONS
    ]
    rows += [
        (f"{label} = generator", lambda door=door: door(x for x in ()))
        for label, door in doors
    ]
    return rows


#: Every pure collection door in the codec, against every wrong shape.
_HOSTILE_COLLECTIONS = _collection_cases()


def test_no_pure_door_leaks_a_builtin_for_a_wrong_typed_collection() -> None:
    """See the THIRD motivating finding above (`Multicall3.aggregate3`)."""
    leaks = [
        f"{label} -> {leak}"
        for label, call in _HOSTILE_COLLECTIONS
        if (leak := escaped(call)) is not None
    ]
    # Five doors times four shapes. A shrunken table is a promise withdrawn.
    assert len(_HOSTILE_COLLECTIONS) >= 20, (
        f"only {len(_HOSTILE_COLLECTIONS)} collection cases: the table has "
        "been trimmed"
    )
    assert not leaks, (
        "a door counts a collection argument, or unpacks fields off its "
        "elements, without first checking it is a sequence of what it needs. "
        "codec/abi.py promises ValidationError for `a value of the wrong "
        "Python type` and its own `_call_element` shows the guard; the doors "
        "above it skip it. Refuse the collection on entry, as "
        "multicall.py's `_check_calls` and rpc.py's `_is_pair_like` do, and "
        "exclude str and bytes by name:\n  " + "\n  ".join(leaks)
    )


def test_the_collection_gate_fires_on_the_defect_and_clears_when_guarded():
    """`aggregate3`'s `calls`, before and after, as scratch functions.

    Reconstructed here rather than by editing source, so the proof outlives
    whatever repair the codec ships. `After` is `_check_calls` in miniature:
    the str/bytes exclusion is what makes the `"ab"` row pass, and a guard
    written without it clears the `isinstance` and still dies on the element.
    """

    class Triple:
        target = "0x" + "ab" * 20

    def before(calls: object) -> list[str]:
        return [call.target for call in calls][: len(calls)]  # type: ignore

    def after(calls: object) -> list[str]:
        if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)):
            raise SourceErrorLike(f"needs a sequence: {calls!r}")
        for call in calls:
            if not isinstance(call, Triple):
                raise SourceErrorLike(f"not a Triple: {call!r}")
        return [call.target for call in calls]

    # Each wrong shape must leak BEFORE and be refused AFTER, and the two
    # shapes fail differently, which is the reason both are in the table.
    assert (escaped(lambda: before(None)) or "").startswith("builtins.TypeError")
    assert (escaped(lambda: before("ab")) or "").startswith("builtins.AttributeError")
    assert (escaped(lambda: before(x for x in ()))or "").startswith("builtins.TypeError")
    for bad in (None, "ab", 5):
        assert escaped(lambda bad=bad: after(bad)) is None, bad
    assert escaped(lambda: after(x for x in ())) is None
    # And the guard must not swallow the legitimate batch, empty one included.
    assert after([Triple()]) == ["0x" + "ab" * 20]
    assert after([]) == []
