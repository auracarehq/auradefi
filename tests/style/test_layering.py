"""SPEC §3.2/§3.3 — the important gate. The layer contract, mechanically
enforced over the real import graph:

  * sources/ may not import positions/; assets/ may not import prices/
  * project/ may not import anything with I/O
  * nothing outside api/ imports a web framework
  * nothing outside ledger/backends/ imports an ORM
  * the domain dependency graph is acyclic
  * foundation flat modules import no domain

A domain absent from ALLOWED_IMPORTS fails loudly: declaring a new domain's
layer is a deliberate act, made here, in review — never an accident.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "auradefi"
PKG = "auradefi"

WEB_FRAMEWORKS = {"fastapi", "starlette", "flask", "django", "sanic", "tornado", "aiohttp"}
ORMS = {"sqlalchemy", "sqlmodel", "peewee", "tortoise"}
HTTP_CLIENTS = {"httpx", "requests", "urllib3"}

# domain -> internal domains it may import (foundation flat modules are
# implicitly importable by everyone; a domain may always import itself).
ALLOWED_IMPORTS: dict[str, set[str]] = {
    "money": set(),
    "chains": set(),
    "testing": set(),
    "assets": {"money", "chains"},
    # `testing` is here for ONE reason: sources/sandbox.py replays the
    # bundled recording through testing/cassettes.py's matcher rather than
    # forking it. That module is shipped, host-facing surface (its own
    # docstring invites hosts to test their integrations with it), not test
    # scaffolding — and two replay matchers with drifting semantics would be
    # worse than this edge. `testing` imports no domain, so it cannot cycle.
    "sources": {"money", "chains", "assets", "testing"},
    "prices": {"money", "chains", "assets", "sources"},
    "decode": {"money", "chains", "assets", "sources", "prices"},
    "positions": {"money", "chains", "assets", "sources", "prices"},
    "portfolio": {"money", "chains", "assets", "sources", "prices"},
    "ledger": {"money", "chains", "assets", "decode", "positions"},
    "accounting": {"money", "chains", "assets", "ledger"},
    "tenancy": {"money", "chains"},
    "project": {"money", "chains", "assets", "decode", "positions", "portfolio", "ledger", "accounting"},
    "embed": {
        "money", "chains", "assets", "sources", "prices", "decode",
        "positions", "portfolio", "ledger", "project",
    },
    "webhooks": {"money", "tenancy", "ledger"},
    "jobs": {
        "money", "chains", "assets", "sources", "prices", "decode",
        "positions", "ledger", "accounting", "tenancy", "webhooks",
    },
    "api": {
        "money", "chains", "assets", "sources", "prices", "decode", "positions",
        "ledger", "accounting", "tenancy", "project", "webhooks", "jobs",
    },
}

# domains allowed to touch an HTTP client (project/ stays pure by omission)
IO_DOMAINS = {"sources", "prices", "testing", "api", "jobs", "webhooks"}

# The root __init__ may lazily export these domains' public entry points
# (SPEC §8 "import, don't call" — `from auradefi import Auradefi`). Every
# other foundation→domain edge stays a violation.
FOUNDATION_LAZY_EXPORTS = {"embed"}


def _domain_of(path: Path) -> str:
    relative = path.relative_to(SRC)
    return "" if len(relative.parts) == 1 else relative.parts[0]


def _module_package(path: Path) -> list[str]:
    return [PKG, *path.relative_to(SRC).parts[:-1]]


def _imports_of(path: Path) -> list[str]:
    """Absolute dotted names imported by the module, relatives resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = _module_package(path)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join(anchor + ([node.module] if node.module else []))
            found.append(base)
            found.extend(f"{base}.{alias.name}" for alias in node.names if base)
    return found


def _edges() -> list[tuple[Path, str, str]]:
    """(file, its domain, imported internal domain) for every internal import."""
    edges = []
    for path in SRC.rglob("*.py"):
        source_domain = _domain_of(path)
        for name in _imports_of(path):
            parts = name.split(".")
            if parts[0] != PKG:
                continue
            target_domain = "" if len(parts) < 2 else parts[1]
            if (SRC / parts[1]).is_dir() if len(parts) > 1 else False:
                edges.append((path, source_domain, target_domain))
            elif len(parts) > 1:
                edges.append((path, source_domain, ""))  # foundation module
    return edges


def _external_offenders(banned: set[str], allowed_domains: set[str] | None, scope: str):
    offenders = []
    for path in SRC.rglob("*.py"):
        domain = _domain_of(path)
        sub = path.relative_to(SRC).parts
        for name in _imports_of(path):
            top = name.split(".")[0]
            if top not in banned:
                continue
            if scope == "web" and domain == "api":
                continue
            if scope == "orm" and len(sub) >= 2 and sub[0] == "ledger" and sub[1] == "backends":
                continue
            if scope == "http" and allowed_domains and domain in allowed_domains:
                continue
            offenders.append(f"{path.relative_to(REPO)} imports {top}")
    return sorted(set(offenders))


def test_every_domain_declares_its_layer():
    undeclared = sorted(
        {
            domain
            for path in SRC.rglob("*.py")
            if (domain := _domain_of(path)) and domain not in ALLOWED_IMPORTS
        }
    )
    assert not undeclared, (
        f"domains missing from ALLOWED_IMPORTS (declare their layer here): {undeclared}"
    )


def test_internal_imports_respect_the_layer_contract():
    violations = []
    for path, source_domain, target_domain in _edges():
        if target_domain == "" or source_domain == target_domain:
            continue
        if source_domain == "":
            if path.name == "__init__.py" and target_domain in FOUNDATION_LAZY_EXPORTS:
                continue
            violations.append(
                f"{path.relative_to(REPO)}: foundation module imports domain "
                f"'{target_domain}' — foundation imports nothing"
            )
        elif target_domain not in ALLOWED_IMPORTS.get(source_domain, set()):
            violations.append(
                f"{path.relative_to(REPO)}: '{source_domain}' may not import "
                f"'{target_domain}'"
            )
    assert not violations, "\n".join(sorted(set(violations)))


def test_no_web_framework_outside_api():
    offenders = _external_offenders(WEB_FRAMEWORKS, None, "web")
    assert not offenders, "web frameworks only under api/:\n" + "\n".join(offenders)


def test_no_orm_outside_ledger_backends():
    offenders = _external_offenders(ORMS, None, "orm")
    assert not offenders, "ORMs only under ledger/backends/:\n" + "\n".join(offenders)


def test_http_clients_only_in_io_domains():
    offenders = _external_offenders(HTTP_CLIENTS, IO_DOMAINS, "http")
    assert not offenders, (
        f"HTTP clients only under {sorted(IO_DOMAINS)}:\n" + "\n".join(offenders)
    )


def test_observed_domain_graph_is_acyclic():
    graph: dict[str, set[str]] = {}
    for _, source_domain, target_domain in _edges():
        if source_domain and target_domain and source_domain != target_domain:
            graph.setdefault(source_domain, set()).add(target_domain)

    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in done:
            return
        assert node not in visiting, f"import cycle: {' -> '.join([*trail, node])}"
        visiting.add(node)
        for neighbour in graph.get(node, set()):
            visit(neighbour, [*trail, node])
        visiting.discard(node)
        done.add(node)

    for start in list(graph):
        visit(start, [])
