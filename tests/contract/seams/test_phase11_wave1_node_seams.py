"""Phase 11 wave-1 seam audit: the boundaries BETWEEN keccak.py and rpc.py,
and between both of them and everything the package already had.

Nothing here looks inside either module. Every assertion crosses a boundary
that no single work order owned:

* the injected client. ``EvmRpc(client, url)`` is declared as "client
  injected, url required, no I/O", and multicall.py, logs.py, reader.py and
  the wave-4 golden all hand it an object. The binding below is written ONLY
  from that declaration: one ``post(url, json=...)`` method and a response
  carrying ``status_code`` and ``json()``. No httpx, no MockTransport, no
  in-repo helper. If the module ever reaches for ``client.request``,
  ``client.headers`` or ``response.raise_for_status``, every in-repo test
  keeps passing because httpx.Client has all three, and this file goes red.

* the recorded wire body. The wave-4 fixture in
  ``tests/golden/test_phase11_reader.py`` was hand-packed before rpc.py
  existed. Its nineteen recorded requests are the other side of the
  ``eth_call`` params seam, so they are replayed here through a matcher keyed
  on the EXACT body, no key sorting, which is the strict reading of the
  declared seam: "any change to key order, casing or an added 'from' key
  breaks that fixture".

* the selectors. Every four-byte prefix in that fixture was derived by hand
  from a signature in RELEASE_0.2.0 §4's call-surface table. keccak256 is the
  second derivation of the same value, and the two are compared here rather
  than each being read.

* the cassette discipline. DECISIONS.md pins "JSON-RPC POSTs share one
  cassette key, so recorded order IS the wire contract". That pin is about
  ``testing/cassettes.py``, a phase-0 module, and rpc.py is the first EVM
  writer to depend on it. It is exercised, not assumed.

* the canonical address. ``chains/evm.py::normalize_address``,
  ``sources/evm/etherscan.py`` and ``sources/solana/rpc.py``'s docstring all
  say the same thing: the EVM source canonicalizes hex to lower case. rpc.py
  is the newest producer of an on-the-wire address.

Every fixture here can express the pinned behaviour and its negation, and
each test names the input that flips it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from auradefi.chains.evm import normalize_address
from auradefi.errors import SourceError
from auradefi.sources.evm.codec.keccak import keccak256
from auradefi.sources.evm.rpc import BatchResult, EvmRpc, block_tag
from auradefi.testing.cassettes import load

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_PATH = REPO_ROOT / "tests" / "golden" / "test_phase11_reader.py"

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_CHECKSUMMED = "0xA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
DECIMALS = "0x313ce567"

#: The call surface RELEASE_0.2.0 §4 enumerates, plus the two names the
#: multicall and log legs add. Signatures, not selectors: deriving the
#: selector here is the point.
SPEC_SIGNATURES = (
    "balanceOf(address)",
    "decimals()",
    "totalSupply()",
    "token0()",
    "token1()",
    "getReserves()",
    "allPairsLength()",
    "allPairs(uint256)",
    "slot0()",
    "positions(uint256)",
    "getPool(address,address,uint24)",
    "tokenOfOwnerByIndex(address,uint256)",
    "getUserAccountData(address)",
    "getExchangeRate()",
    "aggregate3((address,bool,bytes)[])",
)


def golden() -> object:
    """The wave-4 gate module, imported for its pins and its fixture only.

    Read as data. Importing the file is what makes this a seam test: the
    values compared below are the ones that gate actually carries, not a
    copy of them that could drift.
    """
    spec = importlib.util.spec_from_file_location("_phase11_golden_pins", GOLDEN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    """A response written only from what the seam needs: a status and a body."""

    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _OnlyPost:
    """A client with exactly one method, ``post(url, json=...)``.

    Not an httpx.Client and not a subclass of one. Any other attribute the
    module reaches for raises, which is the whole point.
    """

    def __init__(self, payloads: list[object]) -> None:
        self._payloads = list(payloads)
        self.calls: list[tuple[str, object]] = []

    def post(self, url: str, *, json: object) -> _Response:
        self.calls.append((url, json))
        if not self._payloads:
            raise AssertionError("the client was asked for one response too many")
        return _Response(200, self._payloads.pop(0))


class _Tripwire:
    """A client that reports any attribute the constructor touches."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.touched.append(name)
        raise AssertionError(f"the constructor reached for client.{name}")


class _RecordingMiss(Exception):
    """Raised by the body-keyed replay below when a request is not recorded."""


