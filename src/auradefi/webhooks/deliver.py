"""Endpoint registry + durable, HOST-SCHEDULED webhook delivery.

SPEC §7.3: self-serve webhooks with durable delivery. Zerion retries 3x
over ~60s then drops the event permanently and hand-whitelists callback
URLs; Vezgo authenticates by source-IP allowlist. None of that here: six
attempts spread over exactly 24h (DECISIONS "Webhook retry schedule"),
then a dead-letter row that survives for the replay path, and no
allowlist of any kind.

Scheduling belongs to the host (SPEC §8). ``Deliverer.tick(now_ms)``
drains whatever is due through an INJECTED ``httpx.Client`` and returns:
no threads, no sleeps, no read of the wall clock anywhere in this module.
A cron host and a busy-loop host get identical, replayable behaviour, and
tests fast-forward 24 hours in six calls. NO httpx exception escapes
``tick``. A refused connection is a recorded failed attempt, not an
exception that strands every other due delivery.

Tenancy: every read and write is keyed by ``project_id`` first, and a row
under another project is INDISTINGUISHABLE from one that never existed
(``NotFoundError``, same message shape). :meth:`WebhookStore.due` is the
single deliberately host-wide read. The deliverer is infrastructure, not
a tenant.

This is the only module in ``webhooks/`` that imports httpx.
"""

from __future__ import annotations

import dataclasses
import secrets
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import httpx

from auradefi.clock import Clock, SystemClock
from auradefi.errors import ConflictError, NotFoundError
from auradefi.webhooks import models, sign, urls

#: All four roots of httpx's exception surface: ``HTTPError`` covers only
#: the request/response tree, while ``InvalidURL``, ``StreamError`` and
#: ``CookieConflict`` descend straight from ``Exception``. The drain is
#: HOST-WIDE, so one escaping costs every tenant's due row, not one.
_CLIENT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    httpx.InvalidURL,
    httpx.StreamError,
    httpx.CookieConflict,
)


def _new_delivery(
    project_id: str, endpoint_id: str, event_id: str, ordinal: int, at_ms: int
) -> models.Delivery:
    """A fresh PENDING row for one (endpoint, event), attempt 0 due now.

    Shared by :meth:`WebhookStore.emit` (ordinal 0) and
    :meth:`WebhookStore.create_replay` (the next ordinal): a replay is an
    original but for its ordinal, so the two must never drift apart.
    """
    return models.Delivery(
        id=models.delivery_id(endpoint_id, event_id, ordinal),
        project_id=project_id, endpoint_id=endpoint_id, event_id=event_id,
        replay_ordinal=ordinal, status=models.DeliveryStatus.PENDING, attempts=0,
        created_at_ms=at_ms, next_attempt_at_ms=at_ms,
    )


