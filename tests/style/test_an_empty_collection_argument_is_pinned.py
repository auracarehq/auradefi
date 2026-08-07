"""What an EMPTY caller collection does is a contract, and it is pinned here.

MOTIVATING FINDING (0.2.0 phase 11, `src/auradefi/sources/evm/logs.py:188`,
major). `scan_logs` decides whether to send the filter's `address` key with
`if address_filter is not None`. That reads the caller's `None` correctly and
lets the EMPTY sequence fall straight through, so `address=[]` goes out as
`{"address": []}`, and go-ethereum's `filterLogs` skips address matching when
the list is empty (`if len(addresses) > 0 && !includes(...)`). An empty
allowlist therefore scans every log in the range instead of none. The review
mutant that rewrote `[]` to an omitted key left the whole suite green, so
nothing in the tree said which of the two requests the module meant to send.

THE CLASS, which is wider than that one line: a caller-supplied collection
reaches a query, a request or a fan-out, the omit-or-apply decision is keyed on
`is None`, and the empty case falls through into a request that WIDENS. Nothing
raises, nothing logs, and the caller gets more data than they asked for rather
than less. Every site of it in the tree is a one-line difference between "no
filter" and "a filter matching nothing", and the two spellings look identical
in review.

WHY A TABLE AND NOT A REGEX. `is not None` before a dict assignment is correct
code almost everywhere it appears (65 occurrences under `src/`), and whether
emptiness widens or narrows depends on what the remote end does with the key,
which no static check can read. A regex over this shape would fire on correct
code, and a noisy gate gets suppressed. What can be checked is the emitted
request itself, one call per site, and the calls are cheap.

BOTH DIRECTIONS ARE HERE ON PURPOSE. Three sites widen on empty and two
narrow, and the tree is only readable if each says which it is. A later change
that flips one of them, `multicall` sending an `aggregate3` of zero calls or
`defillama` fetching `/prices/current/` with no coins after the empty check is
dropped, is the same defect arriving from the other end.

Offline by construction (profile rule 9): every transport below is an
`httpx.MockTransport`, and the webhook rows touch no transport at all.
"""

from __future__ import annotations

import json

import httpx
import pytest

from auradefi.clock import FrozenClock
from auradefi.prices.oracles.defillama import DefiLlamaOracle
from auradefi.sources.evm.logs import scan_logs
from auradefi.sources.evm.multicall import Multicall3
from auradefi.sources.evm.rpc import EvmRpc
from auradefi.webhooks.deliver import WebhookStore
from auradefi.webhooks.models import EventName

URL = "https://node.invalid/v1"

#: A 20-byte address in the form every EVM boundary in the package accepts.
ADDRESS = "0x" + "ab" * 20

#: keccak256("Transfer(address,address,uint256)"), a well-formed topic0.
TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

PROJECT = "proj_0000000000000001"
HOOK_URL = "https://hooks.example.test/auradefi"
T0 = 1_754_000_000_000


