"""api/deps.py — the container, auth resolution and the nine quota headers.

Everything here runs offline under the autouse socket guard: TestClient
drives the ASGI app in-process, never over a socket.

The golden header block is arithmetic, not a fixture: FrozenClock's
1_754_000_000_000 is 2025-07-31T22:13:20Z, where the next UTC *day*
boundary (1754006400000) IS the next UTC *month* boundary — hence
Reset-Day == Reset-Month. Verified independently with datetime.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from auradefi.api.deps import (
    Deps,
    HoldingsProvider,
    WebhookSink,
    consume_quota,
    install_quota_headers,
    quota_headers,
    require_api_key,
    require_user_token,
    resolve_end_user,
    retry_after_seconds,
)
from auradefi.api.errors import install_error_handlers
from auradefi.chains.registry import ChainRegistry
from auradefi.clock import FrozenClock
from auradefi.errors import (
    AuthError,
    NotFoundError,
    QuotaExceededError,
    ScopeError,
    TokenExpiredError,
    TokenRevokedError,
)
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.keys import ApiKeyStore
from auradefi.tenancy.models import Environment, Scope, end_user_id
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits, WindowSnapshot
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import RevocationSet, mint_token, verify_token

NOW = 1_754_000_000_000
DEPS_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "auradefi" / "api" / "deps.py"
)

# DECISIONS "Quota headers": nine, decimal strings, Reset a MS-EPOCH int.
PINNED_HEADERS = {
    "X-RateLimit-Limit-Second": "5",
    "X-RateLimit-Remaining-Second": "5",
    "X-RateLimit-Reset-Second": "1754000001000",
    "X-RateLimit-Limit-Day": "100",
    "X-RateLimit-Remaining-Day": "100",
    "X-RateLimit-Reset-Day": "1754006400000",
    "X-RateLimit-Limit-Month": "1000",
    "X-RateLimit-Remaining-Month": "1000",
    "X-RateLimit-Reset-Month": "1754006400000",
}


class _Sink:
    """A trivial structural WebhookSink — seven methods, no state, no I/O."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def register_endpoint(self, project_id, url, events, clock):  # noqa: ANN001
        self.calls.append("register_endpoint")

    def endpoints(self, project_id):  # noqa: ANN001
        self.calls.append("endpoints")
        return ()

    def emit(self, project_id, name, data, clock):  # noqa: ANN001
        self.calls.append("emit")

    def deliveries(self, project_id):  # noqa: ANN001
        self.calls.append("deliveries")
        return ()

    def dead_letter(self, project_id):  # noqa: ANN001
        self.calls.append("dead_letter")
        return ()

    def get_delivery(self, project_id, delivery_id):  # noqa: ANN001
        self.calls.append("get_delivery")

    def get_event(self, project_id, event_id):  # noqa: ANN001
        self.calls.append("get_event")


class _Holdings:
    """A trivial structural HoldingsProvider (api may not import portfolio)."""

    def holdings(self, chain_id: str, address: str) -> object:
        return (chain_id, address)


class _CountingClock:
    """Records every read, so 'no I/O at construction' is measurable."""

    def __init__(self) -> None:
        self.reads = 0

    def now_ms(self) -> int:
        self.reads += 1
        return NOW


def _build(limits: QuotaLimits | None = None, now_ms: int = NOW):
    """A wired Deps over real Phase 0-2 collaborators; returns (deps, project, clock)."""
    clock = FrozenClock(now_ms)
    tenancy = TenancyStore()
    org = tenancy.create_organisation("acme", clock)
    project = tenancy.create_project(org.id, "main", Environment.TEST, clock)
    vault = {project.id: project.signing_secret}
    deps = Deps(
        tenancy=tenancy,
        keys=ApiKeyStore(),
        quota=QuotaCounter(limits or QuotaLimits(1_000, 1_000, 1_000), clock),
        audit=AuditLog(),
        revocations=RevocationSet(),
        ledger=MemoryLedger(),
        webhooks=_Sink(),
        chains=ChainRegistry(),
        clock=clock,
        signing_secret_for=vault.get,
    )
    return deps, project, clock


