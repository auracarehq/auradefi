"""Coverage and webhook administration (SPEC §7.3, §10, rules #8, #10).

``GET /coverage`` is the ONE public route: no credential, no quota
consumed and — because it never sets ``request.state.project_id`` — no
``X-RateLimit-*`` headers. Its body is generated from the live chain
registry and the capabilities the host actually bound, never from prose
(rule #10, SPEC §12 risk 6: "Docs lie — including your own").

The webhook surface is the anti-Vezgo, anti-Zerion one (rule #8): a
project registers its own endpoint and gets a signing secret back
IMMEDIATELY and EXACTLY ONCE — there is no allowlist, no support ticket
and no source-IP check anywhere. Deliveries, the dead-letter view and
replay are readable by the project that owns them and by nobody else.

There is deliberately NO endpoint that runs the deliverer: delivery is
host-scheduled (SPEC §8, "the host owns scheduling"), so an operator
ticks ``webhooks.deliver.Deliverer`` from their own cron.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from auradefi.api.deps import Deps, WebhookSink, consume_quota, require_api_key
from auradefi.api.wire import coverage_payload
from auradefi.errors import ValidationError
from auradefi.tenancy.models import Scope
from auradefi.webhooks.models import Delivery, DeliveryStatus, Endpoint, EventName
from auradefi.webhooks.replay import replay
from auradefi.webhooks.urls import validate_endpoint_url


class EndpointRequest(BaseModel):
    """``POST /webhooks/endpoints`` body — exactly ``{url, events}``.

    ``events`` omitted or ``null`` subscribes to all seven; the names are
    validated in the route so the 422 can name the legal values.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    events: list[str] | None = None


def _endpoint_wire(endpoint: Endpoint) -> dict[str, Any]:
    """Project one ``Endpoint``: ``{id, url, events, created_at_ms}``.

    NO ``secret`` key, here or anywhere else that lists endpoints — the
    plaintext is returned exactly once, by the registration route.
    ``events`` is the stored filter, sorted; ``[]`` means "all seven".
    """
    return {
        "id": endpoint.id,
        "url": endpoint.url,
        "events": sorted(str(name) for name in endpoint.events),
        "created_at_ms": endpoint.created_at_ms,
    }


def _delivery_wire(sink: WebhookSink, project_id: str, delivery: Delivery) -> dict[str, Any]:
    """Project one ``Delivery`` — twelve keys, every one always present.

    ``{id, endpoint_id, event_id, event_name, status, attempts,
    created_at_ms, next_attempt_at_ms, delivered_at_ms, last_status_code,
    last_error, replay_ordinal}``. A ``None`` timestamp or status code
    serialises as JSON ``null``, never as an omitted key.

    ``event_name`` is read back through the sink's ``get_event`` — the
    delivery row stores only ``event_id``, and a receiver that must route
    on the event type should not have to make a second call.
    """
    return {
        "id": delivery.id,
        "endpoint_id": delivery.endpoint_id,
        "event_id": delivery.event_id,
        "event_name": str(sink.get_event(project_id, delivery.event_id).name),
        "status": str(delivery.status),
        "attempts": delivery.attempts,
        "created_at_ms": delivery.created_at_ms,
        "next_attempt_at_ms": delivery.next_attempt_at_ms,
        "delivered_at_ms": delivery.delivered_at_ms,
        "last_status_code": delivery.last_status_code,
        "last_error": delivery.last_error,
        "replay_ordinal": delivery.replay_ordinal,
    }


def _parsed_events(names: list[str] | None) -> list[str]:
    """The requested subscription, validated against the seven names.

    ``None`` (all seven) reads as ``[]`` — the store's own "no filter".
    An unknown name raises :class:`~auradefi.errors.ValidationError`
    NAMING every legal value, because a typo in a subscription list is
    silence at 3am otherwise.
    """
    if not names:
        return []
    legal = sorted(str(member) for member in EventName)
    unknown = sorted({name for name in names if name not in legal})
    if unknown:
        raise ValidationError(
            f"unknown webhook event(s) {unknown}; legal values: {legal}"
        )
    return list(names)


