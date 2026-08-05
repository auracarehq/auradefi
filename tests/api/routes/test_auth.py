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

import dataclasses

import pytest
from fastapi.testclient import TestClient

from auradefi.api.app import create_app
from auradefi.api.deps import Deps
from auradefi.api.routes.auth import _client_ip
from auradefi.chains.registry import ChainRegistry
from auradefi.clock import Clock, FrozenClock
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.keys import ApiKeyStore
from auradefi.tenancy.models import ApiKey, Environment, Scope, end_user_id
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import RevocationSet, mint_token, verify_token
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
    # pins: the audited IP is the SOCKET PEER, and the forged header is not
    #       consulted at all under the default trusted_proxy_hops=0. This
    #       assertion previously read `== "203.0.113.7"` ("first
    #       X-Forwarded-For hop"), which pinned RELEASE_0.1.1 §4 #30 as a
    #       feature: any caller could choose the IP its own audit row would
    #       record, permanently, in a log with no mutation surface. The
    #       request below still SENDS the header — that is the point of the
    #       test — it just no longer decides the attribution.
    assert entries[0].ip == "testclient", "socket peer, never the caller's header"
    assert entries[0].ip_source == "peer"
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
    deps.keys.revoke(project_id=project.id, key_id=record.id, clock=clock)
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


def test_revoking_another_projects_token_is_indistinguishable_from_any_failure():
    # pins: a foreign token answers with the SAME status and error type as a
    #       forged one. This test previously asserted 404 + NotFoundError,
    #       which pinned RELEASE_0.1.1 §4 #33 as a feature: 404 meant
    #       "authentic and live", 401 AuthError meant "bad signature" and 401
    #       TokenExpiredError meant "authentic but expired", so an attacker
    #       with any free project could sort captured JWTs into replayable
    #       and not — for projects they hold no credential for — without ever
    #       knowing the victim's signing secret. The uniform answer is the fix;
    #       test_revoke_failure_modes_are_byte_identical proves all four agree.
    deps, project, clock, vault = _build(limits=QuotaLimits(50, 500, 5_000))
    other = _second_project(deps, vault)
    _record, plaintext = _issue(deps, project)
    _other_record, other_plaintext = _issue(deps, other)
    client = TestClient(create_app(deps))

    foreign = _mint(client, other_plaintext, external_user_id="u-other")
    response = client.post(
        "/auth/revoke", json={"token": foreign}, headers=_bearer(plaintext)
    )

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "AuthError"
    # And the foreign token still works for its own project: the uniform
    # refusal hides ownership from the CALLER, it does not revoke anything.
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


# ==========================================================================
# 0.1.1 security regressions — RELEASE_0.1.1 §4 #20, #30, #33, #35, #36.
# Every test below carries a `pins:` line naming the ONE falsifiable
# behaviour it discriminates.
# ==========================================================================

PEER_IP = "198.51.100.23"  # the socket peer this TestClient dials from
FORGED_IP = "203.0.113.9"  # what a hostile caller claims in X-Forwarded-For


class _RehydratedKeyStore(ApiKeyStore):
    """Keys as they come back from JSON or SQL: ``scopes`` holds plain ``str``.

    ``ApiKeyStore.issue`` stores ``frozenset(scopes)`` with no coercion, so
    this IS the shape a rehydrated key has, and ``has_scope``'s ``in`` accepts
    it because ``Scope`` is a ``StrEnum``. Overriding ``authenticate`` (rather
    than reaching into the store's private dict) keeps the fixture on the
    public surface and keeps it reaching the pinned branch even once the
    store-side half of the #35 seam starts coercing to ``Scope``.
    """

    def authenticate(self, plaintext: str, clock: Clock) -> ApiKey:
        key = super().authenticate(plaintext, clock)
        return dataclasses.replace(
            key, scopes=frozenset(str(scope) for scope in key.scopes)
        )


