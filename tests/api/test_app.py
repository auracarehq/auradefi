"""api/app.py — the factory, the route table, and what it refuses to be.

Everything runs offline: ``TestClient`` drives the ASGI app in-process,
so the autouse socket guard never sees a connect.

The contract under test is as much about ABSENCE as presence — no
module-level app, no environment read, no ``Depends``, no route defined
by the factory itself, and no capability advertised that the injected
``Deps`` cannot perform.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import auradefi
from auradefi.api.app import ROUTE_MODULES, create_app
from auradefi.api.deps import Deps
from auradefi.chains.registry import ChainRegistry
from auradefi.clock import FrozenClock
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.keys import ApiKeyStore
from auradefi.tenancy.models import Environment, Scope
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import RevocationSet
from auradefi.webhooks.deliver import WebhookStore

NOW = 1_754_000_000_000
API_SRC = Path(__file__).resolve().parents[2] / "src" / "auradefi" / "api"
SHELL_SOURCES = (
    API_SRC / "app.py",
    API_SRC / "routes" / "auth.py",
    API_SRC / "routes" / "connections.py",
    API_SRC / "routes" / "sync.py",
    API_SRC / "routes" / "admin.py",
)

# The advertised surface with every optional capability UNBOUND.
BASE_SURFACE = [
    ("GET", "/connections"),
    ("GET", "/connections/{connection_id}"),
    ("GET", "/coverage"),
    ("GET", "/crypto/sync"),
    ("GET", "/users"),
    ("GET", "/users/me"),
    ("GET", "/webhooks/dead_letter"),
    ("GET", "/webhooks/deliveries"),
    ("GET", "/webhooks/endpoints"),
    ("POST", "/auth/revoke"),
    ("POST", "/auth/token"),
    ("POST", "/connections"),
    ("POST", "/webhooks/deliveries/{delivery_id}/replay"),
    ("POST", "/webhooks/endpoints"),
]


class _CountingClock:
    """Records every read, so 'no I/O at construction' is measurable."""

    def __init__(self) -> None:
        self.reads = 0

    def now_ms(self) -> int:
        self.reads += 1
        return NOW


class _Holdings:
    """A structural HoldingsProvider (api may not import portfolio)."""

    def holdings(self, chain_id: str, address: str) -> object:
        return (chain_id, address)


def _build(clock=None, **overrides):
    """A wired Deps over the real Phase 0-7 collaborators."""
    clock = clock or FrozenClock(NOW)
    tenancy = TenancyStore()
    org = tenancy.create_organisation("acme", clock)
    project = tenancy.create_project(org.id, "main", Environment.TEST, clock)
    vault = {project.id: project.signing_secret}
    deps = Deps(
        tenancy=tenancy,
        keys=ApiKeyStore(),
        quota=QuotaCounter(QuotaLimits(5, 100, 1_000), clock),
        audit=AuditLog(),
        revocations=RevocationSet(),
        ledger=MemoryLedger(),
        webhooks=WebhookStore(),
        chains=ChainRegistry(),
        clock=clock,
        signing_secret_for=vault.get,
        **overrides,
    )
    return deps, project, clock


def _issue(deps, project, *scopes):
    return deps.keys.issue(project.id, Environment.TEST, scopes, deps.clock)


def _advertised(app: FastAPI) -> list[tuple[str, str]]:
    """The OpenAPI surface — what the deployment tells the world it does."""
    return sorted(
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    )


def _mounted(app: FastAPI) -> list[tuple[str, str]]:
    """Every mounted route, advertised or not (docs routes excluded).

    Walks included routers: FastAPI 0.141 keeps an included router as one
    opaque entry in ``app.routes`` rather than flattening its routes in.
    """
    found: list[tuple[str, str]] = []
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        included = getattr(route, "original_router", None)
        if included is not None:
            stack.extend(included.routes)
        elif isinstance(route, APIRoute):
            found.extend((method, route.path) for method in sorted(route.methods))
    return sorted(found)


def _names_used(path: Path) -> set[str]:
    """Every dotted name imported and every attribute/name referenced."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


# --------------------------------------------------------------------------
# the factory


def test_app_metadata_is_pinned():
    deps, _project, _clock = _build()
    app = create_app(deps)
    assert isinstance(app, FastAPI)
    assert app.title == "auradefi"
    # The literal is the point: a half-bump (one file moved, the other not)
    # is what release_check.sh refuses to ship, and this pins the app
    # surface to the same value.
    assert app.version == auradefi.__version__ == "0.1.1"
    assert app.docs_url == "/docs"


def test_create_app_performs_no_io():
    clock = _CountingClock()
    deps, _project, _clock = _build(clock=clock)
    reads_before = clock.reads
    create_app(deps)
    assert clock.reads == reads_before, "create_app read the clock"


def test_two_apps_in_one_process_share_no_state():
    deps_a, project_a, _clock = _build()
    deps_b, project_b, _clock_b = _build()
    _record_a, plaintext_a = _issue(deps_a, project_a, Scope.USERS_ADMIN)
    client_b = TestClient(create_app(deps_b))

    # A key issued into app A's store must not authenticate against app B.
    response = client_b.get("/users", headers={"Authorization": f"Bearer {plaintext_a}"})
    assert response.status_code == 401
    assert project_a.id != project_b.id