class WebhookStore:
    """In-memory, project-keyed store of endpoints, events, deliveries.

    All state lives in instance dicts: two stores never share state.
    ``entropy`` is injectable exactly as in ``tenancy``'s stores and must
    mirror ``secrets.token_hex``: ``entropy(n)`` returns ``2n`` lowercase
    hex chars. An endpoint's signing secret is ``entropy(32)``.
    """

    def __init__(self, entropy: Callable[[int], str] = secrets.token_hex) -> None:
        """Start empty; bind the entropy source used for endpoint secrets."""
        self._entropy = entropy
        self._endpoints: dict[str, dict[str, models.Endpoint]] = {}
        self._secrets: dict[tuple[str, str], str] = {}
        self._events: dict[str, dict[str, models.Event]] = {}
        self._deliveries: dict[str, dict[str, models.Delivery]] = {}

    def register_endpoint(
        self,
        project_id: str,
        url: str,
        events: Iterable[models.EventName | str] = (),
        clock: Clock | None = None,
    ) -> tuple[models.Endpoint, str]:
        """Register a receiver; return ``(endpoint, plaintext_secret)``.

        ``url`` passes ``urls.validate_endpoint_url`` unchanged and each
        entry of ``events`` passes ``models.parse_event_name``
        (``ValidationError``, nothing created); an EMPTY ``events``
        subscribes to all seven. ``id = models.endpoint_id(project_id,
        url)``, ``created_at_ms = clock.now_ms()``. Registration is the
        one call whose clock may be omitted (a host wiring endpoints from
        config at boot); everything time-sensitive demands one. The
        secret is ``entropy(32)``, returned here and readable only via
        :meth:`endpoint_secret`. :Class:`Endpoint` has no such field.

        A repeat ``(project_id, url)`` raises ``ConflictError`` with
        ``existing_id`` set to the existing ``whe_`` id (§7.1's 409) and
        changes nothing; the first secret stays valid. The same URL under
        another project is a different endpoint, not a conflict.
        """
        checked = urls.validate_endpoint_url(url)
        names = frozenset(models.parse_event_name(name) for name in events)
        identifier = models.endpoint_id(project_id, checked)
        rows = self._endpoints.setdefault(project_id, {})
        existing = rows.get(identifier)
        if existing is not None:
            raise ConflictError(
                f"webhook endpoint already registered: {checked}",
                existing_id=existing.id,
            )
        rows[identifier] = models.Endpoint(
            id=identifier,
            project_id=project_id,
            url=checked,
            events=names,
            created_at_ms=(clock if clock is not None else SystemClock()).now_ms(),
        )
        secret = self._entropy(32)
        self._secrets[project_id, identifier] = secret
        return rows[identifier], secret

    def endpoints(self, project_id: str) -> tuple[models.Endpoint, ...]:
        """This project's endpoints in registration order; ``()`` if none.

        An unknown project is not an error. This store holds no project
        registry (``tenancy`` owns that).
        """
        return tuple(self._endpoints.get(project_id, {}).values())

    def get_endpoint(self, project_id: str, endpoint_id: str) -> models.Endpoint:
        """This project's endpoint by id, else ``NotFoundError``.

        Cross-tenant and absent are the same failure, and the message
        never hints the id was real somewhere else.
        """
        endpoint = self._endpoints.get(project_id, {}).get(endpoint_id)
        if endpoint is None:
            raise NotFoundError(f"webhook endpoint not found: {endpoint_id}")
        return endpoint

    def endpoint_secret(self, project_id: str, endpoint_id: str) -> str:
        """The endpoint's 64-hex plaintext secret; the signer needs it.

        Same tenant gate and ``NotFoundError`` as :meth:`get_endpoint`.
        """
        endpoint = self.get_endpoint(project_id, endpoint_id)
        return self._secrets[project_id, endpoint.id]

    def emit(
        self, project_id: str, name: models.EventName,
        data: Mapping[str, Any], clock: Clock,
    ) -> tuple[models.Delivery, ...]:
        """Record an event and fan it out to subscribed endpoints.

        The :class:`Event` is always recorded (``id =
        models.event_id(...)``, ``created_at_ms = clock.now_ms()``), even
        with no endpoints. One PENDING :class:`Delivery` is created per
        endpoint whose ``events`` is empty or contains ``name``, in
        registration order, with ``replay_ordinal = 0``, ``attempts =
        0``, and ``created_at_ms == next_attempt_at_ms == clock.now_ms()``
        (attempt 0 is due immediately).

        IDEMPOTENT: the ids are deterministic, so re-emitting identical
        ``(name, data)`` at the same millisecond returns the EXISTING
        rows with their current attempt state and creates no duplicates.

        ``data`` must be JSON-carriable and is DEEP-SNAPSHOTTED by
        ``models.snapshot_payload``: the id is a content address, so a
        caller keeping their nested dict must not be able to change a
        signed body under an unchanged ``evt_``/``dlv_``. A ``Decimal``
        or a non-``str`` key raises ``ValidationError`` here, never
        ``TypeError`` at signing time.
        """
        parsed = models.parse_event_name(name)
        at_ms = clock.now_ms()
        event = self._record_event(
            project_id, parsed, models.snapshot_payload(data), at_ms
        )
        rows = self._deliveries.setdefault(project_id, {})
        fanned = []
        for endpoint in self.endpoints(project_id):
            if endpoint.events and parsed not in endpoint.events:
                continue
            identifier = models.delivery_id(endpoint.id, event.id, 0)
            if identifier not in rows:
                rows[identifier] = _new_delivery(
                    project_id, endpoint.id, event.id, 0, at_ms
                )
            fanned.append(rows[identifier])
        return tuple(fanned)

    def _record_event(
        self, project_id: str, name: models.EventName, data: Mapping, at_ms: int
    ) -> models.Event:
        """Record the already-snapshotted payload under its deterministic id."""
        identifier = models.event_id(project_id, name, at_ms, data)
        rows = self._events.setdefault(project_id, {})
        if identifier not in rows:
            rows[identifier] = models.Event(
                id=identifier, project_id=project_id, name=name,
                data=data, created_at_ms=at_ms,
            )
        return rows[identifier]

    def get_event(self, project_id: str, event_id: str) -> models.Event:
        """This project's event by id, else ``NotFoundError``."""
        event = self._events.get(project_id, {}).get(event_id)
        if event is None:
            raise NotFoundError(f"webhook event not found: {event_id}")
        return event

    def deliveries(self, project_id: str) -> tuple[models.Delivery, ...]:
        """This project's deliveries in creation order; never another's."""
        return tuple(self._deliveries.get(project_id, {}).values())

    def get_delivery(self, project_id: str, delivery_id: str) -> models.Delivery:
        """This project's delivery by id, else ``NotFoundError``."""
        delivery = self._deliveries.get(project_id, {}).get(delivery_id)
        if delivery is None:
            raise NotFoundError(f"webhook delivery not found: {delivery_id}")
        return delivery

    def dead_letter(self, project_id: str) -> tuple[models.Delivery, ...]:
        """This project's DEAD_LETTER rows in creation order.

        The operator view SPEC §7.3 asks for; the input to
        ``replay.replay_dead_letter``.
        """
        return tuple(
            delivery
            for delivery in self.deliveries(project_id)
            if delivery.status is models.DeliveryStatus.DEAD_LETTER
        )

    def due(self, now_ms: int) -> tuple[models.Delivery, ...]:
        """Every PENDING delivery due at ``now_ms``, ACROSS projects.

        ``status is PENDING and next_attempt_at_ms is not None and
        next_attempt_at_ms <= now_ms``, ordered by ``(next_attempt_at_ms,
        id)`` ascending so the drain order is total and deterministic.
        """
        ready: list[tuple[int, str, models.Delivery]] = []
        for rows in self._deliveries.values():
            for delivery in rows.values():
                at_ms = delivery.next_attempt_at_ms
                pending = delivery.status is models.DeliveryStatus.PENDING
                if pending and at_ms is not None and at_ms <= now_ms:
                    ready.append((at_ms, delivery.id, delivery))
        ready.sort(key=lambda item: item[:2])
        return tuple(item[2] for item in ready)

    def record_attempt(
        self, project_id: str, delivery_id: str, *, now_ms: int,
        status_code: int | None, error: str | None,
    ) -> models.Delivery:
        """Apply one attempt's outcome and advance the pinned schedule.

        ``attempts`` always increments; ``last_status_code``/``last_error``
        always record this attempt (``status_code`` is ``None`` for a
        transport failure). Then:

        * success (``200 <= status_code < 300``) → ``DELIVERED``,
          ``delivered_at_ms = now_ms``, ``next_attempt_at_ms = None``;
        * failure with attempts left → still ``PENDING``,
          ``next_attempt_at_ms = models.due_at_ms(created_at_ms,
          attempts)``: an offset from CREATION, never from ``now_ms``;
        * failure at ``attempts == MAX_ATTEMPTS`` → ``DEAD_LETTER``,
          ``next_attempt_at_ms = None``.

        A 3xx is a FAILURE: a redirect is not a receipt. An unknown or
        cross-project ``delivery_id`` raises ``NotFoundError``.
        """
        current = self.get_delivery(project_id, delivery_id)
        attempts = current.attempts + 1
        delivered = status_code is not None and 200 <= status_code < 300
        next_at_ms = (
            None if delivered else models.due_at_ms(current.created_at_ms, attempts)
        )
        if delivered:
            status = models.DeliveryStatus.DELIVERED
        elif next_at_ms is None:
            status = models.DeliveryStatus.DEAD_LETTER
        else:
            status = models.DeliveryStatus.PENDING
        updated = dataclasses.replace(
            current,
            status=status,
            attempts=attempts,
            delivered_at_ms=now_ms if delivered else current.delivered_at_ms,
            next_attempt_at_ms=next_at_ms,
            last_status_code=status_code,
            last_error=error,
        )
        self._deliveries[project_id][updated.id] = updated
        return updated

    def create_replay(
        self, project_id: str, delivery_id: str, clock: Clock
    ) -> models.Delivery:
        """Add a fresh delivery for the same (endpoint, event) pair.

        ``replay_ordinal`` is one past the highest already recorded for
        that pair, so ``id = models.delivery_id(endpoint_id, event_id,
        ordinal)`` stays collision-free. The new row is PENDING,
        ``attempts = 0``, ``created_at_ms == next_attempt_at_ms ==
        clock.now_ms()``, the schedule restarts, and the ORIGINAL row
        is left byte-for-byte untouched. Any status may be replayed.
        Unknown or cross-project id raises ``NotFoundError``. Public
        entry points live in :mod:`auradefi.webhooks.replay`.
        """
        original = self.get_delivery(project_id, delivery_id)
        rows = self._deliveries[project_id]
        pair = (original.endpoint_id, original.event_id)
        ordinal = 1 + max(
            row.replay_ordinal
            for row in rows.values()
            if (row.endpoint_id, row.event_id) == pair
        )
        replayed = _new_delivery(project_id, *pair, ordinal, clock.now_ms())
        rows[replayed.id] = replayed
        return replayed


