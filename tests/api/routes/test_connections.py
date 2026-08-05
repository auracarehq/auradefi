"""api/routes/connections.py. Create, list, read, and the optional delete.

Offline throughout; the webhook assertions run against the REAL
``WebhookStore``, so "emits exactly one connection.created" is checked
by reading the delivery the store fanned out, not by a spy.

Two invariants get the most attention here because they are where
aggregators leak: a user-token response never echoes ``project_id``, and
another user's connection id is a 404 that reads exactly like an id that
never existed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from auradefi.api.app import create_app
from auradefi.api.deps import Deps
from auradefi.chains.registry import ChainRegistry
from auradefi.clock import FrozenClock
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.keys import ApiKeyStore
from auradefi.tenancy.models import Environment, Scope, end_user_id
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import RevocationSet
from auradefi.webhooks.deliver import WebhookStore

NOW = 1_754_000_000_000
ALL_SCOPES = (Scope.USERS_ADMIN, Scope.ACCOUNTS_READ, Scope.ACCOUNTS_WRITE)
ADDRESS = "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01"
HOOK_URL = "https://hooks.example.com/inbox"


class _Deleter:
    """The injected connection deleter; records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, project_id: str, connection_id: str) -> None:
        self.calls.append((project_id, connection_id))


def _build(**overrides):
    """(deps, project, clock, vault) over the real Phase 0-7 collaborators."""
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
        webhooks=WebhookStore(),
        chains=ChainRegistry(),
        clock=clock,
        signing_secret_for=vault.get,
        **overrides,
    )
    return deps, project, clock, vault


def _bearer(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


def _token(client, plaintext, external_user_id="u-1", scopes=None):
    body = {"external_user_id": external_user_id}
    if scopes is not None:
        body["scopes"] = scopes
    response = client.post("/auth/token", json=body, headers=_bearer(plaintext))
    assert response.status_code == 200, response.text
    return response.json()["token"]


@pytest.fixture
def wired():
    deps, project, clock, vault = _build()
    _record, plaintext = deps.keys.issue(project.id, Environment.TEST, ALL_SCOPES, clock)
    client = TestClient(create_app(deps))
    return client, deps, project, clock, plaintext


def _create(client, token, descriptor=ADDRESS, kind="address"):
    return client.post(
        "/connections",
        json={"kind": kind, "descriptor": descriptor},
        headers=_bearer(token),
    )


# --------------------------------------------------------------------------
# POST /connections


def test_create_answers_201_with_exactly_five_keys(wired):
    client, _deps, project, _clock, plaintext = wired
    token = _token(client, plaintext)
    response = _create(client, token)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "end_user_id", "kind", "descriptor", "created_at_ms"}
    assert "project_id" not in body, "a user-token caller never learns the tenant id"
    assert body["id"].startswith("conn_")
    assert body["end_user_id"] == end_user_id(project.id, "u-1")
    assert body["kind"] == "address"
    assert body["descriptor"] == ADDRESS.lower(), "EVM descriptors are stored normalized"
    assert body["created_at_ms"] == NOW


def test_create_emits_exactly_one_connection_created(wired):
    client, deps, project, clock, plaintext = wired
    deps.webhooks.register_endpoint(project.id, HOOK_URL, (), clock)
    token = _token(client, plaintext)
    created = _create(client, token).json()

    deliveries = deps.webhooks.deliveries(project.id)
    assert len(deliveries) == 1
    event = deps.webhooks.get_event(project.id, deliveries[0].event_id)
    assert str(event.name) == "connection.created"
    assert set(event.data) == {"connection_id", "descriptor", "end_user_id", "kind"}
    assert event.data["connection_id"] == created["id"]
    assert event.data["descriptor"] == ADDRESS.lower()
    assert event.data["end_user_id"] == created["end_user_id"]
    assert event.data["kind"] == "address"