def _behind_proxies(deps: Deps, hops: int) -> TestClient:
    """A client whose app trusts ``hops`` rightmost ``X-Forwarded-For`` hops.

    ``dataclasses.replace`` keeps every collaborator instance (audit, quota,
    tenancy) shared with ``deps``, so assertions read the same audit log the
    request wrote to.
    """
    return TestClient(
        create_app(dataclasses.replace(deps, trusted_proxy_hops=hops)),
        client=(PEER_IP, 44321),
    )


def _tamper(token: str) -> str:
    """The same three segments with one signature character changed."""
    return token[:-1] + ("A" if token[-1] != "A" else "B")


# --------------------------------------------------------------------- #20


# pins: an explicitly empty `scopes` list mints a token carrying NO scopes —
#       never every scope the API key itself holds.
def test_an_explicitly_empty_scopes_list_mints_a_zero_privilege_token(wired):
    client, _deps, project, clock, _vault, _record, plaintext = wired
    response = client.post(
        "/auth/token",
        json={"external_user_id": "u-1", "scopes": []},
        headers=_bearer(plaintext),
    )

    assert response.status_code == 200, response.text
    claims = verify_token(
        response.json()["token"], signing_secret=project.signing_secret, clock=clock
    )
    assert claims.scopes == (), (
        "`scopes: []` requests ZERO privileges; `body.scopes or key.scopes` "
        "reads an empty list as 'omitted' and mints the key's full authority"
    )


# pins: a zero-privilege token is REFUSED by a scoped route — the empty
#       scopes claim is enforced, not merely printed on the wire.
def test_a_zero_privilege_token_is_refused_by_a_scoped_route(wired):
    client, _deps, _project, _clock, _vault, _record, plaintext = wired
    token = _mint(client, plaintext, scopes=[])

    response = client.get("/users/me", headers=_bearer(token))

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "type": "ScopeError",
            "message": "missing required scope: accounts:read",
            "status": 403,
        }
    }


# --------------------------------------------------------------------- #35


# pins: a key whose stored scopes are plain `str` mints a token instead of an
#       unformatted 500 — the route reads scopes as tolerantly as it filters
#       them.
def test_a_key_with_wire_string_scopes_mints_instead_of_a_500():
    deps, project, clock, _vault = _build()
    deps = dataclasses.replace(deps, keys=_RehydratedKeyStore())
    _record, plaintext = _issue(deps, project)
    # The fixture must actually reach the branch: plain str, not Scope.
    stored = deps.keys.authenticate(plaintext, clock).scopes
    assert stored == frozenset({"accounts:read", "accounts:write", "users:admin"})
    assert all(type(scope) is str for scope in stored), stored
    # raise_server_exceptions=False so a 500 is an assertable response rather
    # than an AttributeError re-raised out of the client.
    client = TestClient(create_app(deps), raise_server_exceptions=False)

    assert client.get("/users", headers=_bearer(plaintext)).status_code == 200, (
        "the same key authenticates on every sibling route — only minting breaks"
    )

    response = client.post(
        "/auth/token", json={"external_user_id": "u-1"}, headers=_bearer(plaintext)
    )

    assert response.status_code == 200, response.text
    claims = verify_token(
        response.json()["token"], signing_secret=project.signing_secret, clock=clock
    )
    assert claims.scopes == ("accounts:read", "accounts:write", "users:admin")


# --------------------------------------------------------------------- #36


# pins: quota is consumed AFTER authentication and BEFORE the privilege
#       check — an exhausted tenant asking for a scope it lacks is refused
#       429, not 403.
def test_an_exhausted_quota_refuses_a_mint_before_the_privilege_check():
    deps, project, _clock, _vault = _build(limits=QuotaLimits(0, 100, 1_000))
    _record, plaintext = _issue(deps, project, Scope.USERS_ADMIN, Scope.ACCOUNTS_READ)

    response = TestClient(create_app(deps)).post(
        "/auth/token",
        json={"external_user_id": "u-1", "scopes": ["accounts:write"]},
        headers=_bearer(plaintext),
    )

    assert response.status_code == 429, response.text
    assert response.json()["error"]["type"] == "QuotaExceededError"
    assert deps.audit.entries(project.id) == ()