def _body_key(body: object) -> str:
    """A request body as its exact wire identity: no key sorting.

    Strict on purpose. The wave-4 matcher canonicalises with
    ``sort_keys=True``, so this key is the tighter of the two: a params
    object whose keys arrive in the other order matches there and misses
    here, which is the declared seam read literally.
    """
    assert isinstance(body, dict), f"a single call posts an object, got {body!r}"
    return json.dumps(
        [body["method"], body["params"]], separators=(",", ":"), sort_keys=False
    )


class _BodyKeyedReplay:
    """Replay of the wave-4 recording, keyed on the request body.

    Written from the declared wire seam and nothing else. The shipped
    cassette matcher keys on method, host, path and sorted query, so it
    cannot tell these nineteen POSTs apart; that is why the wave-4 gate
    carries its own matcher, and why this file carries a third one rather
    than importing either.
    """

    def __init__(self, fixture: tuple[tuple[str, str, str], ...], tag: str) -> None:
        self.posted: list[object] = []
        self.served: dict[str, int] = {}
        self._recorded: dict[str, str] = {}
        for to, data, result in fixture:
            key = _body_key(
                {"method": "eth_call", "params": [{"to": to, "data": data}, tag]}
            )
            self._recorded[key] = result
            self.served[key] = 0

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.posted.append(body)
        key = _body_key(body)
        if key not in self._recorded:
            raise _RecordingMiss(key)
        self.served[key] += 1
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": self._recorded[key]}
        )


def _hex_quantity(value: int) -> str:
    """Minimal lowercase hex, derived by hand rather than by ``hex()``.

    The second derivation of the block tag. ``block_tag`` uses ``hex()``, so
    comparing it against ``hex()`` would compare one derivation with itself.
    """
    digits = "0123456789abcdef"
    out = ""
    while value:
        value, remainder = divmod(value, 16)
        out = digits[remainder] + out
    return "0x" + (out or "0")


def test_the_injected_client_is_reached_only_through_post_and_status_and_json() -> None:
    """Every method drives a client that has nothing but the declared seam.

    Flips to red if the module reaches for any other client attribute (say
    ``request`` or ``headers``) or any other response attribute (say ``text``
    or ``raise_for_status``): both exist on httpx.Client, so no in-repo test
    would notice, and every host-supplied client would break.
    """
    client = _OnlyPost(
        [
            {"jsonrpc": "2.0", "id": 1, "result": "0x12"},
            {"jsonrpc": "2.0", "id": 1, "result": "0x1bc16d674ec80000"},
            {"jsonrpc": "2.0", "id": 1, "result": "0x1380ad0"},
            {"jsonrpc": "2.0", "id": 1, "result": []},
            [
                {"jsonrpc": "2.0", "id": 2, "result": "0x2"},
                {"jsonrpc": "2.0", "id": 1, "result": "0x1"},
            ],
        ]
    )
    rpc = EvmRpc(client, "https://node.invalid/rpc")

    assert rpc.eth_call(USDC, DECIMALS, block_tag(20_450_000)) == "0x12"
    assert rpc.eth_get_balance(USDC) == 2_000_000_000_000_000_000
    assert rpc.eth_block_number() == 20_450_000
    assert rpc.eth_get_logs({"address": USDC}) == []
    results = rpc.batch([("eth_blockNumber", []), ("eth_blockNumber", [])])
    assert [item.result for item in results] == ["0x1", "0x2"]

    assert [url for url, _payload in client.calls] == ["https://node.invalid/rpc"] * 5
    assert client.calls[0][1] == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": USDC, "data": DECIMALS}, "0x1380ad0"],
    }


def test_the_constructor_touches_the_injected_client_not_at_all() -> None:
    """No I/O in the constructor, read strictly: not one attribute access.

    Flips to red the moment the constructor probes the client, for instance
    to read a base_url or to install a header.
    """
    tripwire = _Tripwire()
    EvmRpc(tripwire, "https://node.invalid/rpc")
    assert tripwire.touched == []


