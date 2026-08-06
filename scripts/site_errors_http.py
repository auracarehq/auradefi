"""Two generated reference pages: every exception, and every HTTP endpoint.

Both are derived rather than written, for the same reason: a hand-maintained
list of error types or endpoints is wrong the first time either changes, and
nothing would notice.

* **Errors**: the `auradefi.errors` class tree joined to `api/errors.py`'s
  `STATUS_TABLE`. That table already encodes the deliberate exceptions to
  inheritance (`ScopeError` is 403 under a 401 parent; `CursorError` is 422
  under a 500 parent), so the page documents intent instead of restating an
  MRO a reader could have guessed.

* **HTTP**: from `create_app(...).openapi()`, which needs no server. Plaid's
  anatomy per endpoint: what it needs, what it returns, and a `curl` you can
  paste. The schema is published beside it so a client generator can have it.
"""

from __future__ import annotations

import inspect
import json
from html import escape

from site_render import BLOB, markdown

BASE = "https://api.example.com"


def _error_tree() -> list[tuple[type, int | None]]:
    """Every AuradefiError subclass with its HTTP status, base classes first."""
    from auradefi import errors
    from auradefi.api.errors import STATUS_TABLE, status_for

    classes = [
        value for value in vars(errors).values()
        if inspect.isclass(value) and issubclass(value, errors.AuradefiError)
    ]
    ordered = sorted(classes, key=lambda cls: (len(cls.__mro__), cls.__name__))
    return [(cls, status_for(cls("")) if STATUS_TABLE else None) for cls in ordered]


def errors_html() -> str:
    from auradefi import errors
    from auradefi.api.errors import DOCS_URL_BASE

    rows = []
    for cls, status in _error_tree():
        parents = [base.__name__ for base in cls.__bases__
                   if base is not object and base is not Exception]
        doc = inspect.getdoc(cls) or ""
        first = doc.strip().split("\n")[0] if doc else ""
        rows.append(
            f'<tr id="{cls.__name__.lower()}">'
            f"<td><code>{escape(cls.__name__)}</code></td>"
            f'<td><code>{escape(", ".join(parents) or "none")}</code></td>'
            f"<td>{status if status else 'none'}</td>"
            f"<td>{escape(first)}</td></tr>"
        )
    return f"""<h1>Errors</h1>
<p>Every exception this package raises inherits
<code>auradefi.errors.AuradefiError</code>, so one <code>except</code> clause
catches all of them and nothing else. Catching a narrower type is how you
distinguish a caller mistake from an upstream failure.</p>

<p>The <strong>HTTP status</strong> column is what the API shell returns for
that type. Three of them deliberately disagree with their parent:
<code>ScopeError</code> is 403 though it subclasses a 401,
<code>CursorError</code> is 422 though it subclasses a 500: because the
status describes whose fault it is, not where the class sits.</p>

<p>Two error types are about the offline guarantee rather than your data:
<code>CassetteError</code> and <code>CassetteMissError</code> mean a replayed
recording did not contain a request. In Sandbox that means you asked for
something the recording does not hold: not that a credential is missing. See
<a href="authentication.html">Authentication &amp; keys</a>.</p>

<table><thead><tr><th>Error</th><th>Subclass of</th><th>HTTP</th>
<th>Raised when</th></tr></thead><tbody>{''.join(rows)}</tbody></table>

<h2>Over HTTP</h2>
<p>Every failure is one shape, so a client parses one shape:</p>
<pre class="code">{escape(json.dumps({"error": {
    "type": "ValidationError", "message": "request validation failed",
    "status": 422,
    "docs_url": f"{DOCS_URL_BASE}#validationerror"}}, indent=2))}</pre>
<p><code>docs_url</code> is a link back into this page, at the row for that
type: <code>{escape(DOCS_URL_BASE)}#</code> plus the type name in lower case.
It is built from the exception's own class, so it is right for every type
without a table of URLs to maintain, and a test checks each anchor against the
rows above.</p>
<p>A 409 also carries <code>existing_connection_id</code>, and a 429 carries a
<code>Retry-After</code> header in whole seconds. Source:
<a href="{BLOB}/src/auradefi/errors.py">errors.py</a>,
<a href="{BLOB}/src/auradefi/api/errors.py">api/errors.py</a>.</p>
"""