def _recording_rpc() -> tuple[EvmRpc, list[dict]]:
    """An `EvmRpc` whose requests are recorded and answered with no rows."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})

    return EvmRpc(httpx.Client(transport=httpx.MockTransport(handler)), URL), bodies


def _filters(bodies: list[dict]) -> list[dict]:
    """The filter object of every recorded `eth_getLogs`."""
    return [body["params"][0] for body in bodies]


# ------------------------------------------------- empty WIDENS, deliberately


@pytest.mark.parametrize("address", [[], ()], ids=["empty-list", "empty-tuple"])
def test_an_empty_scan_logs_address_is_sent_and_is_not_an_omitted_key(address):
    """logs.py:391. The motivating site.

    `address=None` omits the key and `address=[]` sends an empty list, which a
    node reads as no address filter at all. The two requests differ in the
    wire, mean the same thing to geth, and mean opposite things to the caller,
    so the module's choice between them is pinned rather than inferred.
    """
    rpc, bodies = _recording_rpc()
    scan_logs(rpc, from_block=1000, to_block=1000, address=address)
    assert _filters(bodies) == [
        {"fromBlock": "0x3e8", "toBlock": "0x3e8", "address": []}
    ]

    omitted, omitted_bodies = _recording_rpc()
    scan_logs(omitted, from_block=1000, to_block=1000, address=None)
    assert _filters(omitted_bodies) == [{"fromBlock": "0x3e8", "toBlock": "0x3e8"}]


@pytest.mark.parametrize("slot", [[], ()], ids=["empty-list", "empty-tuple"])
def test_an_empty_scan_logs_topic_or_slot_is_sent_as_an_empty_array(slot):
    """logs.py:393. The sibling of the row above, one level deeper.

    An empty OR list is the wildcard `null` already means, so `topics=([],)`
    matches every topic in slot 0 while `topics=()` omits the key. Rewriting
    either spelling into the other would hide a caller's empty rule set behind
    a request that reads as intentional.
    """
    rpc, bodies = _recording_rpc()
    scan_logs(rpc, from_block=1000, to_block=1000, topics=(slot,))
    assert _filters(bodies) == [
        {"fromBlock": "0x3e8", "toBlock": "0x3e8", "topics": [[]]}
    ]

    omitted, omitted_bodies = _recording_rpc()
    scan_logs(omitted, from_block=1000, to_block=1000, topics=())
    assert _filters(omitted_bodies) == [{"fromBlock": "0x3e8", "toBlock": "0x3e8"}]


def test_an_empty_webhook_subscription_receives_every_event():
    """deliver.py:191. The same class away from the wire, in a fan-out.

    `Endpoint.events` is an allowlist and an empty one subscribes to all seven
    names, so `POST /webhooks/endpoints` with `"events": []` registers the
    loudest endpoint available rather than a silent one. It is the documented
    reading of the field and it is the reading a reviewer least expects, which
    is the reason it is pinned in a gate as well as in the store's own tests.
    """
    store = WebhookStore(entropy=lambda n: "ab" * n)
    store.register_endpoint(PROJECT, HOOK_URL, [], FrozenClock(T0))
    for index, name in enumerate(EventName, start=1):
        store.emit(PROJECT, name, {"kind": "address"}, FrozenClock(T0 + index))
    assert len(store.deliveries(PROJECT)) == len(list(EventName))


# --------------------------------------------------- empty NARROWS, to nothing


def test_an_empty_multicall_batch_issues_no_request_at_all():
    """multicall.py. Empty in, empty out, and zero cost.

    An `aggregate3` of zero calls is a request whose answer is known, so the
    guard that returns `()` before the transport is the narrowing half of this
    class. Drop it and a refresh whose batch came out empty pays for a node
    call that can only decode to nothing.
    """
    rpc, bodies = _recording_rpc()
    assert Multicall3(rpc, ADDRESS).aggregate3([]) == ()
    assert bodies == []


def test_no_priceable_id_costs_no_defillama_request():
    """defillama.py:126. The same narrowing, over HTTP rather than JSON-RPC.

    `/prices/current/` with no coins after it is a different URL and not an
    empty question, so the oracle answers `{}` itself. This also covers an
    input of ids that are all unmappable, which reaches the same guard with a
    non-empty argument.

    Two things hold this path, and the row is written to need both: deleting
    the `if not ids_by_key` return alone leaves it green, because `chunk_keys`
    of nothing is no chunks. The mutant that kills this row deletes that guard
    and widens the chunk range to `max(len(ordered), 1)`, which is the shape a
    later "always emit one chunk" refactor arrives in.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"coins": {}})

    oracle = DefiLlamaOracle(httpx.Client(transport=httpx.MockTransport(handler)))
    assert oracle.usd_prices([]) == {}
    assert oracle.usd_prices(["eip155:99999/erc20:0xdead"]) == {}
    assert seen == []


# ------------------------------------- empty reaches the wire UNDECLARED
#
# ROUND-2 FINDING, same class from its other end (0.2.0 phase 11, major):
# `logs.py` documented what an empty ADDRESS sequence does to a node and left
# the empty TOPIC OR slot, one branch away in the same request builder,
# unstated. The habit the sweep is guarding is not "empty widens": it is
# "one sibling of a pair got the sentence and the other did not". So the pair
# below is checked as a pair. `Multicall3.aggregate3` says "An empty ``calls``
# issues ZERO requests and returns ``()``" and guards it; `EvmRpc.batch`, the
# other batch door in the same package, says nothing about an empty sequence
# and posts a bare `[]`, which JSON-RPC 2.0 §6 makes an Invalid Request: the
# node answers with a single error object and the caller gets a SourceError
# blaming the wire for a batch that never had anything in it.


def test_an_empty_evm_rpc_batch_is_guarded_or_declared():
    """rpc.py:278. Either behaviour is defensible; silence is not.

    The row deliberately accepts BOTH resolutions, because which one
    `EvmRpc.batch` should take is the owning order's call and not this gate's:
    return `()` without a request, as `aggregate3` does, or keep posting the
    empty array and say so in the docstring. What it refuses is the third
    state, where a caller has to read the transport to find out. A one-line
    sentence naming the empty case turns this green.
    """
    bodies: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # A batch answer is a JSON array, so this is not `_recording_rpc`'s
        # single-envelope handler. An accommodating node is modelled on
        # purpose: the row is about the request going out, not about how
        # gracefully the answer to a malformed batch comes back.
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=[])

    rpc = EvmRpc(httpx.Client(transport=httpx.MockTransport(handler)), URL)
    rpc.batch(())
    declared = "empty" in (EvmRpc.batch.__doc__ or "").lower()
    assert bodies == [] or declared, (
        "EvmRpc.batch sends an empty JSON-RPC array for an empty request "
        "sequence and its docstring never says so, while its sibling "
        "Multicall3.aggregate3 documents and guards the same case"
    )


def test_a_topic_only_scan_still_sends_no_address_key():
    """logs.py:391 once more, from the direction a caller reaches it.

    A scan filtered on topics alone is the ordinary use of this module, and it
    must not acquire an `address` member holding an empty list on the way out:
    that would be the widening request above, sent by a caller who never asked
    for one.
    """
    rpc, bodies = _recording_rpc()
    scan_logs(rpc, from_block=1000, to_block=1000, topics=(TOPIC,))
    assert _filters(bodies) == [
        {"fromBlock": "0x3e8", "toBlock": "0x3e8", "topics": [TOPIC]}
    ]