class Deliverer:
    """Drains due deliveries through an injected ``httpx.Client``.

    The client is the host's: it owns timeouts, proxies, TLS. Tests inject
    ``httpx.Client(transport=httpx.MockTransport(handler))``.
    """

    def __init__(self, store: WebhookStore, client: httpx.Client) -> None:
        """Bind the store to drain and the client to POST through."""
        self._store = store
        self._client = client

    def tick(self, now_ms: int) -> tuple[models.Delivery, ...]:
        """Attempt every delivery due at ``now_ms``; return the updates.

        For each row of ``store.due(now_ms)``, in that order: body =
        ``models.delivery_body(event, delivery)``, signed as
        ``sign.sign(secret, now_ms, body)``, the signature timestamp is
        ``now_ms``, so each attempt is fresh while the BODY stays
        byte-identical, then ``client.post(endpoint.url,
        content=body.encode("utf-8"), headers=...)`` carrying
        ``content-type: application/json``, ``X-Auradefi-Event``,
        ``X-Auradefi-Delivery``, ``X-Auradefi-Timestamp``
        (``str(now_ms)``) and ``X-Auradefi-Signature``. Never ``json=``:
        re-serialising would break the signature. Finally
        ``store.record_attempt(...)`` with the status code, or with
        ``status_code=None, error=str(exc)`` for any
        :data:`_CLIENT_ERRORS`: nothing propagates, one dead receiver
        must not stop the drain. Returns the updated rows in the order
        attempted, ``()`` when nothing is due (and then no request is
        made at all).
        """
        return tuple(
            self._attempt(delivery, now_ms) for delivery in self._store.due(now_ms)
        )

    def _attempt(self, delivery: models.Delivery, now_ms: int) -> models.Delivery:
        """POST one delivery and record its outcome; never raises to httpx."""
        project_id = delivery.project_id
        endpoint = self._store.get_endpoint(project_id, delivery.endpoint_id)
        event = self._store.get_event(project_id, delivery.event_id)
        secret = self._store.endpoint_secret(project_id, endpoint.id)
        body = models.delivery_body(event, delivery)
        headers = {
            "content-type": "application/json",
            sign.EVENT_HEADER: str(event.name),
            sign.DELIVERY_HEADER: delivery.id,
            sign.TIMESTAMP_HEADER: str(now_ms),
            sign.SIGNATURE_HEADER: sign.sign(secret, now_ms, body),
        }
        status_code: int | None = None
        error: str | None = None
        try:
            response = self._client.post(
                endpoint.url, content=body.encode("utf-8"), headers=headers
            )
        except _CLIENT_ERRORS as exc:
            # A refused receiver is a recorded attempt, not an exception
            # that strands every other delivery due at this instant.
            error = str(exc) or type(exc).__name__
        else:
            status_code = response.status_code
        return self._store.record_attempt(
            project_id, delivery.id, now_ms=now_ms, status_code=status_code, error=error
        )