def test_route_modules_are_exactly_four_router_factories():
    assert len(ROUTE_MODULES) == 4
    assert [module.__name__.rsplit(".", 1)[-1] for module in ROUTE_MODULES] == [
        "auth",
        "connections",
        "sync",
        "admin",
    ]
    for module in ROUTE_MODULES:
        assert isinstance(module, ModuleType)
        assert callable(module.router)


# --------------------------------------------------------------------------
# the advertised surface (rule #10 on the route table)


def test_unbound_capabilities_are_not_advertised():
    deps, _project, _clock = _build()
    app = create_app(deps)
    assert _advertised(app) == BASE_SURFACE
    assert ("DELETE", "/connections/{connection_id}") not in _advertised(app)
    assert ("POST", "/batch/holdings") not in _advertised(app)


def test_bound_capabilities_are_advertised():
    deps, _project, _clock = _build(
        holdings=_Holdings(), delete_connection=lambda project_id, cid: None
    )
    app = create_app(deps)
    assert _advertised(app) == sorted(
        [
            *BASE_SURFACE,
            ("DELETE", "/connections/{connection_id}"),
            ("POST", "/batch/holdings"),
        ]
    )


def test_the_only_unadvertised_route_is_the_delete_404_stub():
    """An unbound DELETE must 404, not 405 — and 405 is what Starlette

    answers when a path exists for another method, which would confirm
    the capability exists. The stub is the only route in the app that is
    mounted without being advertised.
    """
    deps, _project, _clock = _build()
    app = create_app(deps)
    hidden = sorted(set(_mounted(app)) - set(_advertised(app)))
    assert hidden == [("DELETE", "/connections/{connection_id}")]

    response = TestClient(app).delete("/connections/conn_deadbeefdeadbeef")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "NotFoundError"


# --------------------------------------------------------------------------
# the two installed concerns


def test_error_handlers_are_installed():
    deps, _project, _clock = _build()
    response = TestClient(create_app(deps)).get("/users")
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "type": "AuthError",
            "message": "api key failed authentication",
            "status": 401,
        }
    }


def test_quota_headers_are_installed_on_an_authenticated_response():
    deps, project, _clock = _build()
    _record, plaintext = _issue(deps, project, Scope.USERS_ADMIN)
    response = TestClient(create_app(deps)).get(
        "/users", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit-Second"] == "5"
    assert response.headers["X-RateLimit-Remaining-Second"] == "4"
    assert response.headers["X-RateLimit-Reset-Second"] == "1754000001000"


def test_the_public_route_carries_no_quota_headers():
    deps, _project, _clock = _build()
    response = TestClient(create_app(deps)).get("/coverage")
    assert response.status_code == 200
    assert not [name for name in response.headers if name.lower().startswith("x-ratelimit")]


# --------------------------------------------------------------------------
# what the shell refuses to be


def test_fastapi_depends_is_used_nowhere_in_the_shell():
    offenders = [
        str(path.name)
        for path in SHELL_SOURCES
        if "Depends" in _names_used(path)
    ]
    assert not offenders, (
        "auth is called explicitly at the top of each handler, never injected "
        f"— Depends found in: {offenders}"
    )


def test_the_factory_defines_no_route_and_reads_no_environment():
    used = _names_used(SHELL_SOURCES[0])
    assert not used & {"os", "os.environ", "environ", "getenv", "dotenv"}
    tree = ast.parse(SHELL_SOURCES[0].read_text(encoding="utf-8"))
    decorators = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for _ in node.decorator_list
    ]
    assert not decorators, "app.py declares a decorated handler — it defines routes"


def test_the_factory_holds_no_module_level_app():
    tree = ast.parse(SHELL_SOURCES[0].read_text(encoding="utf-8"))
    calls = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
    ]
    assert not calls, "app.py builds something at import time"


def test_the_shell_starts_no_scheduler():
    """Delivery is host-scheduled (SPEC §8) — no thread, no sleep, no task."""
    banned = {"Thread", "sleep", "create_task", "run_in_executor", "on_event"}
    offenders = sorted(
        f"{path.name}: {sorted(banned & _names_used(path))}"
        for path in SHELL_SOURCES
        if banned & _names_used(path)
    )
    assert not offenders, "\n".join(offenders)


def test_created_apps_are_independent_objects():
    deps_a, _project_a, _clock_a = _build()
    deps_b, _project_b, _clock_b = _build(holdings=_Holdings())
    app_a, app_b = create_app(deps_a), create_app(deps_b)
    assert app_a is not app_b
    assert ("POST", "/batch/holdings") not in _advertised(app_a)
    assert ("POST", "/batch/holdings") in _advertised(app_b)


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_docs_are_served(path):
    deps, _project, _clock = _build()
    response = TestClient(create_app(deps)).get(path)
    assert response.status_code == 200
