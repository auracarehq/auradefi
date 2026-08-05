"""Webhook value objects, deterministic ids, and the pinned wire format.

SPEC §7.3 and rule #8. Everything in this module is a PUBLIC STABILITY
CONTRACT pinned in docs/internal/DECISIONS.md ("Webhook ids", "Webhook retry
schedule"). Changing a byte here breaks every receiver that verifies a
signature or de-duplicates on a delivery id.

Pinned, to the byte:

* ``canonical_json(obj) = json.dumps(obj, separators=(",", ":"),
  sort_keys=True)``, compact, key-sorted, ``ensure_ascii`` left at its
  default so non-ASCII is ``\\uXXXX``-escaped;
* ``endpoint_id  = "whe_" + sha256(f"{project_id}|{url}")[:16]``;
* ``event_id     = "evt_" + sha256(f"{project_id}|{name}|{created_at_ms}|
  {canonical_json(data)}")[:16]``;
* ``delivery_id  = "dlv_" + sha256(f"{endpoint_id}|{event_id}|
  {replay_ordinal}")[:16]`` (ordinal 0 original, +1 per replay);
* the delivery body is ``canonical_json`` over EXACTLY five keys,
  ``{created_at_ms, data, delivery_id, event_id, type}``. No attempt
  counter, on purpose: retries re-send byte-identical bytes (a receiver's
  de-dup is trivial) and a replay differs in ``delivery_id`` alone;
* ``RETRY_SCHEDULE_MS`` offsets run from the delivery's ``created_at_ms``,
  so the sixth and last attempt lands at exactly +24h and ``tick`` stays
  idempotent.

Rule #8, named casualty: Vezgo authenticates webhooks by SOURCE-IP
ALLOWLIST and Zerion requires support to hand-whitelist each callback
URL. Neither exists here. :mod:`auradefi.webhooks.urls` is purely
STRUCTURAL: ``http://127.0.0.1:9000/hook`` registers fine. It rejects
SYNTAX, never a destination: a URL httpx cannot parse or would rewrite
is not policy, it is a POST to somewhere other than as registered.

Stdlib only; all timestamps are ms-epoch ints.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from auradefi.errors import ValidationError


class EventName(StrEnum):
    """The seven webhook events (SPEC §7.3 "Rich event set").

    Vezgo ships two terminal events; this is the whole lifecycle. The
    set is closed. A new member is a public API change.
    """

    CONNECTION_CREATED = "connection.created"
    CONNECTION_DELETED = "connection.deleted"
    HOLDINGS_UPDATED = "holdings.updated"
    TRANSACTIONS_AVAILABLE = "transactions.available"
    SYNC_STARTED = "sync.started"
    SYNC_FAILED = "sync.failed"
    REORG_DETECTED = "reorg.detected"


class DeliveryStatus(StrEnum):
    """Lifecycle of one delivery attempt chain.

    ``PENDING`` → ``DELIVERED`` on any 2xx, or ``PENDING`` →
    ``DEAD_LETTER`` after the sixth failure. There is no "failed"
    intermediate state: a delivery with attempts remaining is still
    pending.
    """

    PENDING = "pending"
    DELIVERED = "delivered"
    DEAD_LETTER = "dead_letter"


#: Offsets from ``Delivery.created_at_ms`` at which attempt k is due
#: (DECISIONS "Webhook retry schedule"; Dune SIM's 1 + 5 retries).
RETRY_SCHEDULE_MS: tuple[int, ...] = (
    0, 60_000, 300_000, 1_800_000, 7_200_000, 86_400_000
)

#: Total attempts before dead-lettering: one per schedule slot.
MAX_ATTEMPTS: int = len(RETRY_SCHEDULE_MS)

#: The five body keys, sorted (``canonical_json`` sorts them anyway).
BODY_KEYS: tuple[str, ...] = (
    "created_at_ms", "data", "delivery_id", "event_id", "type"
)


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A registered receiver. NOTE: no secret field, deliberately.

    The 64-hex plaintext signing secret is returned once by
    ``WebhookStore.register_endpoint`` and read back only through
    ``WebhookStore.endpoint_secret``; it must never reach a projection
    that a UI or an API response might render.

    ``events`` is the subscription filter: an EMPTY frozenset means
    "every event" (the common case), otherwise only the named ones.
    """

    id: str
    project_id: str
    url: str
    events: frozenset[EventName]
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class Event:
    """One emitted event; the payload every delivery of it re-sends.

    ``created_at_ms`` is the EVENT's creation time and is what lands in
    the body: a replay created hours later still carries this value, so
    the replay body differs from the original in ``delivery_id`` alone.
    """

    id: str
    project_id: str
    name: EventName
    data: Mapping[str, Any]
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class Delivery:
    """One (endpoint, event) attempt chain, durable across retries.

    ``next_attempt_at_ms`` is ``None`` exactly when the chain is over
    (``DELIVERED`` or ``DEAD_LETTER``). ``attempts`` counts attempts
    ALREADY made, so a fresh row is 0 and a dead-lettered one is
    ``MAX_ATTEMPTS``. ``replay_ordinal`` is 0 for the original.
    """

    id: str
    project_id: str
    endpoint_id: str
    event_id: str
    replay_ordinal: int
    status: DeliveryStatus
    attempts: int
    created_at_ms: int
    next_attempt_at_ms: int | None
    delivered_at_ms: int | None = None
    last_status_code: int | None = None
    last_error: str | None = None