def _request(authorization: str | None = None) -> Request:
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "query_string": b"",
            "state": {},
        }
    )


def _mint(project, clock, scopes=("accounts:read",), ttl_ms=600_000, jti="jti-1"):
    return mint_token(
        signing_secret=project.signing_secret,
        project_id=project.id,
        external_user_id="u-1",
        scopes=scopes,
        ttl_ms=ttl_ms,
        clock=clock,
        jti=jti,
    )


def _segment(obj: object) -> str:
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _imported_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names)
    return names


# --------------------------------------------------------------------------
# the container


def test_deps_is_a_frozen_slots_dataclass():
    deps, _project, _clock = _build()
    assert dataclasses.is_dataclass(Deps)
    assert Deps.__dataclass_params__.frozen is True
    assert hasattr(Deps, "__slots__")
    assert not hasattr(deps, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        deps.token_ttl_ms = 1


def test_constructing_deps_touches_no_collaborator_and_defaults_are_pinned():
    clock = _CountingClock()
    sink = _Sink()
    deps = Deps(
        tenancy=TenancyStore(),
        keys=ApiKeyStore(),
        quota=QuotaCounter(QuotaLimits(1, 1, 1), clock),
        audit=AuditLog(),
        revocations=RevocationSet(),
        ledger=MemoryLedger(),
        webhooks=sink,
        chains=ChainRegistry(),
        clock=clock,
        signing_secret_for=lambda project_id: None,
    )
    assert clock.reads == 0
    assert sink.calls == []
    assert deps.holdings is None
    assert deps.delete_connection is None
    assert deps.capabilities == {}
    assert deps.token_ttl_ms == 600_000
    assert deps.sync_limit_default == 100
    assert deps.sync_limit_max == 500
    assert deps.batch_max_items == 100


def test_signing_secret_for_is_a_plain_callable_never_a_bound_method():
    deps, project, _clock = _build()
    assert deps.signing_secret_for(project.id) == project.signing_secret
    assert len(project.signing_secret) == 64
    assert deps.signing_secret_for("proj_0000000000000000") is None


def test_the_two_seams_are_runtime_checkable_structural_protocols():
    class _Partial:
        def register_endpoint(self, project_id, url, events, clock):  # noqa: ANN001
            return None

    assert isinstance(_Sink(), WebhookSink)
    assert isinstance(_Holdings(), HoldingsProvider)
    assert not isinstance(_Partial(), WebhookSink)
    assert not isinstance(object(), WebhookSink)
    assert not isinstance(object(), HoldingsProvider)


def test_deps_imports_neither_portfolio_nor_webhooks_nor_an_orm():
    banned = ("auradefi.portfolio", "auradefi.webhooks", "sqlalchemy", "sqlmodel")
    offenders = [
        name
        for name in _imported_names(DEPS_SOURCE)
        if any(name == b or name.startswith(f"{b}.") for b in banned)
    ]
    assert not offenders, f"api/deps.py must stay independent: {offenders}"


# --------------------------------------------------------------------------
# api key resolution


def test_require_api_key_returns_the_key_and_binds_the_project():
    deps, project, clock = _build()
    record, plaintext = deps.keys.issue(
        project.id, Environment.TEST, {Scope.ACCOUNTS_READ}, clock
    )
    assert plaintext.startswith("adk_test_")
    assert len(plaintext) == 57
    request = _request(f"Bearer {plaintext}")
    key = require_api_key(deps, request, Scope.ACCOUNTS_READ)
    assert key.id == record.id
    assert key.project_id == project.id
    assert request.state.project_id == project.id


def test_every_api_key_failure_raises_one_indistinguishable_auth_error():
    deps, project, clock = _build()
    record, revoked_plaintext = deps.keys.issue(
        project.id, Environment.TEST, {Scope.ACCOUNTS_READ}, clock
    )
    deps.keys.revoke(project.id, record.id, clock)
    with pytest.raises(AuthError) as store_rejection:
        deps.keys.authenticate(f"adk_test_{'0' * 48}", clock)
    baseline = str(store_rejection.value)

    headers = {
        "missing header": None,
        "empty bearer": "Bearer ",
        "garbage": "Bearer garbage",
        "wrong scheme": f"Basic {revoked_plaintext}",
        "no scheme": revoked_plaintext,
        "unknown key": f"Bearer adk_live_{'f' * 48}",
        "revoked key": f"Bearer {revoked_plaintext}",
    }
    messages = set()
    for label, header in headers.items():
        with pytest.raises(AuthError) as caught:
            require_api_key(deps, _request(header), Scope.ACCOUNTS_READ)
        assert type(caught.value) is AuthError, label
        messages.add(str(caught.value))
    assert messages == {baseline}


def test_missing_key_scope_raises_scope_error_after_the_project_is_bound():
    deps, project, clock = _build()
    _record, plaintext = deps.keys.issue(
        project.id, Environment.TEST, {Scope.ACCOUNTS_READ}, clock
    )
    request = _request(f"Bearer {plaintext}")
    with pytest.raises(ScopeError):
        require_api_key(deps, request, Scope.USERS_ADMIN)
    assert request.state.project_id == project.id


# --------------------------------------------------------------------------
# user token resolution


def test_require_user_token_returns_claims_and_binds_the_project():
    deps, project, clock = _build()
    request = _request(f"Bearer {_mint(project, clock)}")
    claims = require_user_token(deps, request, "accounts:read")
    assert claims.project_id == project.id
    assert claims.external_user_id == "u-1"
    assert claims.scopes == ("accounts:read",)
    assert claims.iat == NOW
    assert claims.exp == NOW + 600_000
    assert claims.jti == "jti-1"
    assert request.state.project_id == project.id


def test_a_token_minted_under_one_project_never_verifies_under_another():
    deps, project_a, clock = _build()
    project_b = deps.tenancy.create_project(
        project_a.org_id, "other", Environment.TEST, clock
    )
    assert project_a.signing_secret != project_b.signing_secret
    token = _mint(project_a, clock)
    crossed = dataclasses.replace(
        deps, signing_secret_for={project_a.id: project_b.signing_secret}.get
    )
    with pytest.raises(AuthError) as caught:
        require_user_token(crossed, _request(f"Bearer {token}"), "accounts:read")
    assert type(caught.value) is AuthError


def test_token_probing_learns_nothing_and_cannot_enumerate_project_ids():
    deps, project, clock = _build()
    with pytest.raises(AuthError) as reference:
        verify_token(
            _mint(project, clock), signing_secret="0" * 64, clock=clock, revoked=None
        )
    baseline = str(reference.value)
    header_b64 = _mint(project, clock).split(".")[0]
    unparseable = "@@@@"
    not_json = base64.urlsafe_b64encode(b"{").rstrip(b"=").decode("ascii")
    no_project = _segment(
        {"exp": NOW + 1, "external_user_id": "u-1", "iat": NOW, "jti": "j", "scopes": []}
    )
    bad_project_type = _segment(
        {
            "exp": NOW + 1,
            "external_user_id": "u-1",
            "iat": NOW,
            "jti": "j",
            "project_id": 123,
            "scopes": [],
        }
    )
    unknown_project = mint_token(
        signing_secret="f" * 64,
        project_id="proj_deadbeefdeadbeef",
        external_user_id="u-1",
        scopes=["accounts:read"],
        ttl_ms=600_000,
        clock=clock,
        jti="j",
    )
    cases = {
        "missing header": None,
        "not a token": "Bearer not-a-token",
        "unparseable payload": f"Bearer {header_b64}.{unparseable}.sig",
        "payload not json": f"Bearer {header_b64}.{not_json}.sig",
        "no project_id claim": f"Bearer {header_b64}.{no_project}.sig",
        "non-str project_id": f"Bearer {header_b64}.{bad_project_type}.sig",
        "unknown project": f"Bearer {unknown_project}",
    }
    for label, header in cases.items():
        with pytest.raises(AuthError) as caught:
            require_user_token(deps, _request(header), "accounts:read")
        assert type(caught.value) is AuthError, label
        assert str(caught.value) == baseline, label


def test_an_unknown_project_is_an_auth_error_never_a_not_found():
    deps, _project, clock = _build()
    token = mint_token(
        signing_secret="a" * 64,
        project_id="proj_1111111111111111",
        external_user_id="u-1",
        scopes=["accounts:read"],
        ttl_ms=600_000,
        clock=clock,
        jti="j",
    )
    assert deps.signing_secret_for("proj_1111111111111111") is None
    try:
        require_user_token(deps, _request(f"Bearer {token}"), "accounts:read")
    except NotFoundError as leak:  # pragma: no cover - the failure we guard
        pytest.fail(f"a 404 would enumerate project ids: {leak!r}")
    except AuthError as caught:
        assert type(caught) is AuthError
    else:  # pragma: no cover - the failure we guard
        pytest.fail("an unknown project must never authenticate")


def test_a_raising_resolver_is_still_an_auth_error_never_a_leaked_not_found():
    """The resolver may RAISE for an unknown project, not just return None.

    Every other resolver in this suite is ``dict.get``-shaped and can
    never reach ``_signing_secret``'s except clause — yet the two idioms
    this repo actually ships both raise: ``TenancyStore._require_project``
    raises ``NotFoundError``, a bare dict-backed vault raises
    ``KeyError``. Either escaping ``require_user_token`` answers "does
    this project exist?" with a 404/500 — the enumeration channel
    SPEC §7.2 closes.
    """
    deps, _project, clock = _build()
    with pytest.raises(AuthError) as reference:
        require_user_token(deps, _request("Bearer not-a-token"), "accounts:read")
    baseline = str(reference.value)
    unknown = "proj_1111111111111111"
    token = mint_token(
        signing_secret="a" * 64,
        project_id=unknown,
        external_user_id="u-1",
        scopes=["accounts:read"],
        ttl_ms=600_000,
        clock=clock,
        jti="j",
    )

    def _raises_not_found(project_id: str) -> str | None:
        raise NotFoundError(f"project not found: {project_id!r}")

    cases = (
        ("NotFoundError — the TenancyStore idiom", _raises_not_found, NotFoundError),
        ("KeyError — a bare dict-backed vault", {}.__getitem__, LookupError),
    )
    for label, resolver, raised in cases:
        # the resolver really does raise: this exercises the except clause,
        # not the returns-None path the test above already covers.
        with pytest.raises(raised):
            resolver(unknown)
        scoped = dataclasses.replace(deps, signing_secret_for=resolver)
        with pytest.raises(AuthError) as caught:
            require_user_token(scoped, _request(f"Bearer {token}"), "accounts:read")
        assert type(caught.value) is AuthError, label
        assert str(caught.value) == baseline, label


def test_a_resolver_failing_for_our_own_reasons_is_not_disguised_as_a_bad_token():
    """An unreachable secret store is a 500, never a 401.

    Pins the except clause NARROW. Widening it to ``except Exception``
    would tell every caller "your token is bad" while the vault is down,
    and no other test in this file would notice.
    """
    deps, project, clock = _build()

    def _explodes(project_id: str) -> str | None:
        raise OSError("secret store unreachable")

    broken = dataclasses.replace(deps, signing_secret_for=_explodes)
    request = _request(f"Bearer {_mint(project, clock)}")
    with pytest.raises(OSError) as caught:
        require_user_token(broken, request, "accounts:read")
    assert type(caught.value) is OSError
    assert not isinstance(caught.value, AuthError)
    assert str(caught.value) == "secret store unreachable"
    assert not hasattr(request.state, "project_id")


def test_expired_token_raises_token_expired_error_exclusively_at_exp():
    deps, project, clock = _build()
    token = _mint(project, clock, ttl_ms=1_000)
    clock.advance(999)
    assert require_user_token(deps, _request(f"Bearer {token}"), "accounts:read").exp == (
        NOW + 1_000
    )
    clock.advance(1)
    with pytest.raises(TokenExpiredError):
        require_user_token(deps, _request(f"Bearer {token}"), "accounts:read")


def test_revoked_jti_raises_token_revoked_error():
    deps, project, clock = _build()
    token = _mint(project, clock, jti="jti-revoked")
    deps.revocations.revoke("jti-revoked")
    with pytest.raises(TokenRevokedError):
        require_user_token(deps, _request(f"Bearer {token}"), "accounts:read")


def test_signature_is_checked_before_expiry():
    deps, project, clock = _build()
    token = _mint(project, clock, ttl_ms=1_000)
    forged = token[:-1] + ("a" if token[-1] != "a" else "b")
    clock.advance(10_000)
    with pytest.raises(AuthError) as caught:
        require_user_token(deps, _request(f"Bearer {forged}"), "accounts:read")
    assert type(caught.value) is AuthError


def test_user_token_missing_scope_raises_scope_error_after_binding():
    deps, project, clock = _build()
    request = _request(f"Bearer {_mint(project, clock, scopes=('accounts:read',))}")
    with pytest.raises(ScopeError):
        require_user_token(deps, request, "users:admin")
    assert request.state.project_id == project.id


def test_resolve_end_user_is_get_or_create_and_stays_idempotent():
    deps, project, clock = _build()
    claims = require_user_token(
        deps, _request(f"Bearer {_mint(project, clock)}"), "accounts:read"
    )
    user = resolve_end_user(deps, claims)
    assert user.id == end_user_id(project.id, "u-1")
    assert user.project_id == project.id
    assert user.created_at == NOW
    clock.advance(5_000)
    assert resolve_end_user(deps, claims) == user
    assert deps.tenancy.users(project.id) == (user,)


# --------------------------------------------------------------------------
# quota


def test_consume_quota_takes_one_unit_and_a_rejected_hit_takes_none():
    deps, project, _clock = _build(limits=QuotaLimits(1, 10, 100))
    consume_quota(deps, project.id)
    snapshot = deps.quota.snapshot(project.id)
    assert snapshot["second"].remaining == 0
    assert snapshot["day"].remaining == 9
    assert snapshot["month"].remaining == 99
    with pytest.raises(QuotaExceededError):
        consume_quota(deps, project.id)
    assert deps.quota.snapshot(project.id)["day"].remaining == 9


def test_quota_headers_are_the_nine_pinned_headers():
    counter = QuotaCounter(QuotaLimits(5, 100, 1_000), FrozenClock(NOW))
    headers = quota_headers(counter.snapshot("proj_x"))
    assert headers == PINNED_HEADERS
    assert len(headers) == 9
    assert all(isinstance(value, str) for value in headers.values())
    # Reset is a ms-epoch int as a decimal string, never seconds nor ISO-8601.
    assert headers["X-RateLimit-Reset-Second"] == str(1754000001000)
    assert headers["X-RateLimit-Reset-Day"] == headers["X-RateLimit-Reset-Month"]


def test_quota_headers_track_consumption_per_window():
    counter = QuotaCounter(QuotaLimits(5, 100, 1_000), FrozenClock(NOW))
    counter.hit("proj_x")
    counter.hit("proj_x")
    headers = quota_headers(counter.snapshot("proj_x"))
    assert headers["X-RateLimit-Remaining-Second"] == "3"
    assert headers["X-RateLimit-Remaining-Day"] == "98"
    assert headers["X-RateLimit-Remaining-Month"] == "998"
    assert headers["X-RateLimit-Limit-Second"] == "5"
    assert quota_headers(counter.snapshot("proj_other"))[
        "X-RateLimit-Remaining-Second"
    ] == "5"


def test_retry_after_seconds_ceils_over_the_smallest_exhausted_window():
    second_gone = {
        "second": WindowSnapshot(1, 0, 1754000001000),
        "day": WindowSnapshot(10, 9, 1754006400000),
        "month": WindowSnapshot(100, 99, 1754006400000),
    }
    assert retry_after_seconds(second_gone, NOW) == 1
    assert retry_after_seconds(second_gone, 1754000000999) == 1
    # a reset already behind us still answers a legal RFC 9110 value
    assert retry_after_seconds(second_gone, 1754000009999) == 1

    long_windows = {
        "second": WindowSnapshot(1, 1, 1754000001000),
        "day": WindowSnapshot(10, 0, 1754006400000),
        "month": WindowSnapshot(100, 0, 1756684800000),
    }
    assert retry_after_seconds(long_windows, NOW) == 6400
    assert retry_after_seconds(long_windows, NOW + 1) == 6400

    nothing_exhausted = {
        "second": WindowSnapshot(5, 5, 1754000001000),
        "day": WindowSnapshot(100, 100, 1754006400000),
        "month": WindowSnapshot(1_000, 1_000, 1754006400000),
    }
    assert retry_after_seconds(nothing_exhausted, NOW) == 1


# --------------------------------------------------------------------------
# the middleware


def _app(deps: Deps, project_id: str) -> FastAPI:
    app = FastAPI()

    @app.get("/ok")
    def _ok(request: Request) -> dict[str, bool]:
        request.state.project_id = project_id
        return {"ok": True}

    @app.get("/scope")
    def _scope(request: Request) -> dict[str, bool]:
        request.state.project_id = project_id
        raise ScopeError("missing required scope: users:admin")

    @app.get("/quota")
    def _quota(request: Request) -> dict[str, bool]:
        request.state.project_id = project_id
        consume_quota(deps, project_id)
        consume_quota(deps, project_id)
        return {"ok": True}

    @app.get("/anon")
    def _anon() -> dict[str, bool]:
        return {"ok": True}

    install_error_handlers(app, deps)
    install_quota_headers(app, deps)
    return app


def _rate_limit_headers(response) -> list[str]:
    return [name for name in response.headers if name.lower().startswith("x-ratelimit")]


def test_the_nine_headers_ride_a_200_and_a_scope_error_403_alike():
    deps, project, _clock = _build(limits=QuotaLimits(5, 100, 1_000))
    client = TestClient(_app(deps, project.id))

    ok = client.get("/ok")
    assert ok.status_code == 200
    for name, value in PINNED_HEADERS.items():
        assert ok.headers[name] == value, name
    assert len(_rate_limit_headers(ok)) == 9

    forbidden = client.get("/scope")
    assert forbidden.status_code == 403
    for name, value in PINNED_HEADERS.items():
        assert forbidden.headers[name] == value, name
    assert len(_rate_limit_headers(forbidden)) == 9


def test_the_nine_headers_ride_a_quota_exceeded_429():
    deps, project, _clock = _build(limits=QuotaLimits(1, 10, 100))
    throttled = TestClient(_app(deps, project.id)).get("/quota")
    assert throttled.status_code == 429
    assert throttled.headers["X-RateLimit-Limit-Second"] == "1"
    assert throttled.headers["X-RateLimit-Remaining-Second"] == "0"
    assert throttled.headers["X-RateLimit-Remaining-Day"] == "9"
    assert throttled.headers["X-RateLimit-Remaining-Month"] == "99"
    assert throttled.headers["X-RateLimit-Reset-Second"] == "1754000001000"
    assert throttled.headers["Retry-After"] == "1"
    assert len(_rate_limit_headers(throttled)) == 9


def test_the_middleware_snapshot_consumes_nothing():
    deps, project, _clock = _build(limits=QuotaLimits(5, 100, 1_000))
    client = TestClient(_app(deps, project.id))
    for _ in range(3):
        response = client.get("/ok")
        assert response.headers["X-RateLimit-Remaining-Second"] == "5"
    assert deps.quota.snapshot(project.id)["day"].remaining == 100


def test_a_request_that_never_bound_a_project_carries_no_quota_headers():
    deps, project, _clock = _build(limits=QuotaLimits(5, 100, 1_000))
    anonymous = TestClient(_app(deps, project.id)).get("/anon")
    assert anonymous.status_code == 200
    assert _rate_limit_headers(anonymous) == []
    assert "retry-after" not in anonymous.headers


def test_an_empty_string_secret_is_absent_not_a_live_key():
    """A falsy secret must reject, exactly as ``None`` does.

    THE BUG THIS PINS: guarding only ``if secret is None`` let the
    ordinary host idiom ``vault.get(project_id, "")`` hand back ``""``
    for an unknown or not-yet-provisioned project. An empty HMAC key is
    the maximally guessable one, so a token forged with
    ``signing_secret=""`` verified — granting an attacker-chosen
    ``project_id`` with attacker-chosen scopes, binding
    ``request.state.project_id`` and feeding quota attribution, audit and
    every downstream route. Absent is absent, whether it reads ``None``
    or ``""``.
    """
    deps, project, clock = _build()

    baseline_request = _request(f"Bearer {_mint(project, clock)}")
    with pytest.raises(AuthError) as expected:
        require_user_token(
            dataclasses.replace(deps, signing_secret_for=lambda pid: None),
            baseline_request,
            "accounts:read",
        )
    baseline = str(expected.value)

    for label, resolver in (
        ("dict.get default", {project.id: project.signing_secret}.get),
        ("environ.get default", lambda pid: ""),
    ):
        vault = resolver if label == "environ.get default" else (
            lambda pid, _r=resolver: _r(pid, "")
        )
        forged = mint_token(
            signing_secret="",
            project_id="proj_totally_made_up",
            external_user_id="attacker",
            scopes=("accounts:read", "users:admin"),
            ttl_ms=600_000,
            clock=clock,
            jti="forged",
        )
        scoped = dataclasses.replace(deps, signing_secret_for=vault)
        request = _request(f"Bearer {forged}")
        with pytest.raises(AuthError) as caught:
            require_user_token(scoped, request, "accounts:read")
        assert type(caught.value) is AuthError, label
        assert str(caught.value) == baseline, label
        assert getattr(request.state, "project_id", None) is None, label


def test_webhook_sink_protocol_declares_everything_the_routes_call():
    """The seam must promise every method its consumer uses.

    ``admin.py`` reads ``event_name`` back through ``sink.get_event`` —
    a stored delivery holds only ``event_id``. When ``get_event`` was
    absent from this Protocol, the real WebhookStore still worked, so
    nothing failed; but a host binding a minimal conforming sink would
    have hit ``AttributeError`` at request time. Pin the whole surface.
    """
    import inspect

    from auradefi.api import deps as deps_module
    from auradefi.webhooks.deliver import WebhookStore

    declared = {
        name
        for name in vars(WebhookSink)
        if not name.startswith("__") and callable(getattr(WebhookSink, name, None))
    }
    assert "get_event" in declared

    # every declared method really exists on the shipped implementation
    for name in declared:
        assert hasattr(WebhookStore, name), name

    # and every sink attribute the api package touches is declared
    source = "\n".join(
        p.read_text()
        for p in (Path(deps_module.__file__).parent.rglob("*.py"))
    )
    used = set(re.findall(r"(?:sink|webhooks)\.([a-z_]+)\(", source))
    assert used <= declared | {"emit"}, sorted(used - declared)