def test_reposting_the_same_descriptor_in_another_case_is_409(wired):
    client, deps, project, clock, plaintext = wired
    deps.webhooks.register_endpoint(project.id, HOOK_URL, (), clock)
    token = _token(client, plaintext)
    first = _create(client, token).json()

    response = _create(client, token, descriptor=ADDRESS.upper().replace("0X", "0x"))

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["type"] == "ConflictError"
    assert error["existing_id"] == first["id"]
    assert error["existing_connection_id"] == first["id"], "SPEC §7.1, Vezgo verbatim"
    assert len(deps.webhooks.deliveries(project.id)) == 1, "a refused create emits nothing"


def test_create_requires_accounts_write(wired):
    client, _deps, _project, _clock, plaintext = wired
    token = _token(client, plaintext, scopes=["accounts:read"])
    response = _create(client, token)
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "ScopeError"


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "wallet", "descriptor": ADDRESS},
        {"kind": "address"},
        {"kind": "address", "descriptor": ADDRESS, "project_id": "proj_smuggled"},
        {"kind": "address", "descriptor": ADDRESS, "end_user_id": "usr_other"},
    ],
)
def test_a_bad_body_is_422(wired, body):
    client, _deps, _project, _clock, plaintext = wired
    token = _token(client, plaintext)
    response = client.post("/connections", json=body, headers=_bearer(token))
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "ValidationError"


def test_an_xpub_descriptor_keeps_its_case(wired):
    client, _deps, _project, _clock, plaintext = wired
    token = _token(client, plaintext)
    xpub = "xpub6CUGRUonZSQ4TWtTMmzXdrXDtypWKiKrhko4egpiMZbpiaQL2jkwSB1icqYh2cfDfVxdx4df189oLKnC5fSwqPfgyP3hooxujYzAu3fDVmz"
    response = _create(client, token, descriptor=xpub, kind="xpub")
    assert response.status_code == 201
    assert response.json()["descriptor"] == xpub, "base58 case is significant"


# --------------------------------------------------------------------------
# GET /connections and GET /connections/{id}


def test_list_and_read_are_scoped_to_the_calling_user(wired):
    client, _deps, _project, _clock, plaintext = wired
    alice = _token(client, plaintext, external_user_id="alice")
    bob = _token(client, plaintext, external_user_id="bob")
    hers = _create(client, alice).json()

    mine = client.get("/connections", headers=_bearer(alice)).json()
    assert set(mine) == {"connections", "count"}
    assert mine["count"] == 1
    assert mine["connections"][0] == hers

    theirs = client.get("/connections", headers=_bearer(bob)).json()
    assert theirs == {"connections": [], "count": 0}

    assert client.get(f"/connections/{hers['id']}", headers=_bearer(alice)).json() == hers
    stolen = client.get(f"/connections/{hers['id']}", headers=_bearer(bob))
    assert stolen.status_code == 404
    assert stolen.json()["error"]["type"] == "NotFoundError"


def test_a_missing_connection_and_another_users_are_indistinguishable(wired):
    client, _deps, _project, _clock, plaintext = wired
    alice = _token(client, plaintext, external_user_id="alice")
    bob = _token(client, plaintext, external_user_id="bob")
    hers = _create(client, alice).json()

    stolen = client.get(f"/connections/{hers['id']}", headers=_bearer(bob))
    absent = client.get("/connections/conn_0000000000000000", headers=_bearer(bob))
    assert stolen.status_code == absent.status_code == 404
    assert stolen.json()["error"]["type"] == absent.json()["error"]["type"]