def _openapi() -> dict:
    """The schema, built offline from a fully-wired app."""
    from auradefi.api.app import create_app
    from auradefi.api.deps import Deps
    from auradefi.chains.registry import ChainRegistry
    from auradefi.clock import FrozenClock
    from auradefi.ledger.backends.memory import MemoryLedger
    from auradefi.tenancy.audit import AuditLog
    from auradefi.tenancy.keys import ApiKeyStore
    from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
    from auradefi.tenancy.store import TenancyStore
    from auradefi.tenancy.tokens import RevocationSet
    from auradefi.webhooks.deliver import WebhookStore

    clock = FrozenClock(1_754_000_000_000)

    class _Holdings:
        def holdings(self, chain_id: str, address: str) -> object:
            raise NotImplementedError

    deps = Deps(
        tenancy=TenancyStore(), keys=ApiKeyStore(),
        quota=QuotaCounter(QuotaLimits(1_000, 10_000, 100_000), clock),
        audit=AuditLog(), revocations=RevocationSet(), ledger=MemoryLedger(),
        webhooks=WebhookStore(), chains=ChainRegistry(), clock=clock,
        signing_secret_for=lambda project_id: None,
        holdings=_Holdings(),
        capabilities={"eip155:1": frozenset({"balances", "transactions", "prices"})},
    )
    return create_app(deps).openapi()


def _fields(schema: dict, components: dict, depth: int = 0) -> str:
    """Plaid-style field list from a JSON Schema object."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = components.get(name, {})
    properties = schema.get("properties") or {}
    if not properties:
        return ""
    required = set(schema.get("required") or ())
    items = []
    for name, spec in properties.items():
        if "$ref" in spec:
            spec = components.get(spec["$ref"].rsplit("/", 1)[-1], {})
        kind = spec.get("type") or "object"
        bits = ["required" if name in required else "optional", str(kind)]
        rendered = ", ".join(bits)
        if "default" in spec:
            rendered += f", default <code>{escape(json.dumps(spec['default']))}</code>"
        description = spec.get("description") or spec.get("title") or ""
        items.append(
            f'<div class="param"><code class="pname">{escape(name)}</code>'
            f'<span class="ptype">{rendered}</span>'
            f'<p class="pdoc">{escape(str(description)) or "none"}</p></div>'
        )
    return '<div class="params">' + "".join(items) + "</div>"


def http_html() -> tuple[str, str]:
    """`(page html, openapi json)` for the HTTP surface."""
    schema = _openapi()
    components = (schema.get("components") or {}).get("schemas") or {}
    chunks = ["<h1>HTTP API</h1>", markdown().render(
        "The library is the product; this is one adapter over it. Responses "
        "use **Plaid's wire format**, so a client that already consumes Plaid "
        "consumes this: `/crypto/sync` returns `added`/`modified`/`removed` "
        "with a cursor, and every amount is a tagged decimal **string**.\n\n"
        "Two credentials, both yours to issue: a **server key** "
        "(`adk_live_…`) your backend holds, and a **short-lived user token** "
        "minted from it for one end user. See "
        "[Authentication & keys](authentication.html).\n\n"
        "A route appears here only if its capability is bound: "
        "`POST /batch/holdings` exists only when a holdings provider is "
        "injected. [Download openapi.json](openapi.json).")]

    for path in sorted(schema.get("paths") or {}):
        for method, operation in sorted((schema["paths"][path]).items()):
            anchor = (method + path).replace("/", "-").replace("{", "").replace("}", "")
            summary = operation.get("summary") or ""
            description = operation.get("description") or ""
            chunks.append(f'<h2 id="{anchor}"><code>{method.upper()} {escape(path)}'
                          f"</code></h2>")
            if summary:
                chunks.append(f"<p><strong>{escape(summary)}</strong></p>")
            if description:
                chunks.append(markdown().render(description))

            body = (((operation.get("requestBody") or {}).get("content") or {})
                    .get("application/json") or {}).get("schema")
            parameters = operation.get("parameters") or []
            if parameters:
                items = []
                for parameter in parameters:
                    spec = parameter.get("schema") or {}
                    kind = spec.get("type") or "string"
                    rendered = ("required" if parameter.get("required") else "optional")
                    items.append(
                        f'<div class="param"><code class="pname">'
                        f'{escape(parameter["name"])}</code>'
                        f'<span class="ptype">{rendered}, {escape(str(kind))} '
                        f'(in {escape(parameter.get("in", "query"))})</span>'
                        f'<p class="pdoc">'
                        f'{escape(str(parameter.get("description") or "none"))}</p></div>')
                chunks.append("<h3>Request fields</h3><div class=\"params\">"
                              + "".join(items) + "</div>")
            if body:
                chunks.append("<h3>Request body</h3>" + _fields(body, components))

            responses = operation.get("responses") or {}
            for status in sorted(responses):
                content = ((responses[status].get("content") or {})
                           .get("application/json") or {}).get("schema")
                if content:
                    chunks.append(f"<h3>Response fields ({status})</h3>"
                                  + (_fields(content, components) or
                                     "<p>Shape depends on the route.</p>"))

            chunks.append(f'<h3>Example</h3><pre class="code">'
                          f"curl -X {method.upper()} '{BASE}{escape(path)}' \\\n"
                          f"  -H 'Authorization: Bearer &lt;token&gt;'</pre>")

    return "\n".join(chunks), json.dumps(schema, indent=2)
