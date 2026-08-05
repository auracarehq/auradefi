"""Endpoint registry + host-scheduled durable delivery (SPEC §7.3, rule #8).

Zerion retries 3x over ~60s and then drops the event, and hand-whitelists
each callback URL; Vezgo authenticates by source-IP allowlist. This suite
pins the opposite: six attempts over exactly 24h from an injected client,
a dead-letter row that survives, and NO allowlist anywhere — a webhook to
``http://127.0.0.1:9000/hook`` registers like any other.

All HTTP is ``httpx.MockTransport``; the autouse socket guard in
tests/conftest.py fails the run if anything reaches a real socket.

Golden ids/bodies/signatures were derived INDEPENDENTLY via ``python3
-c`` from the algorithms pinned in docs/internal/DECISIONS.md — see the module
docstring of tests/webhooks/test_models.py for the formulas. With
``entropy = lambda n: "ab" * n`` the endpoint secret is ``"ab" * 32``.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from auradefi.clock import FrozenClock
from auradefi.errors import ConflictError, NotFoundError, ValidationError
from auradefi.webhooks.deliver import Deliverer, WebhookStore
from auradefi.webhooks.models import (
    MAX_ATTEMPTS,
    Delivery,
    DeliveryStatus,
    EventName,
    event_id,
)

from auradefi.webhooks.sign import sign, verify_signature
from auradefi.webhooks.urls import validate_endpoint_url

WEBHOOKS_SRC = (
    Path(__file__).resolve().parents[2] / "src" / "auradefi" / "webhooks"
)

PROJECT = "proj_0000000000000001"
OTHER_PROJECT = "proj_0000000000000002"
URL = "https://hooks.example.test/auradefi"
URL_A = "https://a.example.test/hook"
URL_B = "https://b.example.test/hook"
T0 = 1_754_000_000_000
DATA = {"connection_id": "conn_abc123", "kind": "address"}
SECRET = "ab" * 32

GOLDEN_ENDPOINT_ID = "whe_a81d5036ec375faa"
GOLDEN_EVENT_ID = "evt_490b3195618c4099"
GOLDEN_DELIVERY_ID = "dlv_cb33eb38d1b7aa44"
GOLDEN_DELIVERY_ID_A = "dlv_601b1adeba3d556e"
GOLDEN_DELIVERY_ID_B = "dlv_43cbc1a1314bd1bf"
GOLDEN_BODY = (
    '{"created_at_ms":1754000000000,'
    '"data":{"connection_id":"conn_abc123","kind":"address"},'
    '"delivery_id":"dlv_cb33eb38d1b7aa44",'
    '"event_id":"evt_490b3195618c4099",'
    '"type":"connection.created"}'
)
# sign("ab"*32, 1754000000000, GOLDEN_BODY) — derived independently.
GOLDEN_SIGNATURE = "v1=e12eb2b0c8ad2d8b52078c3500f4a1b8e330d41d65d0d8f56ce1503240a5012d"

# A NESTED payload, for the mutation tests: a shallow copy of the caller's
# dict shares this inner mapping, so retry byte-identity is only real if
# the store snapshots it. Ids derived independently from the DECISIONS
# formulas over data == {"connection_id":"conn_abc123","meta":{"kind":"address"}}.
NESTED_DATA = {"connection_id": "conn_abc123", "meta": {"kind": "address"}}
GOLDEN_NESTED_EVENT_ID = "evt_ae6ee9b058276f0e"
GOLDEN_NESTED_DELIVERY_ID = "dlv_09316c59d22a9367"
GOLDEN_NESTED_BODY = (
    '{"created_at_ms":1754000000000,'
    '"data":{"connection_id":"conn_abc123","meta":{"kind":"address"}},'
    '"delivery_id":"dlv_09316c59d22a9367",'
    '"event_id":"evt_ae6ee9b058276f0e",'
    '"type":"connection.created"}'
)

# A payload containing a JSON ARRAY. The store snapshots lists as TUPLES,
# which json.dumps cannot encode, so the tuple→list round-trip is the
# difference between these bytes and a TypeError inside tick. Ids, body and
# signature derived independently from the DECISIONS formulas over
# data == {"connection_id":"conn_abc123","empty":[],"xs":[1,2,{"a":1,"b":2}]}.
LIST_PAYLOAD = {
    "connection_id": "conn_abc123",
    "xs": [1, 2, {"b": 2, "a": 1}],
    "empty": [],
}
GOLDEN_LIST_EVENT_ID = "evt_4e275242f029a53f"
GOLDEN_LIST_DELIVERY_ID = "dlv_54303aef7b5dfd7a"
GOLDEN_LIST_BODY = (
    '{"created_at_ms":1754000000000,'
    '"data":{"connection_id":"conn_abc123","empty":[],"xs":[1,2,{"a":1,"b":2}]},'
    '"delivery_id":"dlv_54303aef7b5dfd7a",'
    '"event_id":"evt_4e275242f029a53f",'
    '"type":"connection.created"}'
)
GOLDEN_LIST_SIGNATURE = (
    "v1=72c3b77e8708486e1f38ea8709328c8865f7df804855f88753413f6c40b8c611"
)

# The five things JSON cannot carry, each rejected by emit with
# ValidationError — never the bare TypeError json.dumps raises at signing
# time, by which point the delivery row is already persisted.
NON_JSON_PAYLOADS = [
    pytest.param({"amount": Decimal("1.5")}, id="decimal"),
    pytest.param({"tags": {"a", "b"}}, id="set"),
    pytest.param({"blob": b"raw"}, id="bytes"),
    pytest.param({1: "one"}, id="non-str-key"),
    pytest.param({"ratio": float("nan")}, id="nan"),
    pytest.param({"ratio": float("inf")}, id="inf"),
    pytest.param({"meta": {"amount": Decimal("1.5")}}, id="decimal-nested"),
    pytest.param({"xs": [1, {"tags": {"a"}}]}, id="set-nested-in-list"),
]

# Every URL the validator accepts is POSTed verbatim by httpx.
POSTABLE_URLS = (
    "https://hooks.example.test/auradefi",
    "http://hooks.example.test/auradefi",
    "https://hooks.example.test:8443/a/b?c=d",
    "http://127.0.0.1:9000/hook",
    "http://10.0.0.7/hook",
    "http://localhost:8000/",
    "http://[::1]:9000/hook",
)

# ...and every one it rejects is a host httpx would refuse or rewrite.
MALFORMED_URLS = ("https://[::1/x", "https:// /x", "https://a b.com/x", "http://%zz/x")

# httpx errors OUTSIDE the HTTPError family: `except httpx.HTTPError`
# does not catch these, so they escape tick unless it says so explicitly.
NON_HTTP_ERRORS = (httpx.InvalidURL, httpx.StreamError, httpx.CookieConflict)

# created_at_ms + RETRY_SCHEDULE_MS[k] — the six attempt times.
DUE_TIMES = (
    1_754_000_000_000,
    1_754_000_060_000,
    1_754_000_300_000,
    1_754_001_800_000,
    1_754_007_200_000,
    1_754_086_400_000,
)


def _store() -> WebhookStore:
    return WebhookStore(entropy=lambda n: "ab" * n)


def _httpx_error(error: type[Exception], request: httpx.Request) -> Exception:
    """Build ``error`` the way httpx itself constructs it.

    ``httpx.RequestError`` subclasses carry the request; ``InvalidURL``,
    ``StreamError`` and ``CookieConflict`` — none of which are
    ``httpx.HTTPError`` subclasses — take a message alone.
    """
    if issubclass(error, httpx.RequestError):
        return error("connection refused", request=request)
    return error("connection refused")


def _recording(status_code: int = 200, raises: type[Exception] | None = None):
    """A MockTransport handler plus the list of requests it sees."""
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        if raises is not None:
            raise _httpx_error(raises, request)
        return httpx.Response(status_code)

    return handler, recorded


def _deliverer(store: WebhookStore, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Deliverer(store, client)


def _registered(store: WebhookStore | None = None, url: str = URL, events=()):
    store = store or _store()
    endpoint, secret = store.register_endpoint(
        PROJECT, url, events, FrozenClock(T0)
    )
    return store, endpoint, secret


def _emitted(store: WebhookStore, now_ms: int = T0) -> tuple[Delivery, ...]:
    return store.emit(
        PROJECT, EventName.CONNECTION_CREATED, DATA, FrozenClock(now_ms)
    )


# ------------------------------------------------------- registration


def test_register_endpoint_returns_the_record_and_the_plaintext_secret():
    _, endpoint, secret = _registered()
    assert endpoint.id == GOLDEN_ENDPOINT_ID
    assert endpoint.project_id == PROJECT
    assert endpoint.url == URL
    assert endpoint.events == frozenset()
    assert endpoint.created_at_ms == T0
    assert secret == SECRET
    assert len(secret) == 64 and set(secret) <= set("0123456789abcdef")


def test_the_plaintext_secret_never_appears_in_a_projection():
    store, endpoint, secret = _registered()
    _emitted(store)
    assert secret not in repr(endpoint)
    assert secret not in repr(store.endpoints(PROJECT))
    assert secret not in repr(store.deliveries(PROJECT))
    assert "secret" not in {f.name for f in dataclasses.fields(endpoint)}
    assert store.endpoint_secret(PROJECT, endpoint.id) == secret


def test_subscription_list_is_parsed_into_event_names():
    _, endpoint, _ = _registered(events=("holdings.updated", EventName.SYNC_FAILED))
    assert endpoint.events == frozenset(
        {EventName.HOLDINGS_UPDATED, EventName.SYNC_FAILED}
    )


def test_duplicate_url_is_a_conflict_carrying_the_existing_id():
    store, endpoint, secret = _registered()
    with pytest.raises(ConflictError) as caught:
        store.register_endpoint(PROJECT, URL, (), FrozenClock(T0 + 1))
    assert caught.value.existing_id == GOLDEN_ENDPOINT_ID == endpoint.id
    # Nothing changed: one endpoint, original timestamp, original secret.
    assert store.endpoints(PROJECT) == (endpoint,)
    assert store.endpoint_secret(PROJECT, endpoint.id) == secret


def test_the_same_url_under_another_project_is_not_a_conflict():
    store, endpoint, _ = _registered()
    other, other_secret = store.register_endpoint(
        OTHER_PROJECT, URL, (), FrozenClock(T0)
    )
    assert other.id == "whe_7924264c62c07d23" != endpoint.id
    assert other_secret == SECRET  # deterministic test entropy, not shared state
    assert store.endpoints(PROJECT) == (endpoint,)
    assert store.endpoints(OTHER_PROJECT) == (other,)


def test_a_loopback_url_registers_because_there_is_no_ip_allowlist():
    # Rule #8's named casualty: Vezgo's source-IP allowlist does not exist
    # here, and no URL ever needs support to whitelist it.
    store, endpoint, _ = _registered(url="http://127.0.0.1:9000/hook")
    assert endpoint.id == "whe_00a48e997c473b18"
    assert store.endpoints(PROJECT) == (endpoint,)


def test_a_bad_url_is_rejected_and_creates_nothing():
    store = _store()
    with pytest.raises(ValidationError):
        store.register_endpoint(PROJECT, "ftp://hooks.example.test/a", (), FrozenClock(T0))
    assert store.endpoints(PROJECT) == ()


@pytest.mark.parametrize("url", MALFORMED_URLS)
def test_a_malformed_host_never_reaches_the_store(url):
    # These are the URLs that detonate later, not now: "https://[::1/x"
    # makes httpx raise InvalidURL — NOT an httpx.HTTPError — from inside
    # tick, and "https:// /x" is silently POSTed to "https://%20/x", a
    # different receiver than the one registered.
    store = _store()
    with pytest.raises(ValidationError):
        store.register_endpoint(PROJECT, url, (), FrozenClock(T0))
    assert store.endpoints(PROJECT) == ()
    assert store.deliveries(PROJECT) == ()


@pytest.mark.parametrize("url", POSTABLE_URLS)
def test_every_url_the_store_accepts_is_one_httpx_posts_verbatim(url):
    # The invariant that keeps InvalidURL unreachable through the public
    # API: validation admits nothing httpx would reject or rewrite.
    assert validate_endpoint_url(url) == url
    store, endpoint, _ = _registered(url=url)
    assert str(httpx.Request("POST", endpoint.url).url) == url

    _emitted(store)
    handler, recorded = _recording(200)
    _deliverer(store, handler).tick(T0)
    assert [str(request.url) for request in recorded] == [url]


def test_an_unknown_event_name_is_rejected_and_creates_nothing():
    store = _store()
    with pytest.raises(ValidationError):
        store.register_endpoint(PROJECT, URL, ("connection.updated",), FrozenClock(T0))
    assert store.endpoints(PROJECT) == ()


def test_endpoint_reads_are_tenant_scoped():
    store, endpoint, _ = _registered()
    assert store.endpoints(OTHER_PROJECT) == ()
    with pytest.raises(NotFoundError) as cross:
        store.get_endpoint(OTHER_PROJECT, endpoint.id)
    with pytest.raises(NotFoundError) as missing:
        store.get_endpoint(PROJECT, "whe_0000000000000000")
    # Cross-tenant is indistinguishable from genuinely absent.
    assert str(cross.value).replace(endpoint.id, "X") == str(missing.value).replace(
        "whe_0000000000000000", "X"
    )


# --------------------------------------------------------------- emit


def test_emit_creates_one_pending_delivery_per_subscribed_endpoint():
    store, endpoint, _ = _registered()
    (delivery,) = _emitted(store)
    assert delivery == Delivery(
        id=GOLDEN_DELIVERY_ID,
        project_id=PROJECT,
        endpoint_id=GOLDEN_ENDPOINT_ID,
        event_id=GOLDEN_EVENT_ID,
        replay_ordinal=0,
        status=DeliveryStatus.PENDING,
        attempts=0,
        created_at_ms=T0,
        next_attempt_at_ms=T0,
        delivered_at_ms=None,
        last_status_code=None,
        last_error=None,
    )
    assert store.deliveries(PROJECT) == (delivery,)


def test_an_empty_subscription_means_every_one_of_the_seven_events():
    store, _, _ = _registered(events=())
    for index, name in enumerate(EventName, start=1):
        store.emit(PROJECT, name, DATA, FrozenClock(T0 + index))
    assert len(store.deliveries(PROJECT)) == 7


def test_an_endpoint_subscribed_elsewhere_receives_nothing():
    store, _, _ = _registered(events=(EventName.HOLDINGS_UPDATED,))
    assert _emitted(store) == ()
    assert store.deliveries(PROJECT) == ()
    assert store.emit(
        PROJECT, EventName.HOLDINGS_UPDATED, DATA, FrozenClock(T0)
    ) != ()


def test_emit_with_no_endpoints_returns_nothing_but_records_the_event():
    store = _store()
    assert _emitted(store) == ()
    event = store.get_event(PROJECT, GOLDEN_EVENT_ID)
    assert event.name is EventName.CONNECTION_CREATED
    assert event.created_at_ms == T0
    assert dict(event.data) == DATA


def test_emit_is_idempotent_on_identical_event_inputs():
    store, _, _ = _registered()
    first = _emitted(store)
    second = _emitted(store)
    assert first == second
    assert len(store.deliveries(PROJECT)) == 1


def test_emit_is_tenant_scoped():
    store, _, _ = _registered()
    assert store.emit(
        OTHER_PROJECT, EventName.CONNECTION_CREATED, DATA, FrozenClock(T0)
    ) == ()
    assert store.deliveries(OTHER_PROJECT) == ()
    with pytest.raises(NotFoundError):
        store.get_event(OTHER_PROJECT, GOLDEN_EVENT_ID)


# ---------------------------------------------------- successful tick


def test_tick_posts_the_signed_body_exactly_once():
    store, endpoint, secret = _registered()
    _emitted(store)
    handler, recorded = _recording(200)
    _deliverer(store, handler).tick(T0)

    assert len(recorded) == 1
    request = recorded[0]
    assert request.method == "POST"
    assert str(request.url) == URL
    assert request.content == GOLDEN_BODY.encode("utf-8")
    assert request.headers["content-type"] == "application/json"
    assert request.headers["X-Auradefi-Event"] == "connection.created"
    assert request.headers["X-Auradefi-Delivery"] == GOLDEN_DELIVERY_ID
    assert request.headers["X-Auradefi-Timestamp"] == str(T0)
    assert request.headers["X-Auradefi-Signature"] == GOLDEN_SIGNATURE
    assert request.headers["X-Auradefi-Signature"] == sign(
        secret, T0, request.content.decode("utf-8")
    )
    assert (
        verify_signature(
            secret,
            int(request.headers["X-Auradefi-Timestamp"]),
            request.content.decode("utf-8"),
            request.headers["X-Auradefi-Signature"],
            T0,
        )
        is None
    )


def test_a_delivered_row_records_the_attempt_and_closes_the_chain():
    store, _, _ = _registered()
    _emitted(store)
    handler, _ = _recording(200)
    (updated,) = _deliverer(store, handler).tick(T0)

    assert updated.status is DeliveryStatus.DELIVERED
    assert updated.attempts == 1
    assert updated.delivered_at_ms == T0
    assert updated.next_attempt_at_ms is None
    assert updated.last_status_code == 200
    assert updated.last_error is None
    assert store.get_delivery(PROJECT, GOLDEN_DELIVERY_ID) == updated


def test_a_delivered_row_is_never_posted_again():
    store, _, _ = _registered()
    _emitted(store)
    handler, recorded = _recording(200)
    deliverer = _deliverer(store, handler)
    deliverer.tick(T0)
    assert deliverer.tick(T0 + 86_400_000) == ()
    assert len(recorded) == 1


@pytest.mark.parametrize("status_code", [200, 201, 202, 204, 299])
def test_any_2xx_is_a_success(status_code):
    store, _, _ = _registered()
    _emitted(store)
    handler, _ = _recording(status_code)
    (updated,) = _deliverer(store, handler).tick(T0)
    assert updated.status is DeliveryStatus.DELIVERED
    assert updated.last_status_code == status_code


@pytest.mark.parametrize("status_code", [301, 302, 304, 400, 404, 429, 500, 503])
def test_a_redirect_or_error_is_a_failure(status_code):
    store, _, _ = _registered()
    _emitted(store)
    handler, _ = _recording(status_code)
    (updated,) = _deliverer(store, handler).tick(T0)
    assert updated.status is DeliveryStatus.PENDING
    assert updated.attempts == 1
    assert updated.last_status_code == status_code
    assert updated.delivered_at_ms is None
    assert updated.next_attempt_at_ms == DUE_TIMES[1]


# ------------------------------------------------------ the 24h ladder


def test_backoff_golden_then_dead_letter_after_the_sixth_failure():
    store, _, _ = _registered()
    _emitted(store)
    handler, recorded = _recording(500)
    deliverer = _deliverer(store, handler)

    expected_next = [*DUE_TIMES[1:], None]
    for attempt, now_ms in enumerate(DUE_TIMES, start=1):
        (updated,) = deliverer.tick(now_ms)
        assert updated.attempts == attempt
        assert updated.next_attempt_at_ms == expected_next[attempt - 1]
        assert updated.last_status_code == 500
        assert updated.status is (
            DeliveryStatus.DEAD_LETTER
            if attempt == MAX_ATTEMPTS
            else DeliveryStatus.PENDING
        )

    assert len(recorded) == 6
    final = store.get_delivery(PROJECT, GOLDEN_DELIVERY_ID)
    assert final.status is DeliveryStatus.DEAD_LETTER
    assert final.attempts == 6
    assert final.next_attempt_at_ms is None
    assert final.delivered_at_ms is None
    # A seventh tick, a fortnight later: nothing is due, ever again.
    assert deliverer.tick(1_755_000_000_000) == ()
    assert len(recorded) == 6


def test_the_body_is_byte_identical_on_every_retry():
    store, _, _ = _registered()
    _emitted(store)
    handler, recorded = _recording(500)
    deliverer = _deliverer(store, handler)
    for now_ms in DUE_TIMES[:3]:
        deliverer.tick(now_ms)
    assert {request.content for request in recorded} == {GOLDEN_BODY.encode("utf-8")}
    # ...while each attempt carries a fresh timestamp and signature.
    assert [request.headers["X-Auradefi-Timestamp"] for request in recorded] == [
        str(value) for value in DUE_TIMES[:3]
    ]
    assert len({request.headers["X-Auradefi-Signature"] for request in recorded}) == 3


def test_the_body_survives_the_caller_mutating_the_payload_between_retries():
    """Byte-identity has to hold against a LIVE payload, not a flat one.

    ``emit`` takes a Mapping the caller keeps a reference to. A shallow
    ``dict(data)`` copies the top level only, so a nested mutation lands
    straight in the stored event and every later retry POSTs different
    bytes under the same delivery id — the receiver's de-dup and the
    signature both break. Same for anything ``get_event`` hands back.
    """
    store, _, _ = _registered()
    payload = {"connection_id": "conn_abc123", "meta": {"kind": "address"}}
    (created,) = store.emit(
        PROJECT, EventName.CONNECTION_CREATED, payload, FrozenClock(T0)
    )
    assert created.id == GOLDEN_NESTED_DELIVERY_ID
    handler, recorded = _recording(500)
    deliverer = _deliverer(store, handler)

    deliverer.tick(DUE_TIMES[0])
    payload["meta"]["kind"] = "tampered"  # the caller still holds their dict
    payload["injected"] = "boom"
    deliverer.tick(DUE_TIMES[1])
    # Refusing the write outright is the better answer; silently taking it
    # and still POSTing the original bytes is acceptable. Changing the
    # bytes is not.
    with contextlib.suppress(TypeError):
        store.get_event(PROJECT, created.event_id).data["injected"] = "boom"
    deliverer.tick(DUE_TIMES[2])

    assert [request.content for request in recorded] == [
        GOLDEN_NESTED_BODY.encode("utf-8")
    ] * 3


def test_the_stored_payload_refuses_a_write_outright():
    """No suppression: the mapping ``get_event`` hands back is READ-ONLY.

    The retry-byte-identity test above tolerates a store that silently
    accepts the write and still POSTs the original bytes. That tolerance
    is what let a plain deep-copied ``dict`` pass, so this pins the
    stronger contract the snapshot actually provides: every write through
    an :class:`Event`'s payload — top level, nested mapping, nested list,
    and deletion — raises ``TypeError``, and the payload is unchanged.
    """
    store, _, _ = _registered()
    payload = {"connection_id": "conn_abc123", "meta": {"kind": "address"}, "xs": [1, 2]}
    (created,) = store.emit(
        PROJECT, EventName.CONNECTION_CREATED, payload, FrozenClock(T0)
    )
    data = store.get_event(PROJECT, created.event_id).data

    with pytest.raises(TypeError):
        data["injected"] = "boom"
    with pytest.raises(TypeError):
        data["meta"]["kind"] = "tampered"
    with pytest.raises(TypeError):
        data["xs"][0] = 99
    with pytest.raises(TypeError):
        del data["connection_id"]

    assert dict(store.get_event(PROJECT, created.event_id).data) == {
        "connection_id": "conn_abc123",
        "meta": {"kind": "address"},
        "xs": (1, 2),
    }


@pytest.mark.parametrize("payload", NON_JSON_PAYLOADS)
def test_emit_rejects_a_payload_json_cannot_carry(payload):
    # ValidationError at the boundary the CALLER controls, not a bare
    # TypeError out of json.dumps at signing time — by then the row is
    # persisted and the failure surfaces inside the host's cron drain.
    store, _, _ = _registered()
    with pytest.raises(ValidationError):
        store.emit(PROJECT, EventName.CONNECTION_CREATED, payload, FrozenClock(T0))
    # Nothing was recorded: no half-written event, no delivery to drain.
    assert store.deliveries(PROJECT) == ()
    with pytest.raises(NotFoundError):
        store.get_event(PROJECT, GOLDEN_EVENT_ID)


def test_a_list_valued_payload_is_posted_byte_for_byte():
    # snapshot_payload stores JSON arrays as TUPLES, which json.dumps
    # cannot encode: the round-trip back to a list is what stands between
    # a working receiver and a TypeError inside tick.
    store, _, secret = _registered()
    (created,) = store.emit(
        PROJECT, EventName.CONNECTION_CREATED, dict(LIST_PAYLOAD), FrozenClock(T0)
    )
    assert created.event_id == GOLDEN_LIST_EVENT_ID
    assert created.id == GOLDEN_LIST_DELIVERY_ID

    handler, recorded = _recording(200)
    (updated,) = _deliverer(store, handler).tick(T0)

    assert len(recorded) == 1
    assert recorded[0].content == GOLDEN_LIST_BODY.encode("utf-8")
    assert recorded[0].headers["X-Auradefi-Signature"] == GOLDEN_LIST_SIGNATURE
    assert recorded[0].headers["X-Auradefi-Signature"] == sign(
        secret, T0, recorded[0].content.decode("utf-8")
    )
    assert updated.status is DeliveryStatus.DELIVERED


def test_emit_snapshots_the_payload_so_the_event_id_never_lies():
    # The id is a content address over canonical_json(data): if the
    # content can change after hashing, the id is a lie and two different
    # payloads share one evt_.
    store, _, _ = _registered()
    payload = {"connection_id": "conn_abc123", "meta": {"kind": "address"}}
    store.emit(PROJECT, EventName.CONNECTION_CREATED, payload, FrozenClock(T0))
    payload["meta"]["kind"] = "tampered"
    payload["injected"] = "boom"

    event = store.get_event(PROJECT, GOLDEN_NESTED_EVENT_ID)
    assert dict(event.data) == NESTED_DATA
    assert event.id == event_id(PROJECT, event.name, event.created_at_ms, event.data)


def test_nothing_is_attempted_one_millisecond_before_it_is_due():
    store, _, _ = _registered()
    (emitted,) = _emitted(store)
    handler, recorded = _recording(500)
    deliverer = _deliverer(store, handler)

    assert deliverer.tick(T0 - 1) == ()
    assert recorded == []
    assert store.get_delivery(PROJECT, GOLDEN_DELIVERY_ID) == emitted

    deliverer.tick(T0)
    assert deliverer.tick(DUE_TIMES[1] - 1) == ()
    assert len(recorded) == 1


def test_a_late_tick_still_makes_exactly_one_attempt():
    # The host's cron slipped an hour; that is one attempt, not five.
    store, _, _ = _registered()
    _emitted(store)
    handler, recorded = _recording(500)
    (updated,) = _deliverer(store, handler).tick(T0 + 3_600_000)
    assert len(recorded) == 1
    assert updated.attempts == 1
    assert updated.next_attempt_at_ms == DUE_TIMES[1]  # offset from creation


def test_deliveries_due_at_the_same_instant_go_in_delivery_id_order():
    store = _store()
    store.register_endpoint(PROJECT, URL_A, (), FrozenClock(T0))
    store.register_endpoint(PROJECT, URL_B, (), FrozenClock(T0))
    deliveries = _emitted(store)
    assert [delivery.id for delivery in deliveries] == [
        GOLDEN_DELIVERY_ID_A,
        GOLDEN_DELIVERY_ID_B,
    ]  # creation order follows registration order

    handler, recorded = _recording(200)
    _deliverer(store, handler).tick(T0)
    # ...but the drain order is ascending (next_attempt_at_ms, id), and
    # "dlv_43cb..." sorts before "dlv_601b...".
    assert [request.headers["X-Auradefi-Delivery"] for request in recorded] == [
        GOLDEN_DELIVERY_ID_B,
        GOLDEN_DELIVERY_ID_A,
    ]
    assert [str(request.url) for request in recorded] == [URL_B, URL_A]


def test_tick_drains_every_tenant():
    store = _store()
    store.register_endpoint(PROJECT, URL, (), FrozenClock(T0))
    store.register_endpoint(OTHER_PROJECT, URL, (), FrozenClock(T0))
    _emitted(store)
    store.emit(OTHER_PROJECT, EventName.CONNECTION_CREATED, DATA, FrozenClock(T0))

    handler, recorded = _recording(200)
    assert len(_deliverer(store, handler).tick(T0)) == 2
    assert len(recorded) == 2
    assert all(
        store.deliveries(project)[0].status is DeliveryStatus.DELIVERED
        for project in (PROJECT, OTHER_PROJECT)
    )


# -------------------------------------------------- transport failures


def test_a_transport_error_is_recorded_not_raised():
    store, _, _ = _registered()
    _emitted(store)
    handler, recorded = _recording(raises=httpx.ConnectError)
    (updated,) = _deliverer(store, handler).tick(T0)  # must not raise

    assert len(recorded) == 1
    assert updated.status is DeliveryStatus.PENDING
    assert updated.attempts == 1
    assert updated.last_status_code is None
    assert updated.last_error
    assert updated.next_attempt_at_ms == DUE_TIMES[1]  # same ladder as a 500


def test_the_escape_table_names_errors_outside_the_httperror_family():
    # Guards the table below from quietly degrading into "three more
    # HTTPError subclasses": `except httpx.HTTPError` catches NONE of
    # these, which is exactly why they belong in the parametrize list.
    assert not any(issubclass(error, httpx.HTTPError) for error in NON_HTTP_ERRORS)
    assert all(issubclass(error, Exception) for error in NON_HTTP_ERRORS)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.ConnectTimeout,
        *NON_HTTP_ERRORS,
    ],
)
def test_no_httpx_error_escapes_tick(error):
    store, _, _ = _registered()
    _emitted(store)
    handler, recorded = _recording(raises=error)
    (updated,) = _deliverer(store, handler).tick(T0)  # must not raise
    assert len(recorded) == 1
    assert updated.attempts == 1
    assert updated.status is DeliveryStatus.PENDING
    assert updated.last_status_code is None
    assert updated.last_error  # the reason survives on the row
    assert updated.next_attempt_at_ms == DUE_TIMES[1]  # same ladder as a 500
    assert store.get_delivery(PROJECT, GOLDEN_DELIVERY_ID) == updated


@pytest.mark.parametrize("error", NON_HTTP_ERRORS)
def test_an_error_outside_the_httperror_family_does_not_strand_the_drain(error):
    # The cost of letting one escape is not one lost delivery: tick dies
    # mid-drain and every OTHER receiver due at this instant is silently
    # skipped, with nothing recorded to say it happened.
    store = _store()
    store.register_endpoint(PROJECT, URL_A, (), FrozenClock(T0))
    store.register_endpoint(PROJECT, URL_B, (), FrozenClock(T0))
    _emitted(store)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == URL_B:
            raise _httpx_error(error, request)
        return httpx.Response(200)

    updated = _deliverer(store, handler).tick(T0)  # must not raise
    assert seen == [URL_B, URL_A]  # B's delivery id sorts first
    assert len(updated) == 2
    statuses = {delivery.id: delivery.status for delivery in updated}
    assert statuses[GOLDEN_DELIVERY_ID_A] is DeliveryStatus.DELIVERED
    assert statuses[GOLDEN_DELIVERY_ID_B] is DeliveryStatus.PENDING
    assert store.get_delivery(PROJECT, GOLDEN_DELIVERY_ID_B).last_error


def test_one_dead_receiver_does_not_stop_the_drain():
    store = _store()
    store.register_endpoint(PROJECT, URL_A, (), FrozenClock(T0))
    store.register_endpoint(PROJECT, URL_B, (), FrozenClock(T0))
    _emitted(store)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == URL_B:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200)

    updated = _deliverer(store, handler).tick(T0)
    assert seen == [URL_B, URL_A]
    assert len(updated) == 2
    statuses = {delivery.id: delivery.status for delivery in updated}
    assert statuses[GOLDEN_DELIVERY_ID_A] is DeliveryStatus.DELIVERED
    assert statuses[GOLDEN_DELIVERY_ID_B] is DeliveryStatus.PENDING


# --------------------------------------------- record_attempt directly


def test_record_attempt_success_closes_the_chain():
    store, _, _ = _registered()
    _emitted(store)
    updated = store.record_attempt(
        PROJECT, GOLDEN_DELIVERY_ID, now_ms=T0 + 5, status_code=204, error=None
    )
    assert updated.status is DeliveryStatus.DELIVERED
    assert updated.delivered_at_ms == T0 + 5
    assert updated.next_attempt_at_ms is None


def test_record_attempt_failure_advances_the_pinned_ladder():
    store, _, _ = _registered()
    _emitted(store)
    for attempt, expected in enumerate(DUE_TIMES[1:], start=1):
        updated = store.record_attempt(
            PROJECT,
            GOLDEN_DELIVERY_ID,
            now_ms=T0 + attempt,
            status_code=500,
            error="boom",
        )
        assert updated.attempts == attempt
        assert updated.next_attempt_at_ms == expected
        assert updated.last_error == "boom"


def test_record_attempt_is_tenant_scoped():
    store, _, _ = _registered()
    _emitted(store)
    with pytest.raises(NotFoundError):
        store.record_attempt(
            OTHER_PROJECT, GOLDEN_DELIVERY_ID, now_ms=T0, status_code=200, error=None
        )
    assert store.get_delivery(PROJECT, GOLDEN_DELIVERY_ID).attempts == 0


# ----------------------------------------------------- the dead letter


def test_dead_letter_lists_only_exhausted_rows_of_this_project():
    store = _store()
    store.register_endpoint(PROJECT, URL_A, (), FrozenClock(T0))
    store.register_endpoint(PROJECT, URL_B, (), FrozenClock(T0))
    store.register_endpoint(OTHER_PROJECT, URL, (), FrozenClock(T0))
    _emitted(store)
    store.emit(OTHER_PROJECT, EventName.CONNECTION_CREATED, DATA, FrozenClock(T0))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500 if str(request.url) == URL_B else 200)

    deliverer = _deliverer(store, handler)
    for now_ms in DUE_TIMES:
        deliverer.tick(now_ms)

    assert [delivery.id for delivery in store.dead_letter(PROJECT)] == [
        GOLDEN_DELIVERY_ID_B
    ]
    assert store.dead_letter(OTHER_PROJECT) == ()
    assert store.dead_letter("proj_nope") == ()


def test_delivery_reads_are_tenant_scoped():
    store, _, _ = _registered()
    _emitted(store)
    assert store.deliveries(OTHER_PROJECT) == ()
    with pytest.raises(NotFoundError) as cross:
        store.get_delivery(OTHER_PROJECT, GOLDEN_DELIVERY_ID)
    with pytest.raises(NotFoundError) as missing:
        store.get_delivery(PROJECT, "dlv_0000000000000000")
    assert str(cross.value).replace(GOLDEN_DELIVERY_ID, "X") == str(
        missing.value
    ).replace("dlv_0000000000000000", "X")


def test_two_stores_never_share_state():
    first, _, _ = _registered()
    second = _store()
    assert second.endpoints(PROJECT) == ()
    assert first.endpoints(PROJECT) != ()


# ------------------------------------------------------ import hygiene


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_deliver_is_the_only_module_importing_httpx():
    importers = {
        path.name
        for path in sorted(WEBHOOKS_SRC.glob("*.py"))
        if "httpx" in _imports_of(path)
    }
    assert importers == {"deliver.py"}


def test_webhooks_imports_no_web_framework_and_no_orm():
    banned = {"fastapi", "starlette", "flask", "django", "sqlalchemy", "sqlmodel"}
    offenders = {
        path.name: sorted(_imports_of(path) & banned)
        for path in sorted(WEBHOOKS_SRC.glob("*.py"))
        if _imports_of(path) & banned
    }
    assert offenders == {}


def test_webhooks_stays_inside_its_declared_layer():
    allowed = {"money", "tenancy", "ledger", "webhooks"}
    for path in sorted(WEBHOOKS_SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = getattr(node, "module", None)
            if isinstance(node, ast.ImportFrom) and module and module.startswith(
                "auradefi."
            ):
                parts = module.split(".")
                if len(parts) > 2 or (WEBHOOKS_SRC.parent / parts[1]).is_dir():
                    assert parts[1] in allowed, f"{path.name} imports {module}"