def test_every_wave_four_recorded_read_is_served_by_the_body_eth_call_emits() -> None:
    """The nineteen hand-packed requests are the ones rpc.py actually posts.

    Both sides derive the same wire body: the fixture by hand before rpc.py
    existed, rpc.py from its params seam. Flips to red on an added 'from'
    key, on a params object whose keys swap order, on a block tag that is not
    the pin, or on calldata that is case-folded on the way out.
    """
    pins = golden()
    replay = _BodyKeyedReplay(pins.FIXTURE, pins.BLOCK_TAG)
    rpc = EvmRpc(replay.client(), pins.NODE_URL)

    for to, data, result in pins.FIXTURE:
        assert rpc.eth_call(to, data, block_tag(pins.BLOCK)) == result

    assert sorted(replay.served.values()) == [1] * 19
    for body in replay.posted:
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1
        assert body["method"] == "eth_call"
        assert list(body["params"][0]) == ["to", "data"]
        assert body["params"][1] == pins.BLOCK_TAG


def test_a_checksummed_target_still_matches_the_lowercase_recording() -> None:
    """The lowercasing in ``eth_call`` is what lets a host pass a mixed-case
    address to a fixture recorded in the canonical form.

    Flips to red if the lowercasing moves out of ``eth_call``, which is the
    change that would silently break every recorded EVM read.
    """
    pins = golden()
    replay = _BodyKeyedReplay(pins.FIXTURE, pins.BLOCK_TAG)
    rpc = EvmRpc(replay.client(), pins.NODE_URL)
    to, data, result = pins.FIXTURE[0]
    assert rpc.eth_call(to.upper().replace("0X", "0x"), data, pins.BLOCK_TAG) == result


def test_the_replay_is_blind_to_nothing_that_carries_the_read_s_identity() -> None:
    """The negation controls for the fixture above, each one an input flip.

    A fake that ignored an argument would make the previous test pass for the
    wrong reason, so each of these must MISS: the block pin dropped, the
    calldata case-folded, and the two same-typed arguments swapped. That last
    one is the one no type checker catches: ``eth_call`` takes ``to`` then
    ``data``, both ``str``, and a swap posts a plausible body.
    """
    pins = golden()
    to, data, _result = pins.FIXTURE[0]

    replay = _BodyKeyedReplay(pins.FIXTURE, pins.BLOCK_TAG)
    rpc = EvmRpc(replay.client(), pins.NODE_URL)
    with pytest.raises(_RecordingMiss):
        rpc.eth_call(to, data, block_tag(None))

    replay = _BodyKeyedReplay(pins.FIXTURE, pins.BLOCK_TAG)
    rpc = EvmRpc(replay.client(), pins.NODE_URL)
    with pytest.raises(_RecordingMiss):
        rpc.eth_call(to, data.upper().replace("0X", "0x"), pins.BLOCK_TAG)

    replay = _BodyKeyedReplay(pins.FIXTURE, pins.BLOCK_TAG)
    rpc = EvmRpc(replay.client(), pins.NODE_URL)
    with pytest.raises(_RecordingMiss):
        rpc.eth_call(data, to, pins.BLOCK_TAG)


def test_every_selector_in_the_wave_four_fixture_is_a_keccak_of_a_spec_signature() -> (
    None
):
    """Two derivations of one value: the hand-packed selector and keccak256.

    Flips to red if keccak.py's pad byte, lane order or squeeze order moves,
    and equally if the fixture starts carrying a call the spec's surface
    table does not name.
    """
    pins = golden()
    by_selector = {
        keccak256(signature.encode())[:4].hex(): signature
        for signature in SPEC_SIGNATURES
    }
    seen = {data[2:10] for _to, data, _result in pins.FIXTURE}
    assert len(seen) == 10
    unexplained = sorted(selector for selector in seen if selector not in by_selector)
    assert unexplained == [], f"selectors no spec signature derives: {unexplained}"
    assert by_selector["70a08231"] == "balanceOf(address)"
    assert by_selector["e6aa216c"] == "getExchangeRate()"
    assert by_selector["bf92857c"] == "getUserAccountData(address)"


def test_not_one_fixture_selector_survives_the_sha3_pad_byte() -> None:
    """The pad byte is load-bearing at the seam, not only inside the module.

    If keccak256 were the stdlib's SHA3-256, every selector in the fixture
    would address a different function and this set would be non-empty.
    """
    pins = golden()
    sha3 = {
        hashlib.sha3_256(signature.encode()).hexdigest()[:8]
        for signature in SPEC_SIGNATURES
    }
    seen = {data[2:10] for _to, data, _result in pins.FIXTURE}
    assert seen & sha3 == set()


