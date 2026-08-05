"""The webhook seam: what a host-supplied sink must provide (SPEC §7.3).

Split out of ``api/deps.py`` rather than declared there, for two reasons
that both come from RELEASE_0.1.1 §5 Wave C:

* ``deps.py`` sits at the 400-line hard cap, and stating return shapes
  honestly costs lines. The house rule is to split the module, not to
  keep the declaration thin enough to fit.
* ``tests/api/test_deps.py`` pins that ``api/deps.py`` imports neither
  ``portfolio`` nor ``webhooks``, so the injection record stays bindable
  by a host that never installed the shipped stores. Nothing here
  imports ``webhooks`` either: the row Protocols below say what the
  routes READ without naming the classes that happen to ship.

That distinction is the whole point. #27 and #28 were not "two missing
members": they were a declared interface that promised less than its
consumers required, so the shipped store satisfied the routes by
accident while every host-written sink got an unhandled 500. A return
type of bare ``object`` is unimplementable from the declaration alone:
it names no attribute, so a host cannot know what to return, and each
one it omits is another 500. The row Protocols state the whole read
surface; ``tests/contract/seams/test_wave1_webhook_sink.py`` binds a
sink written from these declarations ONLY and drives every webhook route
through it, which is the test no in-repo test was doing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol, runtime_checkable

from auradefi.clock import Clock


class EndpointRow(Protocol):
    """What ``_endpoint_wire`` (api/routes/admin.py) reads off an endpoint."""

    id: str
    url: str
    events: Sequence[str]
    created_at_ms: int


class DeliveryRow(Protocol):
    """What ``_delivery_wire`` (api/routes/admin.py) reads off a delivery.

    ``status`` is typed ``object`` deliberately: the shipped store returns
    a ``DeliveryStatus`` StrEnum, but naming it here would import
    ``webhooks`` and re-couple the seam. The route projects it through
    ``str``, so any value that stringifies to a pinned status name works.
    """

    id: str
    endpoint_id: str
    event_id: str
    status: object
    attempts: int
    created_at_ms: int
    next_attempt_at_ms: int | None
    delivered_at_ms: int | None
    last_status_code: int | None
    last_error: str | None
    replay_ordinal: int


class EventRow(Protocol):
    """What the delivery wire reads off an event.

    Only ``name``: a stored delivery holds an ``event_id`` while the wire
    exposes ``event_name``, so the admin routes MUST read the event back
    through the sink rather than around it.
    """

    name: object


@runtime_checkable
class WebhookSink(Protocol):
    """Structural seam onto the project-scoped webhook store (SPEC §7.3).

    Never an import of ``auradefi.webhooks``: any object with EVERY
    member below is a sink. Every member the routes reach for is below,
    RETURN SHAPES INCLUDED, and nothing else is. An unused promise would
    make every host-supplied sink implement dead code just to satisfy
    ``isinstance(sink, WebhookSink)``. That is why ``get_delivery`` is
    absent: the shipped store calls its own, internally, and no route
    ever does.
    """

    def register_endpoint(
        self,
        project_id: str,
        url: str,
        events: Iterable[str],
        clock: Clock,
    ) -> tuple[EndpointRow, str]:
        """Register (or re-register) one endpoint for ``project_id``.

        Answers the PAIR the registration route unpacks, ``(endpoint,
        plaintext_secret)``, never one object: the 64-hex secret is
        returned exactly once, by that route (SPEC §7.3, §5 #28).
        """
        raise NotImplementedError

    def endpoints(self, project_id: str) -> Sequence[EndpointRow]:
        """This project's endpoints, never another project's."""
        raise NotImplementedError

    def emit(
        self,
        project_id: str,
        name: str,
        data: Mapping[str, object],
        clock: Clock,
    ) -> EventRow:
        """Emit event ``name`` to every endpoint subscribed to it."""
        raise NotImplementedError

    def deliveries(self, project_id: str) -> Sequence[DeliveryRow]:
        """Every delivery for this project, in creation order."""
        raise NotImplementedError

    def dead_letter(self, project_id: str) -> Sequence[DeliveryRow]:
        """Deliveries that exhausted the pinned retry schedule."""
        raise NotImplementedError

    def get_event(self, project_id: str, event_id: str) -> EventRow:
        """The event a delivery carries, inside this project's scope."""
        raise NotImplementedError

    def create_replay(
        self, project_id: str, delivery_id: str, clock: Clock
    ) -> DeliveryRow:
        """The NEW PENDING delivery re-arming one of this project's rows.

        Declared because the replay route reaches it INDIRECTLY, through
        ``webhooks.replay.replay(deps.webhooks, ...)``: grepping this
        package for ``webhooks.create_replay`` finds nothing while every
        host-written sink 500s there (§5 #27). An unknown or cross-project
        id raises ``NotFoundError``; the original row is never mutated.
        """
        raise NotImplementedError
