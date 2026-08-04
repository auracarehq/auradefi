"""api/errors.py — the pinned status table and the ONE exception handler.

The table is DECISIONS "HTTP error table", verbatim and ordered. The two
boundary facts this file exists to keep honest:

* a route raising ``ValueError`` is NOT converted — a bug is not an API
  contract, and TestClient re-raises it;
* ``existing_connection_id`` (SPEC §7.1, Vezgo-verbatim) is emitted by the
  handler from ``ConflictError.existing_id``, never by route-level magic,
  and only when that id is actually a connection id.

Offline throughout: TestClient drives the ASGI app in-process.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from auradefi.api.deps import Deps, consume_quota
from auradefi.api.errors import (
    STATUS_TABLE,
    VALIDATION_MESSAGE,
    error_body,
    install_error_handlers,
    status_for,
)
from auradefi.chains.registry import ChainRegistry
from auradefi.clock import FrozenClock
from auradefi.errors import (
    AuradefiError,
    AuthError,
    CaipParseError,
    ConfigError,
    ConflictError,
    CursorError,
    DecodeError,
    NotFoundError,
    QuotaExceededError,
    ScopeError,
    SourceError,
    TenantIsolationError,
    TokenExpiredError,
    TokenRevokedError,
    UnknownChainError,
    ValidationError,
)
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.keys import ApiKeyStore
from auradefi.tenancy.models import Environment
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import RevocationSet

NOW = 1_754_000_000_000
ERRORS_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "auradefi" / "api" / "errors.py"
)

# DECISIONS "HTTP error table" — order is part of the contract: every
# subclass precedes its base, so a table walk and an MRO walk agree.
PINNED_TABLE = [
    (ValidationError, 422),
    (CaipParseError, 422),
    (CursorError, 422),
    (ScopeError, 403),
    (TokenExpiredError, 401),
    (TokenRevokedError, 401),
    (AuthError, 401),
    (NotFoundError, 404),
    (ConflictError, 409),
    (QuotaExceededError, 429),
    (SourceError, 502),
    (AuradefiError, 500),
]


class _Sink:
    def register_endpoint(self, project_id, url, events, clock):  # noqa: ANN001
        return None

    def endpoints(self, project_id):  # noqa: ANN001
        return ()

    def emit(self, project_id, name, data, clock):  # noqa: ANN001
        return None

    def deliveries(self, project_id):  # noqa: ANN001
        return ()

    def dead_letter(self, project_id):  # noqa: ANN001
        return ()

    def get_delivery(self, project_id, delivery_id):  # noqa: ANN001
        return None


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chain: str


def _build(limits: QuotaLimits | None = None):
    clock = FrozenClock(NOW)
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
    return deps, project


def _app(deps: Deps, project_id: str) -> FastAPI:
    app = FastAPI()

    @app.get("/not-found")
    def _not_found() -> dict[str, str]:
        raise NotFoundError("connection not found: 'conn_x'")

    @app.get("/conflict")
    def _conflict() -> dict[str, str]:
        raise ConflictError(
            "connection already exists: 'conn_deadbeefdeadbeef'",
            existing_id="conn_deadbeefdeadbeef",
        )

    @app.get("/scope")
    def _scope() -> dict[str, str]:
        raise ScopeError("missing required scope: users:admin")

    @app.get("/expired")
    def _expired() -> dict[str, str]:
        raise TokenExpiredError("token expired")

    @app.get("/cursor")
    def _cursor() -> dict[str, str]:
        raise CursorError("malformed cursor: 'nope'")

    @app.get("/source")
    def _source() -> dict[str, str]:
        raise SourceError("explorer returned a malformed payload")

    @app.get("/decode")
    def _decode() -> dict[str, str]:
        raise DecodeError("rows for one transaction disagree")

    @app.get("/boom")
    def _boom() -> dict[str, str]:
        raise ValueError("a bug is not an API contract")

    @app.get("/throttled")
    def _throttled(request: Request) -> dict[str, str]:
        request.state.project_id = project_id
        consume_quota(deps, project_id)
        consume_quota(deps, project_id)
        return {}

    @app.get("/throttled-anon")
    def _throttled_anon() -> dict[str, str]:
        raise QuotaExceededError("quota exceeded in the 'second' window")

    @app.post("/echo")
    def _echo(payload: _Payload) -> dict[str, str]:
        return {"chain": payload.chain}

    install_error_handlers(app, deps)
    return app


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
# the table


def test_status_table_is_the_pinned_twelve_in_the_pinned_order():
    assert list(STATUS_TABLE.items()) == PINNED_TABLE
    assert len(STATUS_TABLE) == 12
    # The three that MUST precede their base, because they disagree with it:
    # ScopeError is 403 under a 401 base. (CaipParseError sits after
    # ValidationError in the pinned order and may: both are 422.)
    order = list(STATUS_TABLE)
    for derived in (ScopeError, TokenExpiredError, TokenRevokedError):
        assert order.index(derived) < order.index(AuthError), (
            f"{derived.__name__} must precede AuthError"
        )
    assert STATUS_TABLE[CaipParseError] == STATUS_TABLE[ValidationError] == 422


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ValidationError("bad input"), 422),
        (CaipParseError("bad caip"), 422),
        (CursorError("bad cursor"), 422),
        (ScopeError("no scope"), 403),
        (AuthError("nope"), 401),
        (TokenExpiredError("expired"), 401),
        (TokenRevokedError("revoked"), 401),
        (NotFoundError("gone"), 404),
        (ConflictError("dup"), 409),
        (QuotaExceededError("slow down"), 429),
        (SourceError("explorer down"), 502),
        (AuradefiError("unknown"), 500),
        (DecodeError("inconsistent"), 500),
        (TenantIsolationError("crossed"), 500),
        (UnknownChainError("eip155:99999"), 500),
        (ConfigError("missing key"), 500),
    ],
)
def test_status_for_walks_the_mro(exc, expected):
    assert status_for(exc) == expected


def test_the_deliberate_mro_overrides():
    # ScopeError is an AuthError but 403, not 401.
    assert issubclass(ScopeError, AuthError)
    assert status_for(ScopeError("x")) == 403
    assert status_for(AuthError("x")) == 401
    # CursorError is a LedgerError but 422 — a mistyped ?cursor= is the
    # client's fault; TenantIsolationError, its sibling, stays 500.
    assert status_for(CursorError("x")) == 422
    assert status_for(TenantIsolationError("x")) == 500


# --------------------------------------------------------------------------
# the body


def test_error_body_is_one_error_object_with_three_keys():
    assert error_body(NotFoundError("connection not found: 'conn_x'")) == {
        "error": {
            "type": "NotFoundError",
            "message": "connection not found: 'conn_x'",
            "status": 404,
        }
    }


def test_conflict_renders_existing_connection_id_only_for_connection_ids():
    assert error_body(
        ConflictError("dup", existing_id="conn_deadbeefdeadbeef")
    ) == {
        "error": {
            "type": "ConflictError",
            "message": "dup",
            "status": 409,
            "existing_id": "conn_deadbeefdeadbeef",
            "existing_connection_id": "conn_deadbeefdeadbeef",
        }
    }
    assert error_body(ConflictError("dup", existing_id="whe_abc")) == {
        "error": {
            "type": "ConflictError",
            "message": "dup",
            "status": 409,
            "existing_id": "whe_abc",
        }
    }
    assert error_body(ConflictError("dup")) == {
        "error": {"type": "ConflictError", "message": "dup", "status": 409}
    }


# --------------------------------------------------------------------------
# installation


def test_exactly_one_auradefi_handler_is_registered():
    deps, project = _build()
    app = FastAPI()
    assert not [
        key
        for key in app.exception_handlers
        if isinstance(key, type) and issubclass(key, AuradefiError)
    ]
    install_error_handlers(app, deps)
    registered = [
        key
        for key in app.exception_handlers
        if isinstance(key, type) and issubclass(key, AuradefiError)
    ]
    assert registered == [AuradefiError]
    assert Exception not in app.exception_handlers
    assert (
        app.exception_handlers[RequestValidationError]
        is not request_validation_exception_handler
    )
    assert project.id  # the container is wired, not inspected


def test_errors_imports_neither_portfolio_nor_webhooks_nor_an_orm():
    banned = ("auradefi.portfolio", "auradefi.webhooks", "sqlalchemy", "sqlmodel")
    offenders = [
        name
        for name in _imported_names(ERRORS_SOURCE)
        if any(name == b or name.startswith(f"{b}.") for b in banned)
    ]
    assert not offenders, f"api/errors.py must stay independent: {offenders}"


# --------------------------------------------------------------------------
# over the wire


def test_each_pinned_error_renders_its_pinned_status_and_body():
    deps, project = _build()
    client = TestClient(_app(deps, project.id))

    not_found = client.get("/not-found")
    assert not_found.status_code == 404
    assert not_found.headers["content-type"].startswith("application/json")
    assert not_found.json() == {
        "error": {
            "type": "NotFoundError",
            "message": "connection not found: 'conn_x'",
            "status": 404,
        }
    }

    conflict = client.get("/conflict")
    assert conflict.status_code == 409
    assert conflict.json() == {
        "error": {
            "type": "ConflictError",
            "message": "connection already exists: 'conn_deadbeefdeadbeef'",
            "status": 409,
            "existing_id": "conn_deadbeefdeadbeef",
            "existing_connection_id": "conn_deadbeefdeadbeef",
        }
    }

    for path, status, kind in (
        ("/scope", 403, "ScopeError"),
        ("/expired", 401, "TokenExpiredError"),
        ("/cursor", 422, "CursorError"),
        ("/source", 502, "SourceError"),
        ("/decode", 500, "DecodeError"),
    ):
        response = client.get(path)
        assert response.status_code == status, path
        assert response.json()["error"]["status"] == status, path
        assert response.json()["error"]["type"] == kind, path
        assert set(response.json()) == {"error"}, path


def test_a_route_raising_value_error_is_not_converted():
    deps, project = _build()
    client = TestClient(_app(deps, project.id))
    with pytest.raises(ValueError, match="a bug is not an API contract"):
        client.get("/boom")


def test_quota_exceeded_carries_retry_after_when_a_project_is_bound():
    deps, project = _build(limits=QuotaLimits(1, 10, 100))
    throttled = TestClient(_app(deps, project.id)).get("/throttled")
    assert throttled.status_code == 429
    assert throttled.headers["Retry-After"] == "1"
    assert throttled.json()["error"]["type"] == "QuotaExceededError"
    assert throttled.json()["error"]["status"] == 429


def test_quota_exceeded_without_a_bound_project_carries_no_retry_after():
    deps, project = _build(limits=QuotaLimits(1, 10, 100))
    anonymous = TestClient(_app(deps, project.id)).get("/throttled-anon")
    assert anonymous.status_code == 429
    assert "retry-after" not in anonymous.headers


def test_request_validation_error_is_reshaped_into_the_same_body():
    deps, project = _build()
    client = TestClient(_app(deps, project.id))

    assert client.post("/echo", json={"chain": "eip155:1"}).json() == {
        "chain": "eip155:1"
    }

    rejected = client.post("/echo", json={"chain": "eip155:1", "nope": 1})
    assert rejected.status_code == 422
    body = rejected.json()
    assert set(body) == {"error"}
    assert "detail" not in body
    assert body["error"]["type"] == "ValidationError"
    assert body["error"]["status"] == 422
    assert body["error"]["message"] == VALIDATION_MESSAGE
    assert VALIDATION_MESSAGE == "request validation failed"
    assert isinstance(body["error"]["details"], list)
    assert body["error"]["details"]


def test_a_missing_required_field_is_also_reshaped():
    deps, project = _build()
    rejected = TestClient(_app(deps, project.id)).post("/echo", json={})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["type"] == "ValidationError"
    assert rejected.json()["error"]["details"]