# pins: a mint refused by the privilege rule still costs exactly one quota
#       unit in every window, and writes NO audit row.
def test_a_refused_mint_is_charged_one_unit_and_never_audited():
    deps, project, _clock, _vault = _build()
    _record, plaintext = _issue(deps, project, Scope.USERS_ADMIN, Scope.ACCOUNTS_READ)

    response = TestClient(create_app(deps)).post(
        "/auth/token",
        json={"external_user_id": "u-1", "scopes": ["accounts:write"]},
        headers=_bearer(plaintext),
    )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "ScopeError"
    snapshot = deps.quota.snapshot(project.id)
    assert (
        snapshot["second"].remaining,
        snapshot["day"].remaining,
        snapshot["month"].remaining,
    ) == (4, 99, 999), "an authenticated refusal that decrements nothing is free"
    assert deps.audit.entries(project.id) == (), (
        "the refusal is charged, never recorded: no token.minted row"
    )


# pins: a SUCCESSFUL mint costs exactly one quota unit — charging in the
#       route must not charge a second time downstream.
def test_a_successful_mint_is_charged_exactly_one_unit_not_two(wired):
    client, deps, project, _clock, _vault, _record, plaintext = wired

    response = client.post(
        "/auth/token", json={"external_user_id": "u-1"}, headers=_bearer(plaintext)
    )

    assert response.status_code == 200, response.text
    snapshot = deps.quota.snapshot(project.id)
    assert (
        snapshot["second"].remaining,
        snapshot["day"].remaining,
        snapshot["month"].remaining,
    ) == (4, 99, 999)
    assert len(deps.audit.entries(project.id)) == 1


# --------------------------------------------------------------------- #33


# The jti a caller tries to revoke for a project it holds no credential for.
SMUGGLED_JTI = "aa" * 16


def _own_signature_foreign_claim(deps, project, other) -> str:
    """A token signed with the CALLER'S OWN secret while claiming ``other``.

    This is the ONLY fixture that reaches the ownership guard. Every other
    foreign case in this file is signed with ``other.signing_secret`` and dies
    at the signature, so ``claims.project_id != key.project_id`` is never
    evaluated. A caller can build this one unaided — it holds its own signing
    secret by definition — and ``RevocationSet`` is not tenant-scoped, so
    accepting it would revoke a jti belonging to a project the caller cannot
    name.
    """
    return mint_token(
        signing_secret=project.signing_secret,
        project_id=other.id,
        external_user_id="u-smuggled",
        scopes=["accounts:read"],
        ttl_ms=600_000,
        clock=deps.clock,
        jti=SMUGGLED_JTI,
    )


def _unowned_revoke_tokens(deps, client, project, other, plaintext, other_plaintext):
    """The five RELEASE_0.1.1 #33 cases the caller does not own, plus garbage.

    Garbage is included because the shipped suite already pins it at 401
    AuthError, so it IS the route's one failure answer: every other failure
    must be indistinguishable from it.

    The fifth case is signed with the CALLER'S OWN secret, so it is the only
    one that survives verification and reaches the ownership guard — the last
    thing binding a verified token to the calling tenant.
    """
    return {
        "own project, bad signature": _tamper(_mint(client, plaintext)),
        "foreign, genuine and live": _mint(
            client, other_plaintext, external_user_id="u-other"
        ),
        "foreign, genuine but expired": mint_token(
            signing_secret=other.signing_secret,
            project_id=other.id,
            external_user_id="u-other",
            scopes=["accounts:read"],
            ttl_ms=0,  # exp == iat, and now_ms >= exp is expired (exclusive)
            clock=deps.clock,
            jti="11" * 16,
        ),
        "foreign, well formed, unknown jti": mint_token(
            signing_secret=other.signing_secret,
            project_id=other.id,
            external_user_id="u-never-seen",
            scopes=["accounts:read"],
            ttl_ms=600_000,
            clock=deps.clock,
            jti="de" * 16,
        ),
        "own signature, foreign project claim": _own_signature_foreign_claim(
            deps, project, other
        ),
        "structurally malformed": "not.a.token",
    }


