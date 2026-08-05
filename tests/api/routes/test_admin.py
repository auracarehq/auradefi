"""api/routes/admin.py — the public coverage route and webhook admin.

Offline throughout. ``GET /coverage`` is the only route in the whole
surface with no credential; the test that it carries NO ``X-RateLimit-*``
header is really a test that it authenticates nobody, since the middleware
emits on ``request.state.project_id`` alone.

The webhook tests are the rule #8 tests: a project self-serves its own
endpoint and gets a signing secret back immediately and exactly once —
no allowlist, no support ticket, no source-IP check.
"""

from __future__ import annotations

import dataclasses
import inspect
import json

import pytest
from fastapi.testclient import TestClient

from auradefi.api.app import create_app
from auradefi.api.deps import Deps, WebhookSink
from auradefi.api.wire import coverage_payload
from auradefi.chains.registry import ChainRegistry
from auradefi.clock import FrozenClock
from auradefi.errors import NotFoundError
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.keys import ApiKeyStore
from auradefi.tenancy.models import Environment, Scope
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import RevocationSet
from auradefi.webhooks.deliver import WebhookStore
from auradefi.webhooks.models import (
    Delivery,
    DeliveryStatus,
    Endpoint,
    Event,
    EventName,
)

NOW = 1_754_000_000_000
ALL_SCOPES = (Scope.USERS_ADMIN, Scope.ACCOUNTS_READ, Scope.ACCOUNTS_WRITE)
HOOK_URL = "https://hooks.example.com/inbox"
OTHER_URL = "https://hooks.example.com/second"
SEED_CHAINS = (
    "bip122:000000000019d6689c085ae165831e93",
    "eip155:1",
    "eip155:137",
    "eip155:8453",
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
)
SEVEN_EVENTS = (
    "connection.created",
    "connection.deleted",
    "holdings.updated",
    "reorg.detected",
    "sync.failed",
    "sync.started",
    "transactions.available",
)
DELIVERY_KEYS = {
    "id",
    "endpoint_id",
    "event_id",
    "event_name",
    "status",
    "attempts",
    "created_at_ms",
    "next_attempt_at_ms",
    "delivered_at_ms",
    "last_status_code",
    "last_error",
    "replay_ordinal",
}


def _build(capabilities=None, webhooks=None, **overrides):
    """(deps, project, clock, vault) over the real Phase 0-7 collaborators.

    ``webhooks`` defaults to the shipped store; pass one to drive the routes
    over a sink written from ``api.deps.WebhookSink`` alone.
    """
    clock = FrozenClock(NOW)
    tenancy = TenancyStore()
    org = tenancy.create_organisation("acme", clock)
    project = tenancy.create_project(org.id, "main", Environment.TEST, clock)
    vault = {project.id: project.signing_secret}
    deps = Deps(
        tenancy=tenancy,
        keys=ApiKeyStore(),
        quota=QuotaCounter(QuotaLimits(50, 500, 5_000), clock),
        audit=AuditLog(),
        revocations=RevocationSet(),
        ledger=MemoryLedger(),
        webhooks=webhooks if webhooks is not None else WebhookStore(),
        chains=ChainRegistry(),
        clock=clock,
        signing_secret_for=vault.get,
        capabilities=capabilities if capabilities is not None else {},
        **overrides,
    )
    return deps, project, clock, vault


def _second_project(deps: Deps, vault: dict[str, str]):
    org = deps.tenancy.create_organisation("other", deps.clock)
    project = deps.tenancy.create_project(org.id, "other", Environment.TEST, deps.clock)
    vault[project.id] = project.signing_secret
    return project


