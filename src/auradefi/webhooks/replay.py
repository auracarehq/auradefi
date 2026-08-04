"""Replay: re-deliver a webhook the receiver lost (SPEC §7.3).

The primitive Zerion and Vezgo both lack. Once a delivery has burned its
six attempts it is not gone — it is a ``dead_letter`` row an operator can
re-arm, one delivery or the whole backlog, after the receiver is fixed.

A replay is a NEW delivery for the SAME (endpoint, event) pair: fresh
``dlv_`` id at the next ``replay_ordinal``, ``attempts = 0``, PENDING,
``created_at_ms == next_attempt_at_ms == clock.now_ms()`` — the full
six-attempt schedule restarts. The original row is never mutated: the
dead-letter view keeps its forensic value, and "replayed twice" is
visible as two extra rows rather than a lost counter.

Because the signed body carries the EVENT's ``created_at_ms`` and no
attempt counter, a replay body differs from the original in exactly one
field — ``delivery_id`` — so a receiver de-duplicating on
``X-Auradefi-Delivery`` sees a genuinely new delivery of a known event.

No httpx here: replay only enqueues. The host's next
``Deliverer.tick`` sends it, through the same signing path.
"""

from __future__ import annotations

from auradefi.clock import Clock
from auradefi.webhooks.deliver import WebhookStore
from auradefi.webhooks.models import Delivery


def replay(
    store: WebhookStore,
    project_id: str,
    delivery_id: str,
    clock: Clock,
) -> Delivery:
    """Re-arm one delivery; return the NEW PENDING row.

    Any status may be replayed — dead-lettered, delivered, or still
    pending. An unknown or cross-project ``delivery_id`` raises
    :class:`auradefi.errors.NotFoundError`, indistinguishably.
    """
    return store.create_replay(project_id, delivery_id, clock)


def replay_dead_letter(
    store: WebhookStore,
    project_id: str,
    clock: Clock,
) -> tuple[Delivery, ...]:
    """Re-arm every DEAD_LETTER delivery of one project.

    Returns the new rows in the order the dead-lettered originals were
    created; ``()`` when the project's dead-letter view is empty (and
    for a project with no rows at all). The originals stay
    DEAD_LETTER, so a second call re-arms them again at the next
    ordinal — draining the backlog is the deliverer's job, not this
    function's.
    """
    # dead_letter() is a snapshot tuple, so re-arming inside the loop
    # cannot feed the new PENDING rows back into it.
    return tuple(
        store.create_replay(project_id, delivery.id, clock)
        for delivery in store.dead_letter(project_id)
    )