# pins: no property of a token belonging to another project — authentic, live,
#       expired, unknown, or signed by us and merely CLAIMING that project —
#       is observable through POST /auth/revoke; every unowned token answers
#       with the same status and the same body bytes as a forged one.
def test_revoke_answers_identically_for_every_token_the_caller_does_not_own():
    deps, project, _clock, vault = _build(limits=QuotaLimits(50, 500, 5_000))
    other = _second_project(deps, vault)
    _record, plaintext = _issue(deps, project)
    _other_record, other_plaintext = _issue(deps, other)
    client = TestClient(create_app(deps), raise_server_exceptions=False)
    cases = _unowned_revoke_tokens(
        deps, client, project, other, plaintext, other_plaintext
    )

    answers = {
        label: client.post(
            "/auth/revoke", json={"token": token}, headers=_bearer(plaintext)
        )
        for label, token in cases.items()
    }

    distinct = {
        (response.status_code, response.content) for response in answers.values()
    }
    assert len(distinct) == 1, (
        "POST /auth/revoke is a cross-tenant authenticity oracle — it answers "
        f"{len(distinct)} different ways:\n"
        + "\n".join(
            f"  {label}: {response.status_code} {response.text}"
            for label, response in answers.items()
        )
    )
    one = answers["foreign, genuine and live"]
    assert one.status_code == 401
    assert one.json()["error"]["type"] == "AuthError"
    assert one.json()["error"]["status"] == 401


# pins: a revoke the route refuses REVOKES NOTHING — the foreign token is
#       still live for the project that owns it.
def test_a_refused_cross_tenant_revoke_leaves_the_foreign_token_live():
    deps, project, _clock, vault = _build(limits=QuotaLimits(50, 500, 5_000))
    other = _second_project(deps, vault)
    _record, plaintext = _issue(deps, project)
    _other_record, other_plaintext = _issue(deps, other)
    client = TestClient(create_app(deps), raise_server_exceptions=False)
    foreign = _mint(client, other_plaintext, external_user_id="u-other")

    refused = client.post(
        "/auth/revoke", json={"token": foreign}, headers=_bearer(plaintext)
    )

    assert refused.status_code >= 400, "another project's token is not revocable"
    assert "revoked" not in refused.json()
    assert client.get("/users/me", headers=_bearer(foreign)).status_code == 200


# pins: a token that PASSES verification under the caller's own secret while
#       claiming another project's id revokes nothing — the ownership guard,
#       not the signature, is what refuses it.
def test_a_verified_token_claiming_another_project_revokes_nothing():
    deps, project, _clock, vault = _build(limits=QuotaLimits(50, 500, 5_000))
    other = _second_project(deps, vault)
    _record, plaintext = _issue(deps, project)
    client = TestClient(create_app(deps), raise_server_exceptions=False)
    smuggled = _own_signature_foreign_claim(deps, project, other)
    # The fixture must reach the GUARD, not die at the signature: it verifies
    # under the caller's own secret and still names the other project.
    claims = verify_token(
        smuggled, signing_secret=project.signing_secret, clock=deps.clock
    )
    assert claims.project_id == other.id != project.id
    assert claims.jti == SMUGGLED_JTI

    refused = client.post(
        "/auth/revoke", json={"token": smuggled}, headers=_bearer(plaintext)
    )

    assert deps.revocations.is_revoked(SMUGGLED_JTI) is False, (
        "RevocationSet is not tenant-scoped, so dropping the ownership guard "
        "lets a caller revoke a jti for a project it holds no credential for"
    )
    assert refused.status_code >= 400
    assert "revoked" not in refused.json()


