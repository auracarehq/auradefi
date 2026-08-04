"""Replay — the primitive Zerion and Vezgo both lack (SPEC §7.3).

Zerion drops an event permanently after 3 retries in ~60 seconds. Here a
dead-lettered delivery is a durable row an operator re-arms once the
receiver is fixed, and the replay is a NEW delivery of the SAME event:
new ``dlv_`` id at the next ordinal, attempts back to 0, the full
six-attempt schedule restarted, the original row untouched.

Golden literals derived INDEPENDENTLY via ``python3 -c`` from the pinned
algorithms (see tests/webhooks/test_models.py). The replayed body is the
original with ONE substring changed — the delivery id — because the body
carries the EVENT's created_at_ms and no attempt counter.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest

from auradefi.clock import FrozenClock
from auradefi.errors import NotFoundError
from auradefi.webhooks.deliver import Deliverer, WebhookStore
from auradefi.webhooks.models import DeliveryStatus, EventName
from auradefi.webhooks.replay import replay, replay_dead_letter
from auradefi.webhooks.sign import verify_signature

PROJECT = "proj_0000000000000001"
OTHER_PROJECT = "proj_0000000000000002"
URL = "https://hooks.example.test/auradefi"
URL_B = "https://b.example.test/hook"
T0 = 1_754_000_000_000
REPLAY_T = 1_754_090_000_000
DATA = {"connection_id": "conn_abc123", "kind": "address"}
SECRET = "ab" * 32

GOLDEN_DELIVERY_ID = "dlv_cb33eb38d1b7aa44"
GOLDEN_REPLAY_1_ID = "dlv_1e9b96248ef253e7"
GOLDEN_REPLAY_2_ID = "dlv_5afae5592d6ba849"
GOLDEN_DELIVERY_ID_B = "dlv_43cbc1a1314bd1bf"
GOLDEN_BODY = (
    '{"created_at_ms":1754000000000,'
    '"data":{"connection_id":"conn_abc123","kind":"address"},'
    '"delivery_id":"dlv_cb33eb38d1b7aa44",'
    '"event_id":"evt_490b3195618c4099",'
    '"type":"connection.created"}'
)
GOLDEN_REPLAY_BODY = GOLDEN_BODY.replace(GOLDEN_DELIVERY_ID, GOLDEN_REPLAY_1_ID)
# sign("ab"*32, 1754090000000, GOLDEN_REPLAY_BODY) — derived independently.
GOLDEN_REPLAY_SIGNATURE = (
    "v1=e4940cdc864058638c1edcd82f83e1a8a79df67a2db9f51109af9782384017de"
)

DUE_TIMES = (
    1_754_000_000_000,
    1_754_000_060_000,
    1_754_000_300_000,
    1_754_001_800_000,
    1_754_007_200_000,
    1_754_086_400_000,
)


def _recording(status_code: int = 200):
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(status_code)

    return handler, recorded


def _deliverer(store: WebhookStore, handler) -> Deliverer:
    return Deliverer(store, httpx.Client(transport=httpx.MockTransport(handler)))


def _store_with(url: str = URL) -> WebhookStore:
    store = WebhookStore(entropy=lambda n: "ab" * n)
    store.register_endpoint(PROJECT, url, (), FrozenClock(T0))
    store.emit(PROJECT, EventName.CONNECTION_CREATED, DATA, FrozenClock(T0))
    return store


def _dead_lettered(url: str = URL) -> WebhookStore:
    """A store whose single delivery burned all six attempts on 500s."""
    store = _store_with(url)
    handler, _ = _recording(500)
    deliverer = _deliverer(store, handler)
    for now_ms in DUE_TIMES:
        deliverer.tick(now_ms)
    return store


# ------------------------------------------------------- single replay


def test_replay_creates_a_fresh_pending_row_at_the_next_ordinal():
    store = _dead_lettered()
    replayed = replay(store, PROJECT, GOLDEN_DELIVERY_ID, FrozenClock(REPLAY_T))

    assert replayed.id == GOLDEN_REPLAY_1_ID
    assert replayed.replay_ordinal == 1
    assert replayed.status is DeliveryStatus.PENDING
    assert replayed.attempts == 0
    assert replayed.created_at_ms == REPLAY_T
    assert replayed.next_attempt_at_ms == REPLAY_T  # the schedule restarts
    assert replayed.delivered_at_ms is None
    assert replayed.last_status_code is None
    assert replayed.last_error is None


def test_replay_reuses_the_same_endpoint_and_event():
    store = _dead_lettered()
    original = store.get_delivery(PROJECT, GOLDEN_DELIVERY_ID)
    replayed = replay(store, PROJECT, GOLDEN_DELIVERY_ID, FrozenClock(REPLAY_T))
    assert replayed.project_id == original.project_id
    assert replayed.endpoint_id == original.endpoint_id
    assert replayed.event_id == original.event_id
    assert replayed.id != original.id


def test_replay_leaves_the_dead_lettered_original_untouched():
    store = _dead_lettered()
    before = store.get_delivery(PROJECT, GOLDEN_DELIVERY_ID)
    replay(store, PROJECT, GOLDEN_DELIVERY_ID, FrozenClock(REPLAY_T))
    after = store.get_delivery(PROJECT, GOLDEN_DELIVERY_ID)

    assert after == before
    assert after.status is DeliveryStatus.DEAD_LETTER
    assert after.attempts == 6
    assert after.next_attempt_at_ms is None
    assert [d.id for d in store.deliveries(PROJECT)] == [
        GOLDEN_DELIVERY_ID,
        GOLDEN_REPLAY_1_ID,
    ]
    # The dead-letter view keeps its forensic row.
    assert [d.id for d in store.dead_letter(PROJECT)] == [GOLDEN_DELIVERY_ID]


def test_a_second_replay_takes_the_next_ordinal():
    store = _dead_lettered()
    first = replay(store, PROJECT, GOLDEN_DELIVERY_ID, FrozenClock(REPLAY_T))
    second = replay(store, PROJECT, GOLDEN_DELIVERY_ID, FrozenClock(REPLAY_T + 1))
    assert first.id == GOLDEN_REPLAY_1_ID and first.replay_ordinal == 1
    assert second.id == GOLDEN_REPLAY_2_ID and second.replay_ordinal == 2
    assert len(store.deliveries(PROJECT)) == 3


def test_replaying_a_replay_also_takes_the_next_ordinal():
    store = _dead_lettered()
    replay(store, PROJECT, GOLDEN_DELIVERY_ID, FrozenClock(REPLAY_T))
    third = replay(store, PROJECT, GOLDEN_REPLAY_1_ID, FrozenClock(REPLAY_T + 2))
    assert third.id == GOLDEN_REPLAY_2_ID
    assert third.replay_ordinal == 2


def test_a_delivered_webhook_may_be_replayed_too():
    store = _store_with()
    handler, _ = _recording(200)
    _deliverer(store, handler).tick(T0)
    assert store.get_delivery(PROJECT, GOLDEN_DELIVERY_ID).status is (
        DeliveryStatus.DELIVERED
    )
    replayed = replay(store, PROJECT, GOLDEN_DELIVERY_ID, FrozenClock(REPLAY_T))
    assert replayed.id == GOLDEN_REPLAY_1_ID
    assert replayed.status is DeliveryStatus.PENDING


def test_replay_is_tenant_scoped_and_indistinguishable_from_missing():
    store = _dead_lettered()
    with pytest.raises(NotFoundError) as cross:
        replay(store, OTHER_PROJECT, GOLDEN_DELIVERY_ID, FrozenClock(REPLAY_T))
    with pytest.raises(NotFoundError) as missing:
        replay(store, PROJECT, "dlv_0000000000000000", FrozenClock(REPLAY_T))
    assert str(cross.value).replace(GOLDEN_DELIVERY_ID, "X") == str(
        missing.value
    ).replace("dlv_0000000000000000", "X")
    assert len(store.deliveries(PROJECT)) == 1


# ----------------------------------------------- the replay round trip


def test_the_next_tick_delivers_the_replay_through_the_same_signing_path():
    store = _dead_lettered()
    replayed = replay(store, PROJECT, GOLDEN_DELIVERY_ID, FrozenClock(REPLAY_T))
    handler, recorded = _recording(200)
    (delivered,) = _deliverer(store, handler).tick(REPLAY_T)

    assert len(recorded) == 1
    request = recorded[0]
    assert str(request.url) == URL
    assert request.content == GOLDEN_REPLAY_BODY.encode("utf-8")
    assert request.headers["X-Auradefi-Delivery"] == GOLDEN_REPLAY_1_ID
    assert request.headers["X-Auradefi-Event"] == "connection.created"
    assert request.headers["X-Auradefi-Timestamp"] == str(REPLAY_T)
    assert request.headers["X-Auradefi-Signature"] == GOLDEN_REPLAY_SIGNATURE
    assert (
        verify_signature(
            SECRET,
            REPLAY_T,
            request.content.decode("utf-8"),
            request.headers["X-Auradefi-Signature"],
            REPLAY_T,
        )
        is None
    )
    assert delivered.id == replayed.id
    assert delivered.status is DeliveryStatus.DELIVERED
    assert delivered.attempts == 1


def test_the_replay_body_differs_from_the_original_only_in_the_delivery_id():
    store = _dead_lettered()
    replay(store, PROJECT, GOLDEN_DELIVERY_ID, FrozenClock(REPLAY_T))
    handler, recorded = _recording(200)
    _deliverer(store, handler).tick(REPLAY_T)

    sent = recorded[0].content.decode("utf-8")
    assert sent.replace(GOLDEN_REPLAY_1_ID, GOLDEN_DELIVERY_ID) == GOLDEN_BODY
    # ...including the event's original timestamp, hours in the past.
    assert '"created_at_ms":1754000000000' in sent


def test_a_replay_walks_the_full_six_attempt_ladder_again():
    store = _dead_lettered()
    replay(store, PROJECT, GOLDEN_DELIVERY_ID, FrozenClock(REPLAY_T))
    handler, recorded = _recording(500)
    deliverer = _deliverer(store, handler)
    for offset in (0, 60_000, 300_000, 1_800_000, 7_200_000, 86_400_000):
        deliverer.tick(REPLAY_T + offset)

    assert len(recorded) == 6
    replayed = store.get_delivery(PROJECT, GOLDEN_REPLAY_1_ID)
    assert replayed.status is DeliveryStatus.DEAD_LETTER
    assert replayed.attempts == 6
    assert replayed.next_attempt_at_ms is None
    assert {d.id for d in store.dead_letter(PROJECT)} == {
        GOLDEN_DELIVERY_ID,
        GOLDEN_REPLAY_1_ID,
    }


# ------------------------------------------------- bulk dead-letter re-arm


def test_replay_dead_letter_re_arms_every_exhausted_row():
    store = WebhookStore(entropy=lambda n: "ab" * n)
    store.register_endpoint(PROJECT, URL, (), FrozenClock(T0))
    store.register_endpoint(PROJECT, URL_B, (), FrozenClock(T0))
    store.emit(PROJECT, EventName.CONNECTION_CREATED, DATA, FrozenClock(T0))
    handler, _ = _recording(500)
    deliverer = _deliverer(store, handler)
    for now_ms in DUE_TIMES:
        deliverer.tick(now_ms)
    originals = {d.id: dataclasses.replace(d) for d in store.dead_letter(PROJECT)}
    assert set(originals) == {GOLDEN_DELIVERY_ID, GOLDEN_DELIVERY_ID_B}

    rearmed = replay_dead_letter(store, PROJECT, FrozenClock(REPLAY_T))

    assert len(rearmed) == 2
    assert all(delivery.status is DeliveryStatus.PENDING for delivery in rearmed)
    assert all(delivery.attempts == 0 for delivery in rearmed)
    assert all(delivery.replay_ordinal == 1 for delivery in rearmed)
    assert all(delivery.created_at_ms == REPLAY_T for delivery in rearmed)
    assert all(delivery.next_attempt_at_ms == REPLAY_T for delivery in rearmed)
    assert [delivery.id for delivery in rearmed][0] == GOLDEN_REPLAY_1_ID
    # Originals survive, untouched.
    for delivery_id, before in originals.items():
        assert store.get_delivery(PROJECT, delivery_id) == before


def test_replay_dead_letter_returns_the_new_rows_in_creation_order():
    store = _dead_lettered()
    (rearmed,) = replay_dead_letter(store, PROJECT, FrozenClock(REPLAY_T))
    assert rearmed.id == GOLDEN_REPLAY_1_ID
    again = replay_dead_letter(store, PROJECT, FrozenClock(REPLAY_T + 1))
    # The originals are still dead-lettered, so a second call re-arms them
    # again at the next ordinal — draining is the deliverer's job.
    assert [delivery.id for delivery in again] == [GOLDEN_REPLAY_2_ID]


def test_replay_dead_letter_is_empty_when_nothing_died():
    store = _store_with()
    handler, _ = _recording(200)
    _deliverer(store, handler).tick(T0)
    assert replay_dead_letter(store, PROJECT, FrozenClock(REPLAY_T)) == ()
    assert len(store.deliveries(PROJECT)) == 1


def test_replay_dead_letter_never_crosses_a_tenant_boundary():
    store = _dead_lettered()
    assert replay_dead_letter(store, OTHER_PROJECT, FrozenClock(REPLAY_T)) == ()
    assert replay_dead_letter(store, "proj_nope", FrozenClock(REPLAY_T)) == ()
    assert len(store.deliveries(PROJECT)) == 1