def _requested_status(status: str | None) -> DeliveryStatus | None:
    """The ``?status=`` filter as a member, or ``None`` for no filter.

    An unrecognised value raises :class:`~auradefi.errors.ValidationError`
    naming the three legal values rather than answering an empty list —
    a filtered-to-nothing response reads exactly like "you have none".
    """
    if status is None:
        return None
    legal = [str(member) for member in DeliveryStatus]
    if status not in legal:
        raise ValidationError(
            f"unknown delivery status {status!r}; legal values: {legal}"
        )
    return DeliveryStatus(status)


def router(deps: Deps) -> APIRouter:
    """Build the coverage/webhooks router over ``deps``.

    * ``GET /coverage`` — no auth, no quota, no rate-limit headers.
    * ``POST /webhooks/endpoints`` — api key + ``users:admin``, quota,
      structural URL validation (rule #8: no allowlist), 201 with the
      64-hex ``secret`` returned exactly once. A repeat ``(project,
      url)`` is a 409 carrying the existing ``whe_`` id.
    * ``GET /webhooks/endpoints`` — this project's endpoints, no secrets.
    * ``GET /webhooks/deliveries?status=`` — creation order, optionally
      filtered.
    * ``GET /webhooks/dead_letter`` — the same shape, dead-lettered only.
    * ``POST /webhooks/deliveries/{delivery_id}/replay`` — 202 with the
      NEW delivery; unknown or cross-project is a 404.
    """
    api = APIRouter()

    @api.get("/coverage")
    def coverage() -> dict[str, Any]:
        """Generated from the registry and the bound capabilities only."""
        return coverage_payload(
            deps.chains.chains(), deps.capabilities, deps.clock.now_ms()
        )

    @api.post("/webhooks/endpoints", status_code=201)
    def register_endpoint(body: EndpointRequest, request: Request) -> dict[str, Any]:
        """Self-serve registration; the secret is returned exactly once."""
        key = require_api_key(deps, request, Scope.USERS_ADMIN)
        consume_quota(deps, key.project_id)
        url = validate_endpoint_url(body.url)
        endpoint, secret = deps.webhooks.register_endpoint(
            key.project_id, url, _parsed_events(body.events), deps.clock
        )
        return {**_endpoint_wire(endpoint), "secret": secret}

    @api.get("/webhooks/endpoints")
    def list_endpoints(request: Request) -> dict[str, Any]:
        """This project's endpoints, in registration order, secretless."""
        key = require_api_key(deps, request, Scope.USERS_ADMIN)
        consume_quota(deps, key.project_id)
        rows = [
            _endpoint_wire(endpoint)
            for endpoint in deps.webhooks.endpoints(key.project_id)
        ]
        return {"endpoints": rows, "count": len(rows)}

    @api.get("/webhooks/deliveries")
    def list_deliveries(request: Request, status: str | None = None) -> dict[str, Any]:
        """Every delivery in creation order, optionally filtered by status."""
        key = require_api_key(deps, request, Scope.USERS_ADMIN)
        consume_quota(deps, key.project_id)
        wanted = _requested_status(status)
        rows = [
            _delivery_wire(deps.webhooks, key.project_id, delivery)
            for delivery in deps.webhooks.deliveries(key.project_id)
            if wanted is None or delivery.status == wanted
        ]
        return {"deliveries": rows, "count": len(rows)}

    @api.get("/webhooks/dead_letter")
    def dead_letter(request: Request) -> dict[str, Any]:
        """The operator view: deliveries that burned all six attempts."""
        key = require_api_key(deps, request, Scope.USERS_ADMIN)
        consume_quota(deps, key.project_id)
        rows = [
            _delivery_wire(deps.webhooks, key.project_id, delivery)
            for delivery in deps.webhooks.dead_letter(key.project_id)
        ]
        return {"deliveries": rows, "count": len(rows)}

    @api.post("/webhooks/deliveries/{delivery_id}/replay", status_code=202)
    def replay_delivery(delivery_id: str, request: Request) -> dict[str, Any]:
        """Re-arm one delivery; the original row is never mutated."""
        key = require_api_key(deps, request, Scope.USERS_ADMIN)
        consume_quota(deps, key.project_id)
        delivery = replay(deps.webhooks, key.project_id, delivery_id, deps.clock)
        return _delivery_wire(deps.webhooks, key.project_id, delivery)

    return api