def test_the_pinned_block_travels_as_the_tag_two_derivations_agree_on() -> None:
    """``block_tag`` against a hand-rolled hex, the gate's pin and the fixture.

    Flips to red on a zero-padded tag, an uppercase tag, or a block-zero read
    that answers 'latest' instead of '0x0'.
    """
    pins = golden()
    assert block_tag(pins.BLOCK) == _hex_quantity(pins.BLOCK) == pins.BLOCK_TAG
    assert pins.BLOCK_TAG == "0x1380ad0"
    assert int(pins.BLOCK_TAG, 16) == pins.BLOCK == 20_450_000
    assert block_tag(0) == _hex_quantity(0) == "0x0"
    assert block_tag(255) == _hex_quantity(255) == "0xff"
    assert block_tag(None) == "latest"
    for interaction in pins._cassette_document()["interactions"]:
        assert interaction["request"]["body"]["params"][1] == block_tag(pins.BLOCK)


def test_the_eth_call_default_block_is_the_tag_block_tag_derives_for_none() -> None:
    """One value, two places: the default argument and ``block_tag(None)``.

    Flips to red if either side starts saying 'pending' or ''.
    """
    client = _OnlyPost([{"jsonrpc": "2.0", "id": 1, "result": "0x12"}])
    EvmRpc(client, "https://node.invalid/rpc").eth_call(USDC, DECIMALS)
    assert client.calls[0][1]["params"][1] == block_tag(None) == "latest"


def test_the_shipped_cassette_harness_serves_the_node_path_in_recorded_order(
    tmp_path: Path,
) -> None:
    """rpc.py is the first EVM writer to lean on ``testing/cassettes.py``.

    DECISIONS.md pins that JSON-RPC POSTs share one cassette key, so recorded
    order IS the wire contract. Exercised rather than assumed: the two reads
    come back in recorded order through the shipped loader.

    The third read is the hazard the pin implies and the wave-4 gate refuses
    to inherit: an unrecorded read replays the LAST recording instead of
    missing, so a reader whose call count drifts is served a stale answer
    with no error. Flips to red the day that matcher starts keying on a body.
    """
    document = {
        "interactions": [
            {
                "request": {"method": "POST", "url": "https://node.invalid/rpc"},
                "response": {
                    "status": 200,
                    "json": {"jsonrpc": "2.0", "id": 1, "result": "0x6"},
                },
            },
            {
                "request": {"method": "POST", "url": "https://node.invalid/rpc"},
                "response": {
                    "status": 200,
                    "json": {"jsonrpc": "2.0", "id": 1, "result": "0x12"},
                },
            },
        ]
    }
    path = tmp_path / "node.json"
    path.write_text(json.dumps(document))
    rpc = EvmRpc(load(path).client(), "https://node.invalid/rpc")

    assert rpc.eth_call(USDC, DECIMALS) == "0x6"
    assert rpc.eth_call(USDC, "0x18160ddd") == "0x12"
    assert rpc.eth_call("0x000000000000000000000000000000000000dead", DECIMALS) == "0x12"


def test_eth_get_logs_forwards_the_filter_object_byte_for_byte() -> None:
    """The filter crosses this boundary unvalidated, so logs.py owns its keys.

    Including the block numbers: an int stays an int on the wire, which a
    node refuses, so ``logs.py`` must call ``block_tag`` itself. Flips to red
    if rpc.py starts converting them, which would make the same conversion in
    logs.py a double encode.
    """
    client = _OnlyPost([{"jsonrpc": "2.0", "id": 1, "result": []}])
    filter_object = {
        "fromBlock": 20_450_000,
        "toBlock": block_tag(20_450_100),
        "address": USDC_CHECKSUMMED,
        "topics": [None, []],
    }
    rows = EvmRpc(client, "https://node.invalid/rpc").eth_get_logs(filter_object)
    assert rows == []
    assert client.calls[0][1]["params"] == [filter_object]
    assert client.calls[0][1]["params"][0]["fromBlock"] == 20_450_000
    assert client.calls[0][1]["params"][0]["address"] == USDC_CHECKSUMMED


