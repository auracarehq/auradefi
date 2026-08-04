"""api/routes/auth.py — mint, revoke, and the two user reads.

Offline throughout: ``TestClient`` speaks ASGI in-process.

The pinned privilege rule is the point of this file. Vezgo's key can mint
any token; ours cannot mint a token more powerful than itself, and the
proof is that a key without ``accounts:write`` gets a 403 with an
UNCHANGED audit log — nothing minted, nothing recorded.

The nine-header block is arithmetic, not a fixture: FrozenClock's
1_754_000_000_000 is 2025-07-31T22:13:20Z, where the next UTC day
boundary (1754006400000) IS the next UTC month boundary.
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
from auradefi.tenancy.tokens import RevocationSet, verify_token
from auradefi.webhooks.deliver import WebhookStore

NOW = 1_754_000_000_000
ALL_SCOPES = (Scope.USERS_ADMIN, Scope.ACCOUNTS_READ, Scope.ACCOUNTS_WRITE)

# DECISIONS "Quota headers": nine, decimal strings, Reset a MS-EPOCH int.
NINE_HEADERS_AFTER_ONE_HIT = {
    "X-RateLimit-Limit-Second": "5",
    "X-RateLimit-Remaining-Second": "4",
    "X-RateLimit-Reset-Second": "1754000001000",
    "X-RateLimit-Limit-Day": "100",
    "X-RateLimit-Remaining-Day": "99",
    "X-RateLimit-Reset-Day": "1754006400000",
    "X-RateLimit-Limit-Month": "1000",
    "X-RateLimit-Remaining-Month": "999",
    "X-RateLimit-Reset-Month": "1754006400000",
}


def _build(limits: QuotaLimits | None = None):
    """(deps, project, clock, vault) over the real Phase 0-7 collaborators."""
    clock = FrozenClock(NOW)
    tenancy = TenancyStore()
    org = tenancy.create_organisation("acme", clock)
    project = tenancy.create_project(org.id, "main", Environment.TEST, clock)
    vault = {project.id: project.signing_secret}
    deps = Deps(
        tenancy=tenancy,
        keys=ApiKeyStore(),
        quota=QuotaCounter(limits or QuotaLimits(5, 100, 1_000), clock),
        audit=AuditLog(),
        revocations=RevocationSet(),
        ledger=MemoryLedger(),
        webhooks=WebhookStore(),
        chains=ChainRegistry(),
        clock=clock,
        signing_secret_for=vault.get,
    )
    return deps, project, clock, vault


def _second_project(deps: Deps, vault: dict[str, str], name: str = "other"):
    org = deps.tenancy.create_organisation(name, deps.clock)
    project = deps.tenancy.create_project(org.id, name, Environment.TEST, deps.clock)
    vault[project.id] = project.signing_secret
    return project


def _issue(deps: Deps, project, *scopes: Scope):
    return deps.keys.issue(project.id, Environment.TEST, scopes or ALL_SCOPES, deps.clock)


def _bearer(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


@pytest.fixture
def wired():
    deps, project, clock, vault = _build()
    record, plaintext = _issue(deps, project)
    return TestClient(create_app(deps)), deps, project, clock, vault, record, plaintext


# --------------------------------------------------------------------------
# POST /auth/token


def test_mint_answers_exactly_a_token_and_audits_once(wired):
    client, deps, project, clock, _vault, record, plaintext = wired
    response = client.post(
        "/auth/token",
        json={"external_user_id": "u-1"},
        headers={**_bearer(plaintext), "X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["token"], "SPEC §7.1: POST -> {token}. That is the whole interface"

    claims = verify_token(body["token"], signing_secret=project.signing_secret, clock=clock)
    assert claims.project_id == project.id
    assert claims.external_user_id == "u-1"
    assert claims.scopes == ("accounts:read", "accounts:write", "users:admin")
    assert claims.iat == NOW
    assert claims.exp == NOW + deps.token_ttl_ms

    entries = deps.audit.entries(project.id)
    assert len(entries) == 1
    assert entries[0].seq == 1
    assert entries[0].event == "token.minted"
    assert entries[0].key_id == record.id
    assert entries[0].external_user_id == "u-1"
    assert entries[0].ip == "203.0.113.7", "first X-Forwarded-For hop, not the whole chain"
    assert entries[0].at_ms == NOW


def test_mint_carries_all_nine_quota_headers(wired):
    client, _deps, _project, _clock, _vault, _record, plaintext = wired
    response = client.post(
        "/auth/token", json={"external_user_id": "u-1"}, headers=_bearer(plaintext)
    )
    assert response.status_code == 200
    for name, value in NINE_HEADERS_AFTER_ONE_HIT.items():
        assert response.headers[name] == value, name


def test_mint_without_forwarded_for_records_the_socket_peer(wired):
    client, deps, project, _clock, _vault, _record, plaintext = wired
    client.post("/auth/token", json={"external_user_id": "u-1"}, headers=_bearer(plaintext))
    assert deps.audit.entries(project.id)[0].ip == "testclient"


def test_mint_narrows_to_the_requested_subset(wired):
    client, _deps, project, clock, _vault, _record, plaintext = wired
    response = client.post(
        "/auth/token",
        json={"external_user_id": "u-1", "scopes": ["accounts:read"]},
        headers=_bearer(plaintext),
    )
    assert response.status_code == 200
    claims = verify_token(
        response.json()["token"], signing_secret=project.signing_secret, clock=clock
    )
    assert claims.scopes == ("accounts:read",)


def test_a_key_can_never_mint_a_token_more_powerful_than_itself():
    deps, project, _clock, _vault = _build()
    _record, plaintext = _issue(deps, project, Scope.USERS_ADMIN, Scope.ACCOUNTS_READ)
    client = TestClient(create_app(deps))

    response = client.post(
        "/auth/token",
        json={"external_user_id": "u-1", "scopes": ["accounts:write"]},
        headers=_bearer(plaintext),
    )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "ScopeError"
    assert deps.audit.entries(project.id) == (), "a refused mint is never audited"


def test_a_key_without_users_admin_cannot_mint():
    deps, project, _clock, _vault = _build()
    _record, plaintext = _issue(deps, project, Scope.ACCOUNTS_READ)
    response = TestClient(create_app(deps)).post(
        "/auth/token", json={"external_user_id": "u-1"}, headers=_bearer(plaintext)
    )
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "ScopeError"
    assert deps.audit.entries(project.id) == ()


def test_a_revoked_key_is_a_plain_401(wired):
    client, deps, project, clock, _vault, record, plaintext = wired
    deps.keys.revoke(record.id, clock)
    response = client.post(
        "/auth/token", json={"external_user_id": "u-1"}, headers=_bearer(plaintext)
    )
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "AuthError"
    assert deps.audit.entries(project.id) == ()


@pytest.mark.parametrize(
    "body",
    [
        {"external_user_id": "u-1", "project_id": "proj_smuggled"},
        {"external_user_id": "u-1", "ttl_ms": 10},
        {},
    ],
)
def test_an_unknown_or_missing_body_key_is_422(wired, body):
    client, _deps, _project, _clock, _vault, _record, plaintext = wired
    response = client.post("/auth/token", json=body, headers=_bearer(plaintext))
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "ValidationError"


def test_an_email_shaped_external_user_id_is_422(wired):
    client, deps, project, _clock, _vault, _record, plaintext = wired
    response = client.post(
        "/auth/token",
        json={"external_user_id": "user@example.dev"},
        headers=_bearer(plaintext),
    )
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "ValidationError"
    assert deps.audit.entries(project.id) == ()


def test_quota_is_consumed_before_anything_is_minted_or_audited():
    deps, project, _clock, _vault = _build(limits=QuotaLimits(0, 100, 1_000))
    _record, plaintext = _issue(deps, project)
    response = TestClient(create_app(deps)).post(
        "/auth/token", json={"external_user_id": "u-1"}, headers=_bearer(plaintext)
    )
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "QuotaExceededError"
    assert response.headers["Retry-After"] == "1"
    assert deps.audit.entries(project.id) == ()
    assert deps.tenancy.users(project.id) == (), "no user created on a refused mint"


# --------------------------------------------------------------------------
# POST /auth/revoke


def _mint(client, plaintext, external_user_id="u-1", scopes=None):
    body = {"external_user_id": external_user_id}
    if scopes is not None:
        body["scopes"] = scopes
    response = client.post("/auth/token", json=body, headers=_bearer(plaintext))
    assert response.status_code == 200, response.text
    return response.json()["token"]


def test_revoke_then_reuse_is_401_token_revoked(wired):
    client, _deps, _project, _clock, _vault, _record, plaintext = wired
    token = _mint(client, plaintext)
    assert client.get("/users/me", headers=_bearer(token)).status_code == 200

    revoked = client.post("/auth/revoke", json={"token": token}, headers=_bearer(plaintext))
    assert revoked.status_code == 200
    assert set(revoked.json()) == {"revoked", "jti"}
    assert revoked.json()["revoked"] is True

    reused = client.get("/users/me", headers=_bearer(token))
    assert reused.status_code == 401
    assert reused.json()["error"]["type"] == "TokenRevokedError"


def test_revoke_is_idempotent(wired):
    client, _deps, _project, _clock, _vault, _record, plaintext = wired
    token = _mint(client, plaintext)
    first = client.post("/auth/revoke", json={"token": token}, headers=_bearer(plaintext))
    second = client.post("/auth/revoke", json={"token": token}, headers=_bearer(plaintext))
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


def test_revoking_another_projects_token_is_404():
    deps, project, clock, vault = _build(limits=QuotaLimits(50, 500, 5_000))
    other = _second_project(deps, vault)
    _record, plaintext = _issue(deps, project)
    _other_record, other_plaintext = _issue(deps, other)
    client = TestClient(create_app(deps))

    foreign = _mint(client, other_plaintext, external_user_id="u-other")
    response = client.post(
        "/auth/revoke", json={"token": foreign}, headers=_bearer(plaintext)
    )

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NotFoundError"
    # And the foreign token still works for its own project.
    assert client.get("/users/me", headers=_bearer(foreign)).status_code == 200


def test_revoking_an_expired_token_is_401(wired):
    client, _deps, _project, clock, _vault, _record, plaintext = wired
    token = _mint(client, plaintext)
    clock.advance(600_001)
    response = client.post("/auth/revoke", json={"token": token}, headers=_bearer(plaintext))
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "TokenExpiredError"


def test_revoking_garbage_is_a_plain_401(wired):
    client, _deps, _project, _clock, _vault, _record, plaintext = wired
    response = client.post(
        "/auth/revoke", json={"token": "not.a.token"}, headers=_bearer(plaintext)
    )
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "AuthError"


def test_revoke_needs_users_admin():
    deps, project, _clock, _vault = _build()
    _record, plaintext = _issue(deps, project, Scope.ACCOUNTS_READ)
    response = TestClient(create_app(deps)).post(
        "/auth/revoke", json={"token": "x.y.z"}, headers=_bearer(plaintext)
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------
# GET /users/me and GET /users


def test_users_me_is_idempotent(wired):
    client, _deps, project, _clock, _vault, _record, plaintext = wired
    token = _mint(client, plaintext)
    first = client.get("/users/me", headers=_bearer(token))
    second = client.get("/users/me", headers=_bearer(token))

    assert first.status_code == 200
    assert set(first.json()) == {"id", "project_id", "external_user_id", "created_at_ms"}
    assert first.json() == second.json()
    assert first.json()["id"] == end_user_id(project.id, "u-1")
    assert first.json()["created_at_ms"] == NOW


def test_users_me_requires_accounts_read(wired):
    client, _deps, _project, _clock, _vault, _record, plaintext = wired
    token = _mint(client, plaintext, scopes=["users:admin"])
    response = client.get("/users/me", headers=_bearer(token))
    assert response.status_code == 403
    assert response.json()["error"]["type"] == "ScopeError"


def test_users_lists_only_the_calling_project():
    deps, project, _clock, vault = _build(limits=QuotaLimits(50, 500, 5_000))
    other = _second_project(deps, vault)
    _record, plaintext = _issue(deps, project)
    _other_record, other_plaintext = _issue(deps, other)
    client = TestClient(create_app(deps))

    _mint(client, plaintext, external_user_id="alice")
    _mint(client, plaintext, external_user_id="bob")
    _mint(client, other_plaintext, external_user_id="carol")

    mine = client.get("/users", headers=_bearer(plaintext)).json()
    theirs = client.get("/users", headers=_bearer(other_plaintext)).json()

    assert set(mine) == {"users", "count"}
    assert mine["count"] == 2
    assert [user["external_user_id"] for user in mine["users"]] == ["alice", "bob"]
    assert theirs["count"] == 1
    assert [user["external_user_id"] for user in theirs["users"]] == ["carol"]

    mine_ids = {user["id"] for user in mine["users"]}
    theirs_ids = {user["id"] for user in theirs["users"]}
    assert mine_ids.isdisjoint(theirs_ids)
    assert all(user["project_id"] == project.id for user in mine["users"])


def test_users_needs_a_key_not_a_user_token(wired):
    client, _deps, _project, _clock, _vault, _record, plaintext = wired
    token = _mint(client, plaintext)
    response = client.get("/users", headers=_bearer(token))
    assert response.status_code == 401
