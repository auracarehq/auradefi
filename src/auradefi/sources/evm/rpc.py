"""EVM JSON-RPC 2.0 transport: single calls and an id-matched batch.

RELEASE_0.2.0 §4, closing #3. Before this module every EVM read went
through the Etherscan V2 aggregator, so there was no direct-node path
and no way to ask a contract a question. This is that path and nothing
more: it knows the JSON-RPC envelope and knows nothing about ABI
encoding, positions or fiat (SPEC §3.3). ``codec/abi.py`` builds the
calldata, ``multicall.py`` and ``logs.py`` sit on top, ``reader.py`` is
the concrete ContractReader.

Every single request is a POST of exactly

    {"jsonrpc": "2.0", "id": 1, "method": <method>, "params": <params>}

A batch posts a JSON ARRAY of the same objects with ids ``1..N`` in
request order, restarting at 1 for every batch, matched back by ``id``
and returned in REQUEST order. Position matching is the defect this
module is written against: a node may answer a batch in any order.

NO retry and NO rate limiting, here or anywhere else in the package. No
network at import time and none in a constructor: the ``httpx.Client`` is
REQUIRED and injected so cassettes plug in, and ``url`` has no default
because a node endpoint is host configuration. Both follow
``sources/solana/rpc.py`` and ``sources/evm/etherscan.py``.

docs/internal/DECISIONS.md, pinned: "JSON-RPC POSTs share one cassette
key, so recorded order IS the wire contract." The replay harness matches
on method, host, path and sorted query only, so every POST to one node
URL lands on a single key and is served in recorded order.

Amounts and block numbers arrive as hex STRINGS and parse with
``int(x, 16)``, never through float (SPEC rules #1/#2). The ``eth_call``
target address is lowercased, as everywhere else in the EVM source.

Every failure raises :class:`auradefi.errors.SourceError` and nothing
else, kept at the PUBLIC boundary and not at the last statement before
the socket, by one rule: whatever touches a caller's argument first is
what refuses it. ``to`` and each batch ``(method, params)`` pair are
CONSUMED here, one lowercased and one unpacked, so both are checked on
entry. Every other argument is FORWARDED untouched and so is checked
where it is first read, at the JSON encoding one statement before the
send, which names the payload and not the node: an argument JSON cannot
hold is a caller bug, and a package with no retry should say so.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

import httpx

from auradefi.errors import SourceError, require_str
from auradefi.sources.evm.jsonrpc import (
    BatchResult,
    _batch_result,
    _error_text,
    _is_pair_like,
    _quantity,
    block_tag,
)


class EvmRpc:
    """EVM JSON-RPC 2.0 over an injected ``httpx.Client``.

    The client is REQUIRED and injected so cassettes plug in, ``url`` is
    required because a node endpoint is host configuration, and the
    constructor performs no I/O.
    """

    def __init__(self, client: httpx.Client, url: str) -> None:
        """Bind the injected client and endpoint URL. No I/O.

        ``url`` is refused HERE, not by widening ``_post``'s except
        tuple. Catching ``TypeError`` around the send did keep the
        promise, and it also swallowed any TypeError this module got
        wrong itself, dressed up as a node failure. A type check at the
        door keeps the promise without buying that.
        """
        self._client = client
        self._url = require_str(url, "url", SourceError)

    def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        """``eth_call`` against ``to`` with ``data``; the result hex string.

        Posts params exactly ``[{"to": to.lower(), "data": data}, block]``.
        That array is what the wave-4 golden fixture keys on, so key
        order, address casing and an added ``"from"`` key are all wire
        contract. ``data`` is a lowercase ``0x``-prefixed hex string and
        goes on the wire verbatim; ``block`` is a tag from
        :func:`block_tag`. The result is returned as received, with no
        case folding and no padding change: decoding the word is
        ``codec/abi.py``'s job.

        Raises:
            SourceError: on a ``to`` that is not a string, and on any
                transport, envelope or shape failure, including a result
                that is not a string.
        """
        # `to` is CONSUMED here, so it is refused here: a bare to.lower()
        # leaks AttributeError past the SourceError promise, and half a
        # taxonomy is worse than none, hiding the gap from anyone who
        # writes `except SourceError` and believes it.
        if not isinstance(to, str):
            raise SourceError(f"eth_call needs a string target address: {to!r}")
        result = self._call("eth_call", [{"to": to.lower(), "data": data}, block])
        if not isinstance(result, str):
            raise SourceError(f"eth_call result must be a string: {result!r}")
        return result

    def eth_get_balance(self, address: str, block: str = "latest") -> int:
        """The address's native balance in wei.

        Calls ``eth_getBalance`` with params ``[address, block]``. The
        result is a hex STRING and parses with ``int(x, 16)``, never
        through float, so a JSON integer result is a malformed envelope
        and not a lenient success.

        Raises:
            SourceError: on any transport or envelope failure, on a
                result that is not a string, and on a string that is not
                ``0x``-prefixed hex.
        """
        # Lowercased for the same reason eth_call lowercases `to`: the
        # params array is the cassette match key, so a mixed-case address
        # here and a canonical one there are two wire identities for one
        # account, and a recording made through one path misses through
        # the other. EIP-55 casing is a checksum, never an identity.
        address = require_str(address, "address", SourceError).lower()
        return _quantity(
            self._call("eth_getBalance", [address, block]), "eth_getBalance"
        )

    def eth_block_number(self) -> int:
        """The chain head as an ``int``.

        Calls ``eth_blockNumber`` with params ``[]``. The result is a hex
        STRING and parses with ``int(x, 16)``.

        Raises:
            SourceError: on any transport or envelope failure and on a
                result that is not a hex string.
        """
        return _quantity(self._call("eth_blockNumber", []), "eth_blockNumber")

    def eth_get_logs(self, filter_object: dict) -> list[dict]:
        """``eth_getLogs`` rows for ``filter_object``, raw and untyped.

        The filter dict goes on the wire unvalidated as params
        ``[filter_object]`` and the rows come back exactly as received,
        in received order: ``logs.py`` owns both the filter keys and the
        row typing.

        Raises:
            SourceError: on any transport or envelope failure and when
                the result is not a list.
        """
        result = self._call("eth_getLogs", [filter_object])
        if not isinstance(result, list):
            raise SourceError(
                f"eth_getLogs result must be a list, got {type(result).__name__}"
            )
        return result

    def batch(self, requests: Sequence[tuple[str, list]]) -> tuple[BatchResult, ...]:
        """Post ``(method, params)`` pairs as one JSON-RPC array.

        Ids are ``1..N`` assigned in request order and restart at 1 for
        every batch. Each response is looked up by its ``id``, never by
        its array position, and the returned tuple is in REQUEST order.

        An EMPTY ``requests`` returns ``()`` and issues NO request, the
        same answer ``Multicall3.aggregate3`` gives its own empty case.
        A node may accept an empty JSON-RPC array or reject it as
        invalid, so posting one asks a question with no agreed answer.

        An item carrying an ``error`` member becomes a declared
        :class:`BatchResult` failure and does NOT raise: one reverting
        call must not void the batch. An item matched to its request but
        carrying no usable ``result`` is declared the same way, so a
        ``null`` result is reported and never read as an answer.

        Raises:
            SourceError: on ``requests`` that is not a sequence and on an
                entry that is not a ``(method, params)`` pair, on any
                transport or envelope failure, on a body that is not a
                list, on a length that differs from the request's, on an
                item that is not an object or carries no ``id``, on a
                duplicate ``id``, and on an ``id`` not in the batch.
        """
        # Each entry is CHECKED, never unpacked hopefully: a bare method
        # name, a three-element entry or a plain string argument otherwise
        # reaches a caller as ValueError, past the SourceError promise.
        if not _is_pair_like(requests):
            raise SourceError(f"evm rpc batch needs a sequence of pairs: {requests!r}")
        if len(requests) == 0:
            return ()
        payload: list[dict] = []
        for number, request in enumerate(requests, start=1):
            if not _is_pair_like(request) or len(request) != 2:
                raise SourceError(
                    f"evm rpc batch request {number} must be a "
                    f"(method, params) pair: {request!r}"
                )
            method, params = request
            payload.append(
                {"jsonrpc": "2.0", "id": number, "method": method, "params": params}
            )
        body = self._post(payload)
        if not isinstance(body, list):
            raise SourceError(
                f"evm rpc batch body must be a list, got {type(body).__name__}"
            )
        if len(body) != len(payload):
            raise SourceError(
                f"evm rpc batch of {len(payload)} was answered with {len(body)} items"
            )
        answers = self._by_id(body, len(payload))
        # By id, never by position: a compliant node may answer a batch in
        # any order, and the equal lengths plus the distinct in-range ids
        # above make every request number present here.
        return tuple(
            _batch_result(answers[number]) for number in range(1, len(payload) + 1)
        )

    @staticmethod
    def _by_id(body: list, count: int) -> dict[int, dict]:
        """The batch's items keyed by their ``id``, one per request.

        Raises:
            SourceError: on an item that is not an object, an ``id`` that
                is not an int, a duplicate ``id``, one outside 1..count.
        """
        answers: dict[int, dict] = {}
        for item in body:
            if not isinstance(item, dict):
                raise SourceError(
                    f"evm rpc batch item must be an object, got {type(item).__name__}"
                )
            number = item.get("id")
            # bool is an int subclass, and an id of True matches nothing.
            if isinstance(number, bool) or not isinstance(number, int):
                raise SourceError(f"evm rpc batch item carries no usable id: {item!r}")
            if number in answers:
                raise SourceError(f"evm rpc batch answered id {number} twice")
            if not 1 <= number <= count:
                raise SourceError(
                    f"evm rpc batch answered id {number}, outside the 1..{count} asked"
                )
            answers[number] = item
        return answers

    def _call(self, method: str, params: list) -> object:
        """One JSON-RPC POST; the envelope's ``result`` member, untyped.

        Posts exactly ``{"jsonrpc": "2.0", "id": 1, "method": method,
        "params": params}``. Every single call carries id 1: batching is
        the only place ids climb. The result's shape belongs to the
        caller, so it is returned as received.

        Raises:
            SourceError: on a transport failure, a non-2xx status, a
                non-JSON body, a body that is not an object, an ``error``
                member (its ``code`` and ``message`` are embedded in the
                exception text), or a missing ``result``.
        """
        body = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        if not isinstance(body, dict):
            raise SourceError(
                f"evm rpc body must be an object, got {type(body).__name__}"
            )
        error = body.get("error")
        if error is not None:
            raise SourceError(f"evm rpc {method} error: {_error_text(error)}")
        if "result" not in body:
            raise SourceError(f"evm rpc {method} response carries no result")
        return body["result"]

    def _post(self, payload: object) -> object:
        """POST ``payload`` as JSON to the node; the decoded body.

        The one transport door, shared by the single and batch paths, so
        both refuse the same way, and exactly one request is issued.

        Raises:
            SourceError: on a payload JSON cannot encode, on any refusal
                of the send itself (a transport failure, an unusable
                ``url``), a non-2xx status, and a body that is not JSON.
        """
        try:
            json.dumps(payload)
        except (TypeError, ValueError) as exc:
            # Encoded twice, deliberately. httpx raises this same TypeError
            # from `json=`, but from inside the send, where it cannot be
            # told apart from a TypeError this module got wrong itself.
            # Asking first keeps them apart: a forwarded argument JSON
            # cannot hold is named as one, and an internal TypeError stays
            # a TypeError instead of being dressed up as a node failure.
            raise SourceError(f"evm rpc cannot encode the request: {exc}") from exc
        try:
            response = self._client.post(self._url, json=payload)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            # httpx.HTTPError alone under-catches this door. In httpx 0.28
            # InvalidURL descends from Exception, not HTTPError, and a
            # scheme-less url ("localhost:8545", the likeliest local-node
            # typo) surfaces as urllib's ValueError("unknown url type")
            # from cookie extraction. Both are host configuration. !r
            # keeps the exception's type and text, `from` its traceback.
            raise SourceError(f"evm rpc request failed: {exc!r}") from exc
        if not 200 <= response.status_code < 300:
            raise SourceError(f"evm rpc HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError("evm rpc returned a non-JSON body") from exc
