"""How do I get told when something changes, and trust what arrives?

    pip install auradefi && python 09_deliver_signed_webhooks.py

Webhooks are the part of an integration that fails silently. Three things
have to be true or you cannot rely on them, and this file exercises all
three against a receiver it controls:

* **signed.** HMAC-SHA256 over `timestamp.body` with a per-endpoint secret,
  compared in constant time. The verifier ships in the package, so the
  receiving side is not left to improvise it;
* **durable.** A receiver that is down is retried on a schedule that is
  pinned in code — not "eventually" — and ends in a dead letter queue you
  can list rather than in a log line nobody reads;
* **replayable.** A dead letter can be re-sent as a NEW delivery row. The
  original is never mutated, so the history of what you attempted survives.

Delivery is driven by `Deliverer.tick(now_ms)` — you call it from your own
worker. There is no background thread in this package.
"""

from __future__ import annotations

import httpx

from auradefi.clock import FrozenClock
from auradefi.errors import AuthError
from auradefi.webhooks.deliver import Deliverer, WebhookStore
from auradefi.webhooks.models import RETRY_SCHEDULE_MS, EventName
from auradefi.webhooks.replay import replay
from auradefi.webhooks.sign import sign, verify_signature

PROJECT = "proj_demo"
HOOK_URL = "https://hooks.example.com/auradefi"


class Receiver:
    """The other end. Records what arrives and answers one status code."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code)


def deliverer_for(store: WebhookStore, receiver: Receiver) -> Deliverer:
    """A Deliverer over an httpx client you own — here, a mock transport."""
    return Deliverer(store, httpx.Client(transport=httpx.MockTransport(receiver)))


clock = FrozenClock(1_754_000_000_000)
store = WebhookStore()

# ------------------------------------------------------- 1. register, once
endpoint, secret = store.register_endpoint(PROJECT, HOOK_URL, clock=clock)
assert len(secret) == 64
# The secret is returned once, here, and is not readable off the endpoint
# afterwards — list your endpoints and it is absent.
assert secret not in repr(endpoint)
print(f"endpoint {endpoint.id} -> {endpoint.url}")
print(f"  secret shown once at creation ({len(secret)} hex chars), never again")

# --------------------------------------------------------- 2. emit and sign
clock.advance(1_000)
queued_at = clock.now_ms()
(delivery,) = store.emit(PROJECT, EventName.CONNECTION_CREATED,
                         {"connection_id": "conn_demo"}, clock)
assert delivery.status.value == "pending"
event = store.get_event(PROJECT, delivery.event_id)
print(f"\nqueued {delivery.id}: {event.name} "
      f"(attempts={delivery.attempts}, due at {delivery.next_attempt_at_ms})")

receiver = Receiver(200)
(delivered,) = deliverer_for(store, receiver).tick(queued_at)
assert delivered.status.value == "delivered"

(sent,) = receiver.requests
body = sent.content.decode("utf-8")
assert sent.method == "POST" and str(sent.url) == HOOK_URL
assert sent.headers["X-Auradefi-Timestamp"] == str(queued_at)
print(f"  POST -> 200, headers X-Auradefi-Timestamp + X-Auradefi-Signature")
print(f"  body: {body[:88]}…")

# --------------------------------------- 3. the receiving side, done properly
# This is the code YOUR endpoint runs. It is four lines because the verifier
# ships: constant-time compare, and the timestamp is inside the signed
# preimage so a captured request cannot be replayed later.
verify_signature(secret, queued_at, body, sent.headers["X-Auradefi-Signature"],
                 clock.now_ms())
print(f"\nverified with the shipped verifier: {sent.headers['X-Auradefi-Signature'][:34]}…")

for label, corruption in (("body altered by one space", (secret, queued_at, body + " ")),
                          ("wrong secret", ("ff" * 32, queued_at, body)),
                          ("timestamp moved", (secret, queued_at + 1, body))):
    try:
        verify_signature(*corruption, sent.headers["X-Auradefi-Signature"], clock.now_ms())
        raise AssertionError(f"{label} must not verify")
    except AuthError as exc:
        print(f"  {label:<26} -> {type(exc).__name__}: {exc}")

# An old-but-genuine delivery is refused too: the timestamp is signed, so a
# captured request has a shelf life.
stale = sign(secret, queued_at, body)
try:
    verify_signature(secret, queued_at, body, stale, queued_at + 10 * 60 * 1_000)
    raise AssertionError("a 10-minute-old signature must not verify")
except AuthError as exc:
    print(f"  {'10 minutes late':<26} -> {type(exc).__name__}: {exc}")

# ------------------------------------------- 4. a receiver that is down
# The schedule is pinned in `RETRY_SCHEDULE_MS`, so what happens next is a
# fact you can plan around rather than a vendor behaviour you discover.
clock.advance(1_000)
born_at = clock.now_ms()
(pending,) = store.emit(PROJECT, EventName.CONNECTION_CREATED,
                        {"connection_id": "conn_unlucky"}, clock)

down = Receiver(500)
worker = deliverer_for(store, down)
print(f"\nretry schedule (ms after queueing): {RETRY_SCHEDULE_MS}")
for attempt, offset in enumerate(RETRY_SCHEDULE_MS, start=1):
    (row,) = worker.tick(born_at + offset)
    assert row.attempts == attempt and row.last_status_code == 500
    print(f"  attempt {attempt} at +{offset:>8} ms -> 500, "
          f"next at {row.next_attempt_at_ms}")

assert len(down.requests) == len(RETRY_SCHEDULE_MS)
assert row.status.value == "dead_letter" and row.next_attempt_at_ms is None
(dead,) = store.dead_letter(PROJECT)
assert dead.id == pending.id and dead.attempts == 6
print(f"  {row.attempts} attempts over 24h -> dead_letter, and it is LISTED: "
      f"store.dead_letter() has {len(store.dead_letter(PROJECT))}")

# -------------------------------------------------------------- 5. replay
# A new row, a new id, the original left exactly as it was.
clock.advance(1_000)
replayed = replay(store, PROJECT, dead.id, clock)
assert replayed.id != dead.id and replayed.replay_ordinal == 1
assert replayed.status.value == "pending"
assert store.get_delivery(PROJECT, dead.id).status.value == "dead_letter"

back_up = Receiver(200)
(settled,) = deliverer_for(store, back_up).tick(clock.now_ms())
assert settled.id == replayed.id and settled.status.value == "delivered"
print(f"\nreplayed {dead.id}")
print(f"      -> {settled.id} {settled.status.value} (replay_ordinal="
      f"{replayed.replay_ordinal}); the dead row is still dead, on purpose")

print("\nfinal delivery log: " + ", ".join(
    f"{row.id[:14]}…={row.status.value}" for row in store.deliveries(PROJECT)))

# Over HTTP the same three operations are routes — register, list the dead
# letter queue, replay one — so an operator does not need database access:
#     POST /webhooks/endpoints
#     GET  /webhooks/dead_letter
#     POST /webhooks/deliveries/{id}/replay
print("\nOK — signed, retried on a pinned schedule, dead-lettered, replayable.")
