"""Webhook value objects, deterministic ids, pinned wire format (SPEC §7.3).

Every golden literal below was derived INDEPENDENTLY of the code under
test, via ``python3 -c`` implementing the algorithms pinned in
docs/internal/DECISIONS.md ("Webhook ids", "Webhook retry schedule"):

    canonical_json = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    endpoint_id    = "whe_" + sha256(f"{project_id}|{url}")[:16]
    event_id       = "evt_" + sha256(f"{project_id}|{name}|{created_at_ms}|{canonical_json(data)}")[:16]
    delivery_id    = "dlv_" + sha256(f"{endpoint_id}|{event_id}|{replay_ordinal}")[:16]
    body           = canonical_json({created_at_ms, data, delivery_id, event_id, type})

A stability contract is a hardcoded literal, not a call to the function
under test: these bytes are what a receiver's HMAC check hashes and what
its de-duplication keys on, so drift is a broken integration, not a
refactor.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from decimal import Decimal

import pytest

from auradefi.errors import ValidationError
from auradefi.webhooks.models import (
    BODY_KEYS,
    MAX_ATTEMPTS,
    RETRY_SCHEDULE_MS,
    Delivery,
    DeliveryStatus,
    Endpoint,
    Event,
    EventName,
    canonical_json,
    delivery_body,
    delivery_id,
    due_at_ms,
    endpoint_id,
    event_id,
    parse_event_name,
    snapshot_payload,
)

PROJECT = "proj_0000000000000001"
OTHER_PROJECT = "proj_0000000000000002"
URL = "https://hooks.example.test/auradefi"
T0 = 1_754_000_000_000
DATA = {"connection_id": "conn_abc123", "kind": "address"}

GOLDEN_ENDPOINT_ID = "whe_a81d5036ec375faa"
GOLDEN_EVENT_ID = "evt_490b3195618c4099"
GOLDEN_DELIVERY_ID = "dlv_cb33eb38d1b7aa44"
GOLDEN_REPLAY_1_ID = "dlv_1e9b96248ef253e7"
GOLDEN_REPLAY_2_ID = "dlv_5afae5592d6ba849"
GOLDEN_CANONICAL_DATA = '{"connection_id":"conn_abc123","kind":"address"}'
GOLDEN_BODY = (
    '{"created_at_ms":1754000000000,'
    '"data":{"connection_id":"conn_abc123","kind":"address"},'
    '"delivery_id":"dlv_cb33eb38d1b7aa44",'
    '"event_id":"evt_490b3195618c4099",'
    '"type":"connection.created"}'
)
GOLDEN_REPLAY_BODY = (
    '{"created_at_ms":1754000000000,'
    '"data":{"connection_id":"conn_abc123","kind":"address"},'
    '"delivery_id":"dlv_1e9b96248ef253e7",'
    '"event_id":"evt_490b3195618c4099",'
    '"type":"connection.created"}'
)


def _event(
    event_id_: str = GOLDEN_EVENT_ID,
    created_at_ms: int = T0,
    name: EventName = EventName.CONNECTION_CREATED,
    data: dict | None = None,
) -> Event:
    return Event(
        id=event_id_,
        project_id=PROJECT,
        name=name,
        data=DATA if data is None else data,
        created_at_ms=created_at_ms,
    )


def _delivery(
    delivery_id_: str = GOLDEN_DELIVERY_ID,
    replay_ordinal: int = 0,
    created_at_ms: int = T0,
) -> Delivery:
    return Delivery(
        id=delivery_id_,
        project_id=PROJECT,
        endpoint_id=GOLDEN_ENDPOINT_ID,
        event_id=GOLDEN_EVENT_ID,
        replay_ordinal=replay_ordinal,
        status=DeliveryStatus.PENDING,
        attempts=0,
        created_at_ms=created_at_ms,
        next_attempt_at_ms=created_at_ms,
    )


# --------------------------------------------------------------- enums


def test_event_name_has_exactly_the_seven_spec_events():
    assert len(EventName) == 7
    assert sorted(member.value for member in EventName) == [
        "connection.created",
        "connection.deleted",
        "holdings.updated",
        "reorg.detected",
        "sync.failed",
        "sync.started",
        "transactions.available",
    ]


def test_event_names_are_plain_strings_on_the_wire():
    assert EventName.CONNECTION_CREATED == "connection.created"
    assert f"{EventName.REORG_DETECTED}" == "reorg.detected"
    assert all(isinstance(member, str) for member in EventName)


def test_delivery_status_has_exactly_three_states():
    assert [member.value for member in DeliveryStatus] == [
        "pending",
        "delivered",
        "dead_letter",
    ]


# ---------------------------------------------------------- the schedule


def test_retry_schedule_is_pinned_to_the_byte():
    assert RETRY_SCHEDULE_MS == (0, 60_000, 300_000, 1_800_000, 7_200_000, 86_400_000)
    assert isinstance(RETRY_SCHEDULE_MS, tuple)


def test_max_attempts_is_one_per_schedule_slot():
    assert MAX_ATTEMPTS == len(RETRY_SCHEDULE_MS) == 6


def test_last_attempt_lands_at_exactly_twenty_four_hours():
    assert RETRY_SCHEDULE_MS[-1] == 24 * 60 * 60 * 1000
    assert RETRY_SCHEDULE_MS[0] == 0  # attempt 0 is immediate


def test_schedule_is_strictly_increasing():
    assert list(RETRY_SCHEDULE_MS) == sorted(set(RETRY_SCHEDULE_MS))


def test_due_at_ms_offsets_from_creation_not_from_now():
    assert [due_at_ms(T0, k) for k in range(7)] == [
        1_754_000_000_000,
        1_754_000_060_000,
        1_754_000_300_000,
        1_754_001_800_000,
        1_754_007_200_000,
        1_754_086_400_000,
        None,
    ]


def test_due_at_ms_is_none_past_the_last_slot():
    assert due_at_ms(T0, MAX_ATTEMPTS) is None
    assert due_at_ms(T0, 99) is None


def test_due_at_ms_rejects_a_negative_attempt():
    with pytest.raises(ValidationError):
        due_at_ms(T0, -1)


# -------------------------------------------------------- canonical_json


def test_canonical_json_is_compact_and_key_sorted():
    assert canonical_json(DATA) == GOLDEN_CANONICAL_DATA
    assert canonical_json({"kind": "address", "connection_id": "conn_abc123"}) == (
        GOLDEN_CANONICAL_DATA
    )


def test_canonical_json_escapes_non_ascii_and_has_no_spaces():
    assert canonical_json({"k": "é", "a": 1}) == '{"a":1,"k":"\\u00e9"}'
    assert canonical_json({}) == "{}"
    assert canonical_json({"n": [1, 2, {"b": 2, "a": 1}]}) == '{"n":[1,2,{"a":1,"b":2}]}'


def test_canonical_json_keeps_integers_exact():
    huge = 10**77
    assert canonical_json({"v": huge}) == '{"v":%d}' % huge


# --------------------------------------------------------------- the ids


def test_endpoint_id_golden():
    assert endpoint_id(PROJECT, URL) == GOLDEN_ENDPOINT_ID


def test_endpoint_id_is_project_scoped():
    assert endpoint_id(OTHER_PROJECT, URL) == "whe_7924264c62c07d23"
    assert endpoint_id(OTHER_PROJECT, URL) != endpoint_id(PROJECT, URL)


def test_endpoint_id_does_not_normalise_the_url():
    # A trailing slash is a different receiver — ids hash the exact bytes.
    assert endpoint_id(PROJECT, URL + "/") == "whe_41ccbde4da4f9837"


def test_event_id_golden():
    assert event_id(PROJECT, EventName.CONNECTION_CREATED, T0, DATA) == GOLDEN_EVENT_ID


def test_event_id_uses_the_wire_name_not_the_member_name():
    assert event_id(PROJECT, EventName.CONNECTION_CREATED, T0, DATA) == event_id(
        PROJECT, "connection.created", T0, DATA
    )


def test_event_id_varies_with_project_name_time_and_data():
    base = event_id(PROJECT, EventName.CONNECTION_CREATED, T0, DATA)
    assert event_id(OTHER_PROJECT, EventName.CONNECTION_CREATED, T0, DATA) == (
        "evt_d0c5dcf524603b60"
    )
    assert event_id(PROJECT, EventName.HOLDINGS_UPDATED, T0, DATA) == (
        "evt_13bb5fd0445dce15"
    )
    assert event_id(PROJECT, EventName.CONNECTION_CREATED, T0 + 1, DATA) != base
    assert event_id(PROJECT, EventName.CONNECTION_CREATED, T0, {}) != base


def test_event_id_ignores_data_key_order():
    assert event_id(
        PROJECT,
        EventName.CONNECTION_CREATED,
        T0,
        {"kind": "address", "connection_id": "conn_abc123"},
    ) == GOLDEN_EVENT_ID


def test_delivery_id_golden_original_and_replays():
    assert delivery_id(GOLDEN_ENDPOINT_ID, GOLDEN_EVENT_ID, 0) == GOLDEN_DELIVERY_ID
    assert delivery_id(GOLDEN_ENDPOINT_ID, GOLDEN_EVENT_ID, 1) == GOLDEN_REPLAY_1_ID
    assert delivery_id(GOLDEN_ENDPOINT_ID, GOLDEN_EVENT_ID, 2) == GOLDEN_REPLAY_2_ID


def test_ids_carry_their_prefix_and_sixteen_hex_chars():
    for value, prefix in (
        (endpoint_id(PROJECT, URL), "whe_"),
        (event_id(PROJECT, EventName.SYNC_FAILED, T0, DATA), "evt_"),
        (delivery_id(GOLDEN_ENDPOINT_ID, GOLDEN_EVENT_ID, 0), "dlv_"),
    ):
        assert value.startswith(prefix)
        body = value.removeprefix(prefix)
        assert len(body) == 16
        assert set(body) <= set("0123456789abcdef")


# ------------------------------------------------------------- the body


def test_delivery_body_golden_bytes():
    assert delivery_body(_event(), _delivery()) == GOLDEN_BODY


def test_delivery_body_has_exactly_five_keys_and_no_attempt_counter():
    import json

    decoded = json.loads(delivery_body(_event(), _delivery()))
    assert sorted(decoded) == list(BODY_KEYS)
    assert sorted(decoded) == [
        "created_at_ms",
        "data",
        "delivery_id",
        "event_id",
        "type",
    ]
    assert "attempt" not in delivery_body(_event(), _delivery())


def test_delivery_body_is_byte_identical_across_retries():
    # No attempt counter, no now_ms: a retry re-sends the same bytes.
    first = delivery_body(_event(), _delivery())
    second = delivery_body(_event(), _delivery())
    assert first == second == GOLDEN_BODY


def test_replay_body_differs_from_the_original_only_in_delivery_id():
    replayed = _delivery(GOLDEN_REPLAY_1_ID, replay_ordinal=1, created_at_ms=T0 + 999)
    body = delivery_body(_event(), replayed)
    assert body == GOLDEN_REPLAY_BODY
    assert body.replace(GOLDEN_REPLAY_1_ID, GOLDEN_DELIVERY_ID) == GOLDEN_BODY


def test_delivery_body_timestamps_the_event_not_the_delivery():
    late = _delivery(created_at_ms=T0 + 86_400_000)
    assert '"created_at_ms":1754000000000' in delivery_body(_event(), late)


def test_delivery_body_carries_the_event_wire_name():
    event = _event(name=EventName.REORG_DETECTED)
    assert '"type":"reorg.detected"' in delivery_body(event, _delivery())


def test_delivery_body_sorts_nested_payload_keys():
    event = _event(data={"z": 1, "a": {"y": 2, "b": 3}})
    body = delivery_body(event, _delivery())
    assert '"data":{"a":{"b":3,"y":2},"z":1}' in body


# -------------------------------------------------------------- the URL


def test_parse_event_name_round_trips_every_member():
    for member in EventName:
        assert parse_event_name(member.value) is member


@pytest.mark.parametrize("value", ["", "connection.updated", "CONNECTION_CREATED", "*"])
def test_parse_event_name_rejects_unknown_names(value):
    with pytest.raises(ValidationError):
        parse_event_name(value)


# --------------------------------------------------------- immutability


def test_endpoint_is_frozen_and_holds_no_secret():
    endpoint = Endpoint(
        id=GOLDEN_ENDPOINT_ID,
        project_id=PROJECT,
        url=URL,
        events=frozenset(),
        created_at_ms=T0,
    )
    field_names = {field.name for field in dataclasses.fields(endpoint)}
    assert field_names == {"id", "project_id", "url", "events", "created_at_ms"}
    assert "secret" not in field_names
    with pytest.raises(dataclasses.FrozenInstanceError):
        endpoint.url = "https://evil.example.test/"


def test_event_is_frozen():
    event = _event()
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.created_at_ms = 0


def test_delivery_is_frozen_and_defaults_are_empty():
    delivery = _delivery()
    assert delivery.delivered_at_ms is None
    assert delivery.last_status_code is None
    assert delivery.last_error is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        delivery.attempts = 99


def test_delivery_replace_produces_an_independent_row():
    original = _delivery()
    advanced = dataclasses.replace(original, attempts=1, last_status_code=500)
    assert original.attempts == 0 and original.last_status_code is None
    assert advanced.attempts == 1 and advanced.last_status_code == 500
    assert advanced.id == original.id


# --------------------------------------------------- payload snapshotting

# A payload with a JSON ARRAY in it: snapshot_payload stores lists as
# tuples, which json.dumps cannot encode, so the round-trip back to a list
# is load-bearing for every signed body.
LIST_PAYLOAD = {"connection_id": "conn_abc123", "xs": [1, 2, {"b": 2, "a": 1}], "empty": []}
GOLDEN_LIST_BODY = (
    '{"created_at_ms":1754000000000,'
    '"data":{"connection_id":"conn_abc123","empty":[],"xs":[1,2,{"a":1,"b":2}]},'
    '"delivery_id":"dlv_cb33eb38d1b7aa44",'
    '"event_id":"evt_490b3195618c4099",'
    '"type":"connection.created"}'
)

# The five things JSON cannot carry. Each MUST raise ValidationError at the
# snapshot boundary — a bare TypeError here means json.dumps blows up at
# SIGNING time instead, inside the deliverer, on a row already persisted.
NON_JSON_PAYLOADS = [
    pytest.param({"amount": Decimal("1.5")}, id="decimal"),
    pytest.param({"tags": {"a", "b"}}, id="set"),
    pytest.param({"blob": b"raw"}, id="bytes"),
    pytest.param({1: "one"}, id="non-str-key"),
    pytest.param({"ratio": float("nan")}, id="nan"),
    pytest.param({"ratio": float("inf")}, id="inf"),
    pytest.param({"ratio": float("-inf")}, id="-inf"),
]

NESTED_NON_JSON_PAYLOADS = [
    pytest.param({"meta": {"amount": Decimal("1.5")}}, id="decimal-in-mapping"),
    pytest.param({"xs": [1, Decimal("2")]}, id="decimal-in-list"),
    pytest.param({"meta": {"tags": {"a"}}}, id="set-in-mapping"),
    pytest.param({"xs": [b"raw"]}, id="bytes-in-list"),
    pytest.param({"meta": {2: "two"}}, id="non-str-key-in-mapping"),
    pytest.param({"xs": [{"ratio": float("nan")}]}, id="nan-in-list-in-mapping"),
]


def test_validation_error_is_not_a_type_error():
    # The whole point of the branches below: ValidationError is a distinct
    # class, so `pytest.raises(ValidationError)` cannot be satisfied by the
    # bare TypeError json.dumps would have raised later.
    assert not issubclass(ValidationError, TypeError)
    assert not issubclass(TypeError, ValidationError)


@pytest.mark.parametrize("payload", NON_JSON_PAYLOADS)
def test_snapshot_payload_rejects_what_json_cannot_carry(payload):
    with pytest.raises(ValidationError):
        snapshot_payload(payload)


@pytest.mark.parametrize("payload", NESTED_NON_JSON_PAYLOADS)
def test_snapshot_payload_rejects_non_json_at_any_depth(payload):
    with pytest.raises(ValidationError):
        snapshot_payload(payload)


@pytest.mark.parametrize("payload", [None, 1, "text", [1, 2], ("a",), b"raw"])
def test_snapshot_payload_rejects_a_non_mapping_top_level(payload):
    with pytest.raises(ValidationError):
        snapshot_payload(payload)


def test_snapshot_payload_accepts_every_json_scalar():
    snapshot = snapshot_payload(
        {"s": "x", "i": 10**77, "f": 1.5, "t": True, "n": None, "neg": -3}
    )
    assert dict(snapshot) == {
        "s": "x",
        "i": 10**77,
        "f": 1.5,
        "t": True,
        "n": None,
        "neg": -3,
    }


def test_snapshot_payload_freezes_mappings_and_lists_all_the_way_down():
    snapshot = snapshot_payload(LIST_PAYLOAD)
    assert isinstance(snapshot["xs"], tuple)
    assert isinstance(snapshot["xs"][2], Mapping)
    with pytest.raises(TypeError):
        snapshot["injected"] = "boom"
    with pytest.raises(TypeError):
        snapshot["xs"][0] = 99
    with pytest.raises(TypeError):
        snapshot["xs"][2]["a"] = 99
    with pytest.raises(TypeError):
        del snapshot["connection_id"]


def test_snapshot_payload_does_not_alias_the_callers_mapping():
    payload = {"connection_id": "conn_abc123", "meta": {"kind": "address"}}
    snapshot = snapshot_payload(payload)
    payload["meta"]["kind"] = "tampered"
    payload["injected"] = "boom"
    assert dict(snapshot) == {"connection_id": "conn_abc123", "meta": {"kind": "address"}}
    assert dict(snapshot["meta"]) == {"kind": "address"}


def test_a_snapshotted_payload_hashes_and_serialises_like_the_plain_one():
    # tuple-vs-list and mappingproxy-vs-dict must be invisible on the wire:
    # otherwise a stored event's id stops matching its own content.
    snapshot = snapshot_payload(DATA)
    assert event_id(PROJECT, EventName.CONNECTION_CREATED, T0, snapshot) == (
        GOLDEN_EVENT_ID
    )
    assert delivery_body(_event(data=snapshot), _delivery()) == GOLDEN_BODY


def test_a_list_payload_survives_the_snapshot_round_trip_byte_for_byte():
    assert delivery_body(_event(data=LIST_PAYLOAD), _delivery()) == GOLDEN_LIST_BODY
    assert delivery_body(
        _event(data=snapshot_payload(LIST_PAYLOAD)), _delivery()
    ) == GOLDEN_LIST_BODY
    assert event_id(
        PROJECT, EventName.CONNECTION_CREATED, T0, snapshot_payload(LIST_PAYLOAD)
    ) == event_id(PROJECT, EventName.CONNECTION_CREATED, T0, LIST_PAYLOAD)