def canonical_json(obj: Any) -> str:
    """``json.dumps(obj, separators=(",", ":"), sort_keys=True)``.

    The one serialisation used for id derivation and for the signed
    body. Compact separators, sorted keys, default ``ensure_ascii``:
    pinned in DECISIONS, so nothing here may be "improved".
    """
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def _short_digest(material: str) -> str:
    """First 16 hex chars of the UTF-8 sha256: every id's tail."""
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _plain(value: Any) -> Any:
    """The JSON-native twin of ``value``: mappings → dict, sequences → list.

    :func:`snapshot_payload` stores payloads as ``MappingProxyType`` and
    tuples, which ``json.dumps`` cannot encode. Every hash and every
    signed body flows through here first, so :func:`canonical_json` stays
    the pinned one-liner and never grows an encoder hook.
    """
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _frozen(value: Any) -> Any:
    """Deep, immutable, JSON-clean twin of ``value``; else ``ValidationError``."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationError(f"webhook payload float is not JSON: {value!r}")
        return value
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"webhook payload key must be str: {key!r}")
            frozen[key] = _frozen(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_frozen(item) for item in value)
    raise ValidationError(
        f"webhook payload is not JSON-serialisable: {type(value).__name__}"
    )


def snapshot_payload(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """A deep, immutable, JSON-clean copy of an emitted event's payload.

    :func:`event_id` is a content address over the payload and every
    retry must re-send byte-identical bytes, so the store may never hold
    a mapping the caller, or anything handed an :class:`Event` later,
    can still write to. Mappings become ``MappingProxyType`` and lists
    become tuples all the way down.

    Anything JSON cannot carry (``Decimal``, ``set``, ``bytes``, a
    non-``str`` key, a non-finite float) raises
    :class:`auradefi.errors.ValidationError` HERE, at the boundary the
    caller controls, rather than a bare ``TypeError`` out of
    ``json.dumps`` at signing time.
    """
    frozen = _frozen(data)
    if not isinstance(frozen, Mapping):
        raise ValidationError(f"webhook payload must be a mapping: {type(data)}")
    return frozen




def parse_event_name(value: str) -> EventName:
    """Return the :class:`EventName` member whose value is ``value``.

    An unknown name raises :class:`auradefi.errors.ValidationError`
    (never ``KeyError``/``ValueError``). The subscription list arrives
    from HTTP.
    """
    try:
        return EventName(value)
    except ValueError:
        raise ValidationError(f"unknown webhook event: {value!r}") from None


def endpoint_id(project_id: str, url: str) -> str:
    """``"whe_" + sha256(f"{project_id}|{url}".encode())[:16]``.

    Deterministic and project-scoped: the same URL under two projects
    yields two different ids, and re-registering the same URL under one
    project collides on purpose (that collision is the 409).
    """
    return "whe_" + _short_digest(f"{project_id}|{url}")


def event_id(
    project_id: str,
    name: EventName,
    created_at_ms: int,
    data: Mapping[str, Any],
) -> str:
    """``"evt_" + sha256(f"{project_id}|{name}|{created_at_ms}|
    {canonical_json(data)}".encode())[:16]``.

    ``name`` interpolates as its wire value (StrEnum). Identical inputs
    give an identical id. That determinism is what makes ``emit``
    idempotent. A payload already frozen by :func:`snapshot_payload`
    hashes identically to the plain mapping it was taken from.
    """
    payload = canonical_json(_plain(data))
    return "evt_" + _short_digest(f"{project_id}|{name}|{created_at_ms}|{payload}")


def delivery_id(endpoint_id: str, event_id: str, replay_ordinal: int) -> str:
    """``"dlv_" + sha256(f"{endpoint_id}|{event_id}|{replay_ordinal}")[:16]``.

    ``replay_ordinal`` is 0 for the original delivery and increments by
    one per replay of the same (endpoint, event) pair.
    """
    return "dlv_" + _short_digest(f"{endpoint_id}|{event_id}|{replay_ordinal}")


def delivery_body(event: Event, delivery: Delivery) -> str:
    """The exact string POSTed (and signed) for ``delivery``.

    ``canonical_json`` over exactly ``{created_at_ms: event
    .created_at_ms, data: event.data, delivery_id: delivery.id,
    event_id: event.id, type: event.name}``: five keys, no attempt
    counter, so every retry re-sends byte-identical bytes.
    """
    return canonical_json(
        {
            "created_at_ms": event.created_at_ms,
            "data": _plain(event.data),
            "delivery_id": delivery.id,
            "event_id": event.id,
            "type": str(event.name),
        }
    )


def due_at_ms(created_at_ms: int, attempt: int) -> int | None:
    """When attempt number ``attempt`` (zero-based) is due.

    ``created_at_ms + RETRY_SCHEDULE_MS[attempt]``, or ``None`` once
    ``attempt >= MAX_ATTEMPTS``. The chain is exhausted and the
    delivery dead-letters. A negative ``attempt`` raises
    :class:`auradefi.errors.ValidationError`.
    """
    if attempt < 0:
        raise ValidationError(f"attempt must be zero or positive: {attempt}")
    if attempt >= MAX_ATTEMPTS:
        return None
    return created_at_ms + RETRY_SCHEDULE_MS[attempt]