# --------------------------------------------------------------------- #30


def _audited_mint(client, plaintext, forwarded: str):
    """POST /auth/token carrying ``X-Forwarded-For: forwarded``."""
    response = client.post(
        "/auth/token",
        json={"external_user_id": "u-1"},
        headers={**_bearer(plaintext), "X-Forwarded-For": forwarded},
    )
    assert response.status_code == 200, response.text
    return response


# pins: with the default zero trusted proxy hops the socket peer is the ONLY
#       source of the audited IP — X-Forwarded-For is not consulted at all.
def test_a_forwarded_for_header_never_reaches_the_audit_row_by_default():
    deps, project, _clock, _vault = _build()
    assert deps.trusted_proxy_hops == 0, "the pinned default: trust no proxy"
    _record, plaintext = _issue(deps, project)
    client = _behind_proxies(deps, 0)

    _audited_mint(client, plaintext, FORGED_IP)

    (record,) = deps.audit.entries(project.id)
    assert record.ip == PEER_IP, f"audited a caller-supplied IP: {record.ip!r}"


# pins: the audit row DECLARES that a socket-derived IP came from the peer,
#       so a header-derived one can never be mistaken for a verified one.
def test_the_audit_row_declares_a_socket_derived_ip_as_peer():
    deps, project, _clock, _vault = _build()
    _record, plaintext = _issue(deps, project)
    client = _behind_proxies(deps, 0)

    _audited_mint(client, plaintext, FORGED_IP)

    (record,) = deps.audit.entries(project.id)
    assert record.ip_source == "peer"


# pins: with one trusted proxy hop the audited IP is the FIRST hop from the
#       right — the one that proxy appended — declared as header-derived.
def test_one_trusted_hop_audits_that_hop_and_declares_it_forwarded():
    deps, project, _clock, _vault = _build()
    _record, plaintext = _issue(deps, project)
    client = _behind_proxies(deps, 1)

    _audited_mint(client, plaintext, f"{FORGED_IP}, 198.51.100.4")

    (record,) = deps.audit.entries(project.id)
    assert record.ip == "198.51.100.4", (
        "trusted_proxy_hops=1 trusts the 1st hop from the RIGHT — the address "
        "our own proxy appended — and never the leftmost, caller-written one"
    )
    assert record.ip_source == "forwarded"


# pins: the trusted hop is picked from EVERY X-Forwarded-For FIELD LINE joined,
#       so a proxy that appends its own line instead of extending the caller's
#       still supplies the audited IP — the caller's line is never the hop.
def test_a_repeated_forwarded_for_field_line_is_joined_before_the_hop_is_picked():
    deps, project, _clock, _vault = _build()
    _record, plaintext = _issue(deps, project)
    client = _behind_proxies(deps, 1)

    # TWO field lines, not one comma list — RFC 9110 §5.3 makes them one
    # comma-joined value, and a proxy appending its own line is an ordinary
    # wire form. The list-of-tuples form is required: `headers={...}` cannot
    # express a repeated field name, which is why the comma fixture above
    # passes whether the chain is joined or only its first line is read.
    response = client.post(
        "/auth/token",
        json={"external_user_id": "u-1"},
        headers=[
            (b"authorization", f"Bearer {plaintext}".encode()),
            (b"x-forwarded-for", FORGED_IP.encode()),  # the caller's own line
            (b"x-forwarded-for", b"198.51.100.4"),  # our outermost proxy's
        ],
    )
    assert response.status_code == 200, response.text
    # The fixture must REACH the join: if the client ever collapsed the two
    # lines into one, this test would stop discriminating the pinned branch.
    assert response.request.headers.get_list("x-forwarded-for") == [
        FORGED_IP,
        "198.51.100.4",
    ], "the request must leave with two X-Forwarded-For field lines"

    (record,) = deps.audit.entries(project.id)
    assert record.ip == "198.51.100.4", (
        "reading only the FIRST X-Forwarded-For field line counts the caller's "
        "own line as the trusted rightmost hop, so a forged address lands in "
        f"the permanent audit row stamped 'forwarded': audited {record.ip!r}"
    )
    assert record.ip_source == "forwarded"