def _bearer(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


@pytest.fixture
def wired():
    deps, project, clock, vault = _build()
    _record, plaintext = deps.keys.issue(project.id, Environment.TEST, ALL_SCOPES, clock)
    return TestClient(create_app(deps)), deps, project, clock, vault, plaintext


# --------------------------------------------------------------------------
# GET /coverage — the single public route


def test_coverage_needs_no_credential_and_carries_no_quota_headers():
    deps, _project, _clock, _vault = _build()
    response = TestClient(create_app(deps)).get("/coverage")

    assert response.status_code == 200
    assert not [name for name in response.headers if name.lower().startswith("x-ratelimit")]
    assert [chain["chain_id"] for chain in response.json()["chains"]] == list(SEED_CHAINS)


def test_coverage_is_byte_equal_to_the_wire_projection():
    capabilities = {
        "eip155:1": frozenset({"balances", "transactions", "prices"}),
        "bip122:000000000019d6689c085ae165831e93": frozenset({"xpub", "balances"}),
    }
    deps, _project, _clock, _vault = _build(capabilities=capabilities)
    expected = coverage_payload(deps.chains.chains(), capabilities, NOW)

    response = TestClient(create_app(deps)).get("/coverage")

    assert response.json() == expected
    assert response.content == json.dumps(
        expected, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def test_coverage_flags_come_only_from_the_bound_capabilities():
    deps, _project, _clock, _vault = _build(
        capabilities={"eip155:1": frozenset({"balances"})}
    )
    rows = {
        chain["chain_id"]: chain["capabilities"]
        for chain in TestClient(create_app(deps)).get("/coverage").json()["chains"]
    }
    assert rows["eip155:1"] == {
        "balances": True,
        "transactions": False,
        "positions": False,
        "prices": False,
        "xpub": False,
    }
    assert set(rows["eip155:137"].values()) == {False}, "unbound chains under-claim"


# --------------------------------------------------------------------------
# POST /webhooks/endpoints


def _register(client, plaintext, url=HOOK_URL, events=None):
    body: dict[str, object] = {"url": url}
    if events is not None:
        body["events"] = events
    return client.post("/webhooks/endpoints", json=body, headers=_bearer(plaintext))


def test_registration_returns_the_secret_exactly_once(wired):
    client, _deps, _project, _clock, _vault, plaintext = wired
    response = _register(client, plaintext)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "url", "events", "created_at_ms", "secret"}
    assert body["id"].startswith("whe_")
    assert body["url"] == HOOK_URL
    assert body["events"] == []
    assert body["created_at_ms"] == NOW
    assert len(body["secret"]) == 64
    assert set(body["secret"]) <= set("0123456789abcdef")

    listed = client.get("/webhooks/endpoints", headers=_bearer(plaintext)).json()
    assert set(listed) == {"endpoints", "count"}
    assert listed["count"] == 1
    assert set(listed["endpoints"][0]) == {"id", "url", "events", "created_at_ms"}
    assert "secret" not in json.dumps(listed)


def test_a_subscription_filter_is_echoed_sorted(wired):
    client, _deps, _project, _clock, _vault, plaintext = wired
    response = _register(
        client, plaintext, events=["sync.failed", "connection.created"]
    )
    assert response.status_code == 201
    assert response.json()["events"] == ["connection.created", "sync.failed"]


def test_re_registering_the_same_url_is_409_with_the_existing_id(wired):
    client, _deps, _project, _clock, _vault, plaintext = wired
    first = _register(client, plaintext).json()
    response = _register(client, plaintext)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["type"] == "ConflictError"
    assert error["existing_id"] == first["id"]
    assert error["existing_id"].startswith("whe_")
    assert "existing_connection_id" not in error, "only conn_ ids get Vezgo's alias"


@pytest.mark.parametrize("url", ["ftp://x", "https://", "not a url", "https://h.t:443/x"])
def test_a_structurally_invalid_url_is_422(wired, url):
    client, deps, project, _clock, _vault, plaintext = wired
    response = _register(client, plaintext, url=url)
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "ValidationError"
    assert deps.webhooks.endpoints(project.id) == ()


def test_localhost_registers_fine_because_there_is_no_allowlist(wired):
    client, _deps, _project, _clock, _vault, plaintext = wired
    assert _register(client, plaintext, url="http://127.0.0.1:9000/hook").status_code == 201


def test_an_unknown_event_name_is_422_naming_the_seven(wired):
    client, deps, project, _clock, _vault, plaintext = wired
    response = _register(client, plaintext, events=["connection.crated"])

    assert response.status_code == 422
    message = response.json()["error"]["message"]
    assert response.json()["error"]["type"] == "ValidationError"
    for name in SEVEN_EVENTS:
        assert name in message, f"the 422 must name {name}"
    assert deps.webhooks.endpoints(project.id) == ()


def test_endpoints_are_project_scoped(wired):
    client, deps, _project, clock, vault, plaintext = wired
    other = _second_project(deps, vault)
    _record, other_plaintext = deps.keys.issue(
        other.id, Environment.TEST, ALL_SCOPES, clock
    )
    _register(client, plaintext)

    listed = client.get("/webhooks/endpoints", headers=_bearer(other_plaintext)).json()
    assert listed == {"endpoints": [], "count": 0}


def test_webhook_admin_requires_users_admin():
    deps, project, clock, _vault = _build()
    _record, plaintext = deps.keys.issue(
        project.id, Environment.TEST, (Scope.ACCOUNTS_READ,), clock
    )
    client = TestClient(create_app(deps))
    assert _register(client, plaintext).status_code == 403
    assert client.get("/webhooks/endpoints", headers=_bearer(plaintext)).status_code == 403


# --------------------------------------------------------------------------
# deliveries, dead letter, replay


def _dead_lettered(deps, project, clock):
    """Emit one event and burn its six attempts against a 500."""
    endpoint, _secret = deps.webhooks.register_endpoint(project.id, HOOK_URL, (), clock)
    delivery = deps.webhooks.emit(
        project.id, EventName.SYNC_STARTED, {"connection_id": "conn_1"}, clock
    )[0]
    for attempt in range(6):
        deps.webhooks.record_attempt(
            project.id, delivery.id, now_ms=NOW + attempt, status_code=500, error=None
        )
    return endpoint, delivery


def test_deliveries_are_listed_in_creation_order_with_every_key(wired):
    client, deps, project, clock, _vault, plaintext = wired
    endpoint, delivery = _dead_lettered(deps, project, clock)

    body = client.get("/webhooks/deliveries", headers=_bearer(plaintext)).json()

    assert set(body) == {"deliveries", "count"}
    assert body["count"] == 1
    row = body["deliveries"][0]
    assert set(row) == DELIVERY_KEYS
    assert row["id"] == delivery.id
    assert row["endpoint_id"] == endpoint.id
    assert row["event_id"] == delivery.event_id
    assert row["event_name"] == "sync.started"
    assert row["status"] == "dead_letter"
    assert row["attempts"] == 6
    assert row["created_at_ms"] == NOW
    assert row["next_attempt_at_ms"] is None
    assert row["delivered_at_ms"] is None
    assert row["last_status_code"] == 500
    assert row["last_error"] is None
    assert row["replay_ordinal"] == 0


def test_dead_letter_lists_only_dead_lettered_rows(wired):
    client, deps, project, clock, _vault, plaintext = wired
    _endpoint, dead = _dead_lettered(deps, project, clock)
    deps.webhooks.emit(project.id, EventName.SYNC_FAILED, {"why": "boom"}, clock)

    everything = client.get("/webhooks/deliveries", headers=_bearer(plaintext)).json()
    letters = client.get("/webhooks/dead_letter", headers=_bearer(plaintext)).json()

    assert everything["count"] == 2
    assert letters["count"] == 1
    assert [row["id"] for row in letters["deliveries"]] == [dead.id]
    assert set(letters["deliveries"][0]) == DELIVERY_KEYS


def test_deliveries_can_be_filtered_by_status(wired):
    client, deps, project, clock, _vault, plaintext = wired
    _endpoint, dead = _dead_lettered(deps, project, clock)
    deps.webhooks.emit(project.id, EventName.SYNC_FAILED, {"why": "boom"}, clock)

    pending = client.get(
        "/webhooks/deliveries?status=pending", headers=_bearer(plaintext)
    ).json()
    assert pending["count"] == 1
    assert pending["deliveries"][0]["status"] == "pending"
    assert pending["deliveries"][0]["id"] != dead.id


def test_an_unknown_status_filter_is_422(wired):
    client, _deps, _project, _clock, _vault, plaintext = wired
    response = client.get(
        "/webhooks/deliveries?status=deliverd", headers=_bearer(plaintext)
    )
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "ValidationError"


def test_replay_answers_202_with_a_new_delivery(wired):
    client, deps, project, clock, _vault, plaintext = wired
    _endpoint, dead = _dead_lettered(deps, project, clock)

    response = client.post(
        f"/webhooks/deliveries/{dead.id}/replay", headers=_bearer(plaintext)
    )

    assert response.status_code == 202
    body = response.json()
    assert set(body) == DELIVERY_KEYS
    assert body["id"].startswith("dlv_")
    assert body["id"] != dead.id
    assert body["replay_ordinal"] == 1
    assert body["status"] == "pending"
    assert body["attempts"] == 0
    assert body["created_at_ms"] == NOW
    assert body["next_attempt_at_ms"] == NOW
    assert body["event_id"] == dead.event_id

    original = deps.webhooks.get_delivery(project.id, dead.id)
    assert original.status.value == "dead_letter"
    assert original.attempts == 6, "the forensic row is never mutated"


def test_replaying_an_unknown_or_foreign_delivery_is_404(wired):
    client, deps, project, clock, vault, plaintext = wired
    _endpoint, dead = _dead_lettered(deps, project, clock)
    other = _second_project(deps, vault)
    _record, other_plaintext = deps.keys.issue(
        other.id, Environment.TEST, ALL_SCOPES, clock
    )

    unknown = client.post(
        "/webhooks/deliveries/dlv_0000000000000000/replay", headers=_bearer(plaintext)
    )
    foreign = client.post(
        f"/webhooks/deliveries/{dead.id}/replay", headers=_bearer(other_plaintext)
    )

    assert unknown.status_code == foreign.status_code == 404
    assert unknown.json()["error"]["type"] == foreign.json()["error"]["type"]


def test_there_is_no_endpoint_that_runs_the_deliverer():
    """SPEC §8: the host owns scheduling. Nothing here drains deliveries."""
    deps, _project, _clock, _vault = _build()
    paths = create_app(deps).openapi()["paths"]
    writes = sorted(path for path, operations in paths.items() if "post" in operations)
    assert writes == [
        "/auth/revoke",
        "/auth/token",
        "/connections",
        "/webhooks/deliveries/{delivery_id}/replay",
        "/webhooks/endpoints",
    ]


# --------------------------------------------------------------------------
# RELEASE_0.1.1 §5 Wave C #27 / #28 — a sink written from the Protocol alone
#
# Both defects are one class: the route requires MORE than
# ``api.deps.WebhookSink`` promises, so every host-supplied sink gets an
# unhandled 500 while the shipped store works by accident (#27,
# ``create_replay``, declared nowhere; #28, ``register_endpoint`` typed as
# returning one object where the route unpacks a pair). Neither existing test
# catches it, because every existing test binds the shipped store.
#
# The instrument below is the one that does: a sink built from the Protocol
# declaration and nothing else, wrapped so it can expose no more than the
# Protocol promises, driving EVERY webhook route.

#: This sink's fixed ids — invented here, not derived from webhooks/models.py,
#: so the expected bodies below are literals a reader can check by eye.
SINK_ENDPOINT_ID = "whe_minimal000000001"
SINK_EVENT_ID = "evt_minimal000000001"
SINK_ORIGINAL_ID = "dlv_minimal000000000"
SINK_REPLAY_ID = "dlv_minimal000000001"
SINK_SECRET = "5f" * 32


class _MinimalSink:
    """A webhook sink written from ``api.deps.WebhookSink`` and nothing else.

    Deliberately NOT derived from ``auradefi.webhooks.deliver.WebhookStore``:
    this is what a HOST writes when it reads the Protocol — flat dicts, fixed
    ids, no retry schedule, no signing, no entropy. Every method here exists
    because the seam declares it, and nothing else exists at all.
    """

    def __init__(self) -> None:
        self.endpoint_rows: dict[str, list[Endpoint]] = {}
        self.event_rows: dict[str, dict[str, Event]] = {}
        self.delivery_rows: dict[str, dict[str, Delivery]] = {}

    def register_endpoint(self, project_id, url, events, clock):  # noqa: ANN001
        endpoint = Endpoint(
            id=SINK_ENDPOINT_ID,
            project_id=project_id,
            url=url,
            events=frozenset(EventName(name) for name in events),
            created_at_ms=clock.now_ms(),
        )
        self.endpoint_rows.setdefault(project_id, []).append(endpoint)
        return endpoint, SINK_SECRET

    def endpoints(self, project_id):  # noqa: ANN001
        return tuple(self.endpoint_rows.get(project_id, ()))

    def emit(self, project_id, name, data, clock):  # noqa: ANN001
        event = Event(
            id=SINK_EVENT_ID,
            project_id=project_id,
            name=EventName(name),
            data=dict(data),
            created_at_ms=clock.now_ms(),
        )
        self.event_rows.setdefault(project_id, {})[event.id] = event
        delivery = Delivery(
            id=SINK_ORIGINAL_ID,
            project_id=project_id,
            endpoint_id=SINK_ENDPOINT_ID,
            event_id=event.id,
            replay_ordinal=0,
            status=DeliveryStatus.PENDING,
            attempts=0,
            created_at_ms=clock.now_ms(),
            next_attempt_at_ms=clock.now_ms(),
        )
        self.delivery_rows.setdefault(project_id, {})[delivery.id] = delivery
        return (delivery,)

    def deliveries(self, project_id):  # noqa: ANN001
        return tuple(self.delivery_rows.get(project_id, {}).values())

    def dead_letter(self, project_id):  # noqa: ANN001
        return tuple(
            delivery
            for delivery in self.deliveries(project_id)
            if delivery.status is DeliveryStatus.DEAD_LETTER
        )

    def get_event(self, project_id, event_id):  # noqa: ANN001
        event = self.event_rows.get(project_id, {}).get(event_id)
        if event is None:
            raise NotFoundError(f"webhook event not found: {event_id}")
        return event

    def _row(self, project_id, delivery_id):  # noqa: ANN001
        # Private helper, not a `get_delivery` member: the seam does not
        # promise one (the shipped store calls its own, internally), and
        # this sink implements the DECLARED surface and nothing else.
        delivery = self.delivery_rows.get(project_id, {}).get(delivery_id)
        if delivery is None:
            raise NotFoundError(f"webhook delivery not found: {delivery_id}")
        return delivery

    def create_replay(self, project_id, delivery_id, clock):  # noqa: ANN001
        original = self._row(project_id, delivery_id)
        replayed = dataclasses.replace(
            original,
            id=SINK_REPLAY_ID,
            replay_ordinal=original.replay_ordinal + 1,
            status=DeliveryStatus.PENDING,
            attempts=0,
            created_at_ms=clock.now_ms(),
            next_attempt_at_ms=clock.now_ms(),
            delivered_at_ms=None,
            last_status_code=None,
            last_error=None,
        )
        self.delivery_rows[project_id][replayed.id] = replayed
        return replayed


def _declared_members(protocol: type) -> frozenset[str]:
    """The member names ``protocol`` promises, as ``isinstance`` sees them."""
    declared = set(getattr(protocol, "__protocol_attrs__", ()) or ())
    if not declared:
        declared = {
            name
            for name in vars(protocol)
            if not name.startswith("_") and callable(getattr(protocol, name, None))
        }
    return frozenset(declared)


class _DeclaredSurfaceOnly:
    """``deps.webhooks`` as a HOST's deployment sees it.

    An attribute WebhookSink does not declare raises ``AttributeError`` — the
    host never wrote it, because the Protocol never asked for it — and every
    call is first bound against the Protocol's DECLARED signature, so a route
    passing arguments the seam does not promise is recorded rather than
    silently absorbed by the shipped store's wider signature.
    """

    def __init__(self, inner: object) -> None:
        self.__dict__["_inner"] = inner
        self.__dict__["_declared"] = _declared_members(WebhookSink)
        self.__dict__["_refused"] = []
        self.__dict__["_mismatched"] = []
        self.__dict__["_used"] = []

    def __getattr__(self, name: str) -> object:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self.__dict__["_declared"]:
            self.__dict__["_refused"].append(name)
            raise AttributeError(
                f"api.deps.WebhookSink does not declare {name!r}, so a sink "
                "written from the Protocol does not have it"
            )
        member = getattr(self.__dict__["_inner"], name)
        promised = inspect.signature(getattr(WebhookSink, name))

        def _as_declared(*args: object, **kwargs: object) -> object:
            try:
                promised.bind(self, *args, **kwargs)
            except TypeError as exc:
                self.__dict__["_mismatched"].append(f"{name}{promised}: {exc}")
            self.__dict__["_used"].append(name)
            return member(*args, **kwargs)

        return _as_declared


def _seed_dead_letter(sink: _MinimalSink, project_id: str) -> None:
    """One DEAD_LETTER delivery and its event, placed by hand.

    Written directly rather than driven through a ``Deliverer``: the point of
    this fixture is that NOTHING here comes from ``webhooks/``'s store.
    """
    sink.event_rows.setdefault(project_id, {})[SINK_EVENT_ID] = Event(
        id=SINK_EVENT_ID,
        project_id=project_id,
        name=EventName.SYNC_STARTED,
        data={"connection_id": "conn_1"},
        created_at_ms=NOW,
    )
    sink.delivery_rows.setdefault(project_id, {})[SINK_ORIGINAL_ID] = Delivery(
        id=SINK_ORIGINAL_ID,
        project_id=project_id,
        endpoint_id=SINK_ENDPOINT_ID,
        event_id=SINK_EVENT_ID,
        replay_ordinal=0,
        status=DeliveryStatus.DEAD_LETTER,
        attempts=6,
        created_at_ms=NOW,
        next_attempt_at_ms=None,
        last_status_code=500,
    )


#: What ``_delivery_wire`` must project for the seeded dead-lettered row.
DEAD_ROW = {
    "id": SINK_ORIGINAL_ID,
    "endpoint_id": SINK_ENDPOINT_ID,
    "event_id": SINK_EVENT_ID,
    "event_name": "sync.started",
    "status": "dead_letter",
    "attempts": 6,
    "created_at_ms": NOW,
    "next_attempt_at_ms": None,
    "delivered_at_ms": None,
    "last_status_code": 500,
    "last_error": None,
    "replay_ordinal": 0,
}

#: And for the row the replay route creates from it.
REPLAY_ROW = {
    "id": SINK_REPLAY_ID,
    "endpoint_id": SINK_ENDPOINT_ID,
    "event_id": SINK_EVENT_ID,
    "event_name": "sync.started",
    "status": "pending",
    "attempts": 0,
    "created_at_ms": NOW,
    "next_attempt_at_ms": NOW,
    "delivered_at_ms": None,
    "last_status_code": None,
    "last_error": None,
    "replay_ordinal": 1,
}


@pytest.fixture
def protocol_only():
    """(client, project, plaintext, sink, facade) over a Protocol-only sink.

    ``raise_server_exceptions=False``: an undeclared attribute must arrive as
    the 500 RESPONSE a host's caller would see, so the regression reads as a
    failed assertion instead of a traceback out of the ASGI app.
    """
    sink = _MinimalSink()
    facade = _DeclaredSurfaceOnly(sink)
    deps, project, clock, _vault = _build(webhooks=facade)
    _record, plaintext = deps.keys.issue(project.id, Environment.TEST, ALL_SCOPES, clock)
    client = TestClient(create_app(deps), raise_server_exceptions=False)
    return client, project, plaintext, sink, facade


# pins: every webhook route completes over a sink that implements ONLY what
#       WebhookSink declares — no route reaches for an undeclared attribute
#       and no route calls a declared one outside its declared signature.
def test_every_webhook_route_drives_a_sink_written_only_from_the_protocol(protocol_only):
    client, project, plaintext, sink, facade = protocol_only
    _seed_dead_letter(sink, project.id)

    registered = _register(client, plaintext)
    listed = client.get("/webhooks/endpoints", headers=_bearer(plaintext))
    deliveries = client.get("/webhooks/deliveries", headers=_bearer(plaintext))
    filtered = client.get(
        "/webhooks/deliveries?status=dead_letter", headers=_bearer(plaintext)
    )
    letters = client.get("/webhooks/dead_letter", headers=_bearer(plaintext))
    replayed = client.post(
        f"/webhooks/deliveries/{SINK_ORIGINAL_ID}/replay", headers=_bearer(plaintext)
    )

    assert facade._refused == [], (
        "a webhook route requires sink attributes WebhookSink does not "
        f"declare: {sorted(set(facade._refused))} — every host-supplied sink "
        "gets an unhandled 500 there"
    )
    assert facade._mismatched == [], (
        "a webhook route called a declared member outside its declared "
        f"signature: {facade._mismatched}"
    )
    assert facade._used, "the Protocol-only sink was never used — this proves nothing"

    statuses = [
        registered.status_code,
        listed.status_code,
        deliveries.status_code,
        filtered.status_code,
        letters.status_code,
        replayed.status_code,
    ]
    assert statuses == [201, 200, 200, 200, 200, 202], (
        f"{statuses} over a Protocol-only sink; last body: {replayed.text[:300]}"
    )
    assert registered.json() == {
        "id": SINK_ENDPOINT_ID,
        "url": HOOK_URL,
        "events": [],
        "created_at_ms": NOW,
        "secret": SINK_SECRET,
    }
    assert listed.json() == {
        "endpoints": [
            {
                "id": SINK_ENDPOINT_ID,
                "url": HOOK_URL,
                "events": [],
                "created_at_ms": NOW,
            }
        ],
        "count": 1,
    }
    assert deliveries.json() == {"deliveries": [DEAD_ROW], "count": 1}
    assert filtered.json() == {"deliveries": [DEAD_ROW], "count": 1}
    assert letters.json() == {"deliveries": [DEAD_ROW], "count": 1}
    assert replayed.json() == REPLAY_ROW
    assert sink.delivery_rows[project.id][SINK_ORIGINAL_ID].attempts == 6, (
        "the forensic row is never mutated"
    )


# pins: widening WebhookSink never outruns the shipped store — every declared
#       member exists on webhooks.deliver.WebhookStore and accepts the
#       parameters the Protocol declares.
def test_the_shipped_webhook_store_satisfies_every_declared_member():
    store = WebhookStore()
    declared = _declared_members(WebhookSink)

    assert declared, "api.deps.WebhookSink declares no members at all"
    assert isinstance(store, WebhookSink), (
        "the shipped store no longer satisfies WebhookSink; it is missing "
        f"{sorted(name for name in declared if not hasattr(store, name))}"
    )
    incompatible = []
    for name in sorted(declared):
        promised = inspect.signature(getattr(WebhookSink, name))
        actual = inspect.signature(getattr(store, name))
        arguments = [object()] * (len(promised.parameters) - 1)  # drop `self`
        try:
            actual.bind(*arguments)
        except TypeError as exc:
            incompatible.append(
                f"{name}: WebhookSink promises {promised}, the store has {actual} ({exc})"
            )
    assert not incompatible, (
        "the widened Protocol promises call shapes the shipped store cannot "
        "honour:\n" + "\n".join(incompatible)
    )


# pins: the minimal sink really is limited to the declaration — it satisfies
#       WebhookSink structurally, and its own surface adds nothing the seam
#       does not promise.
def test_the_minimal_sink_is_exactly_the_declared_surface():
    sink = _MinimalSink()
    declared = _declared_members(WebhookSink)
    implemented = {
        name
        for name in vars(_MinimalSink)
        if not name.startswith("_") and callable(getattr(_MinimalSink, name))
    }

    assert isinstance(sink, WebhookSink)
    assert implemented - declared == set(), (
        f"the sink implements {sorted(implemented - declared)}, which "
        "WebhookSink does not declare — a host reading the Protocol would "
        "never have written it, so the route driving it is untested for hosts"
    )