def test_another_tenants_connection_id_is_also_404():
    deps, project, clock, vault = _build()
    org = deps.tenancy.create_organisation("other", clock)
    other = deps.tenancy.create_project(org.id, "other", Environment.TEST, clock)
    vault[other.id] = other.signing_secret
    _mine, mine_plaintext = deps.keys.issue(project.id, Environment.TEST, ALL_SCOPES, clock)
    _theirs, theirs_plaintext = deps.keys.issue(other.id, Environment.TEST, ALL_SCOPES, clock)
    client = TestClient(create_app(deps))

    ours = _create(client, _token(client, mine_plaintext)).json()
    intruder = _token(client, theirs_plaintext, external_user_id="u-1")

    response = client.get(f"/connections/{ours['id']}", headers=_bearer(intruder))

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NotFoundError"
    assert client.get("/connections", headers=_bearer(intruder)).json()["count"] == 0


def test_read_requires_accounts_read(wired):
    client, _deps, _project, _clock, plaintext = wired
    token = _token(client, plaintext, scopes=["users:admin"])
    assert client.get("/connections", headers=_bearer(token)).status_code == 403


# --------------------------------------------------------------------------
# DELETE /connections/{id}: mounted iff the host bound a deleter


def test_delete_is_404_when_the_deployment_cannot_delete():
    deps, project, clock, _vault = _build()
    _record, plaintext = deps.keys.issue(project.id, Environment.TEST, ALL_SCOPES, clock)
    client = TestClient(create_app(deps))
    token = _token(client, plaintext)
    created = _create(client, token).json()

    response = client.delete(f"/connections/{created['id']}", headers=_bearer(token))

    assert response.status_code == 404, "never 405: that would confirm the capability"
    assert response.json()["error"]["type"] == "NotFoundError"
    assert client.get(f"/connections/{created['id']}", headers=_bearer(token)).status_code == 200


def test_delete_calls_the_injected_deleter_once_and_emits():
    deleter = _Deleter()
    deps, project, clock, _vault = _build(delete_connection=deleter)
    _record, plaintext = deps.keys.issue(project.id, Environment.TEST, ALL_SCOPES, clock)
    deps.webhooks.register_endpoint(project.id, HOOK_URL, (), clock)
    client = TestClient(create_app(deps))
    token = _token(client, plaintext)
    created = _create(client, token).json()

    response = client.delete(f"/connections/{created['id']}", headers=_bearer(token))

    assert response.status_code == 204
    assert response.content == b""
    assert deleter.calls == [(project.id, created["id"])]

    names = [
        str(deps.webhooks.get_event(project.id, delivery.event_id).name)
        for delivery in deps.webhooks.deliveries(project.id)
    ]
    assert names == ["connection.created", "connection.deleted"]
    deleted = deps.webhooks.get_event(
        project.id, deps.webhooks.deliveries(project.id)[1].event_id
    )
    assert set(deleted.data) == {"connection_id", "descriptor", "end_user_id", "kind"}
    assert deleted.data["connection_id"] == created["id"]


def test_delete_of_another_users_connection_is_404_and_deletes_nothing():
    deleter = _Deleter()
    deps, project, clock, _vault = _build(delete_connection=deleter)
    _record, plaintext = deps.keys.issue(project.id, Environment.TEST, ALL_SCOPES, clock)
    client = TestClient(create_app(deps))
    alice = _token(client, plaintext, external_user_id="alice")
    bob = _token(client, plaintext, external_user_id="bob")
    hers = _create(client, alice).json()

    response = client.delete(f"/connections/{hers['id']}", headers=_bearer(bob))

    assert response.status_code == 404
    assert deleter.calls == []


def test_delete_requires_accounts_write():
    deleter = _Deleter()
    deps, project, clock, _vault = _build(delete_connection=deleter)
    _record, plaintext = deps.keys.issue(project.id, Environment.TEST, ALL_SCOPES, clock)
    client = TestClient(create_app(deps))
    writer = _token(client, plaintext)
    created = _create(client, writer).json()
    reader = _token(client, plaintext, scopes=["accounts:read"])

    response = client.delete(f"/connections/{created['id']}", headers=_bearer(reader))

    assert response.status_code == 403
    assert deleter.calls == []