def test_both_node_reads_put_the_packages_canonical_address_on_the_wire() -> None:
    """One canonical form, two producers on the same transport.

    ``chains/evm.py::normalize_address`` is the package's canonical EVM
    address, ``etherscan.py`` lowercases at its own boundary, and
    ``solana/rpc.py`` names the rule from the other side: "unlike the EVM
    source which canonicalizes hex to lower case". ``eth_call`` honours it
    and ``eth_get_balance`` does not, so one class puts two different
    identities for one address on one wire.

    Flips to red either way: lowercase both and it passes, lowercase neither
    and the first assertion fails instead.
    """
    client = _OnlyPost(
        [
            {"jsonrpc": "2.0", "id": 1, "result": "0x12"},
            {"jsonrpc": "2.0", "id": 1, "result": "0x1bc16d674ec80000"},
        ]
    )
    rpc = EvmRpc(client, "https://node.invalid/rpc")
    rpc.eth_call(USDC_CHECKSUMMED, DECIMALS)
    rpc.eth_get_balance(USDC_CHECKSUMMED)

    canonical = normalize_address(USDC_CHECKSUMMED)
    assert client.calls[0][1]["params"][0]["to"] == canonical
    assert client.calls[1][1]["params"][0] == canonical, (
        "eth_getBalance puts a mixed-case address on the wire while eth_call "
        "puts the canonical lowercase one, so one address has two wire "
        "identities on one transport"
    )


def test_batch_ids_restart_at_one_and_a_single_call_is_always_id_one() -> None:
    """The id scheme the wave-4 recording keys on, across three posts.

    Every recorded interaction in that fixture answers id 1, so a single call
    that ever posted id 2 would be answered by a recording it did not ask
    for. Flips to red on an id counter that persists across batches.
    """
    client = _OnlyPost(
        [
            [
                {"jsonrpc": "2.0", "id": 1, "result": "0x1"},
                {"jsonrpc": "2.0", "id": 2, "result": "0x2"},
            ],
            [{"jsonrpc": "2.0", "id": 1, "result": "0x3"}],
            {"jsonrpc": "2.0", "id": 1, "result": "0x4"},
        ]
    )
    rpc = EvmRpc(client, "https://node.invalid/rpc")
    rpc.batch([("eth_blockNumber", []), ("eth_blockNumber", [])])
    rpc.batch([("eth_blockNumber", [])])
    rpc.eth_block_number()

    assert [item["id"] for item in client.calls[0][1]] == [1, 2]
    assert [item["id"] for item in client.calls[1][1]] == [1]
    assert client.calls[2][1]["id"] == 1
    pins = golden()
    recorded = pins._cassette_document()["interactions"]
    assert {entry["request"]["body"]["id"] for entry in recorded} == {1}


def test_a_batch_item_the_node_answered_with_null_is_declared_not_returned() -> None:
    """The only carrier phase 11 has for a failure, on its least obvious input.

    A null result is DECLARED as an error rather than handed on as a value,
    so no consumer can read a JSON null as an answer. Flips to red if the
    carrier ever passes ``result=None`` through, which would also raise from
    ``BatchResult`` itself.
    """
    client = _OnlyPost([[{"jsonrpc": "2.0", "id": 1, "result": None}]])
    rpc = EvmRpc(client, "https://node.invalid/rpc")
    (item,) = rpc.batch([("eth_getTransactionReceipt", ["0x00"])])
    assert item.result is None
    assert item.error is not None
    assert isinstance(item, BatchResult)


def test_a_batch_and_a_single_call_refuse_through_the_same_door() -> None:
    """One transport, one failure type, whichever path reached it.

    Consumers write ``except SourceError`` once. Flips to red if either path
    starts letting an httpx exception or a JSON decode error escape.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    rpc = EvmRpc(httpx.Client(transport=httpx.MockTransport(refuse)), "https://n.invalid")
    with pytest.raises(SourceError):
        rpc.eth_call(USDC, DECIMALS)
    with pytest.raises(SourceError):
        rpc.batch([("eth_blockNumber", [])])


def test_the_multicall_carrier_is_not_the_batch_carrier_despite_the_seam_text() -> None:
    """``CallResult`` is declared as a mirror of ``BatchResult``. It is not.

    ``BatchResult`` refuses a carrier with both members set. The wave-4 gate
    requires ``CallResult(False, b"")`` and ``CallResult(True, <32 bytes>)``,
    both of which set both members, so the aggregate3 carrier must NOT
    inherit the exactly-one invariant. Skips until multicall.py lands, then
    binds. Flips to red if that invariant is copied across.
    """
    multicall = pytest.importorskip("auradefi.sources.evm.multicall")
    assert multicall.CallResult(False, b"").data == b""
    assert multicall.CallResult(True, b"\x00" * 32).success is True
    with pytest.raises(Exception):
        BatchResult(None, None)
    with pytest.raises(Exception):
        BatchResult("0x1", "boom")