# pins: a chain shorter than the trusted hop count is not trusted at all —
#       a caller-written hop never lands in the audit row as forwarded.
def test_a_chain_shorter_than_the_trusted_hop_count_is_not_trusted():
    deps, project, _clock, _vault = _build()
    _record, plaintext = _issue(deps, project)
    client = _behind_proxies(deps, 2)

    _audited_mint(client, plaintext, FORGED_IP)  # one hop, two expected

    (record,) = deps.audit.entries(project.id)
    assert record.ip != FORGED_IP, "an untrustable hop reached the audit row"
    assert record.ip_source != "forwarded"


# pins: an EMPTY trusted hop is no address at all — the peer is audited and the
#       provenance says peer, so a blank value is never stamped "forwarded".
def test_an_empty_trusted_hop_falls_back_to_the_peer_not_a_blank_forwarded_ip():
    deps, project, _clock, _vault = _build()
    _record, plaintext = _issue(deps, project)
    client = _behind_proxies(deps, 1)

    # A trailing comma makes the 1st hop from the right the EMPTY string, which
    # is the hostile shape: it shifts the caller's own address out of the
    # trusted position while supplying nothing verified in its place.
    _audited_mint(client, plaintext, f"{FORGED_IP},")

    (record,) = deps.audit.entries(project.id)
    assert record.ip == PEER_IP, f"a blank hop displaced the peer: {record.ip!r}"
    assert record.ip_source == "peer", (
        "an unknown IP is DECLARED, never guessed: a blank trusted hop must "
        f"not be stamped as header-derived, got {record.ip_source!r}"
    )


class _StubRequest:
    """Just the two attributes ``_client_ip`` reads."""

    def __init__(self, hops: list[str], client: object | None) -> None:
        self.headers = _StubHeaders(hops)
        self.client = client


class _StubHeaders:
    def __init__(self, hops: list[str]) -> None:
        self._hops = hops

    def getlist(self, name: str) -> list[str]:
        assert name == "x-forwarded-for"
        return self._hops


class _Peer:
    def __init__(self, host: str) -> None:
        self.host = host


def test_an_unattributable_request_is_declared_unknown_never_guessed():
    # pins: with no trusted proxy and no socket peer there is NO ip, and the
    #       audit row says so with the literal "unknown". The audit log has
    #       no mutation surface, so a guessed value would be permanent and
    #       indistinguishable from an observed one; the terminal no-data
    #       branch must stay a declared absence rather than be promoted to a
    #       confident empty string that reads like a real peer.
    assert _client_ip(_StubRequest([], None), 0) == ("", "unknown")
    assert _client_ip(_StubRequest(["203.0.113.7"], None), 0) == ("", "unknown")
    assert _client_ip(_StubRequest([], _Peer("")), 0) == ("", "unknown")


def test_the_socket_peer_is_labelled_peer_and_a_trusted_hop_is_labelled_forwarded():
    # The controls that make "unknown" meaningful: the other two sources are
    # distinguishable from it and from each other.
    assert _client_ip(_StubRequest([], _Peer("10.0.0.4")), 0) == ("10.0.0.4", "peer")
    assert _client_ip(_StubRequest(["203.0.113.7"], _Peer("10.0.0.4")), 0) == (
        "10.0.0.4",
        "peer",
    ), "hops=0 never reads the header"
    assert _client_ip(_StubRequest(["1.1.1.1, 203.0.113.7"], _Peer("10.0.0.4")), 1) == (
        "203.0.113.7",
        "forwarded",
    )
