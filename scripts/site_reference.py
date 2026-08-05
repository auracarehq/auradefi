"""Plaid-shaped API reference, generated from the code itself.

Plaid documents an endpoint as: description, then **request fields**, then a
code sample, then **response fields**, then a JSON example, with every field
written `required, string` or `optional, integer, Default: 100`. The value is
not the styling; it is that a reader can answer "what do I pass and what do I
get back" without reading prose.

The equivalent for a library is per-callable: signature, description,
**Parameters**, **Returns** (with the returned type's own fields documented
the same way), **Raises**, and a runnable snippet. That is what this module
emits.

WHAT IS DERIVED AND WHAT IS WRITTEN. Signatures, types, defaults, dataclass
fields and NamedTuple fields come from `inspect` and the annotations, so they
cannot drift. Per-parameter prose cannot be derived, the docstrings here are
narrative rather than `:param:`-annotated, so it lives in :data:`PARAM_DOCS`,
and `tests/style/test_reference_is_generated.py` fails if a documented
parameter is not in the real signature. Raised exceptions are scraped from
the docstring, which by house rule states the error contract.

The curated symbol list is the point, not a limitation. An auto-dump of 120
modules buries the 25 things a host actually touches; this file names them in
the order a host meets them.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import re
from html import escape

from site_render import BLOB, anchored, markdown

#: The public surface, grouped and ordered the way a host meets it.
SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Getting started", "The two factories and the object they return.", (
        "auradefi.embed.facade:Auradefi",
        "auradefi.embed.handle:UserHandle",
        "auradefi.config:Settings",
    )),
    ("Ports you implement", "Bring your own: each is a structural Protocol.", (
        "auradefi.ledger.port:LedgerPort",
        "auradefi.embed.state:SyncStatePort",
        "auradefi.portfolio.holdings:BalanceSource",
        "auradefi.embed.sync:PageFetcher",
        "auradefi.prices.inquirer:PriceOracle",
    )),
    ("Ports we ship", "Defaults, so you only implement what you want to.", (
        "auradefi.sources.evm.source:EtherscanSource",
        "auradefi.sources.evm.etherscan:EtherscanV2",
        "auradefi.prices.oracles.defillama:DefiLlamaOracle",
        "auradefi.prices.inquirer:Inquirer",
        "auradefi.ledger.backends.memory:MemoryLedger",
        "auradefi.ledger.backends.sqlmodel:SqlModelLedger",
        "auradefi.embed.state:MemorySyncState",
        "auradefi.clock:SystemClock",
        "auradefi.clock:FrozenClock",
    )),
    ("Values on the wire", "What you get back, field by field.", (
        "auradefi.money.quantity:Quantity",
        "auradefi.money.fiat:Money",
        "auradefi.portfolio.models:HoldingsReport",
        "auradefi.portfolio.models:Holding",
        "auradefi.embed.models:SyncReport",
        "auradefi.embed.models:ConnectionSyncReport",
        "auradefi.embed.models:ConnectionRecord",
        "auradefi.ledger.models:LedgerTransaction",
        "auradefi.ledger.models:Entry",
        "auradefi.ledger.models:SyncPage",
    )),
    ("Accounting", "Cost basis and PnL at any instant.", (
        "auradefi.accounting.pnl:pnl_at",
        "auradefi.accounting.lots:derive_events",
        "auradefi.accounting.report:PnLReport",
        "auradefi.accounting.report:TaxLot",
    )),
    ("Webhooks", "Signing, delivery and replay.", (
        "auradefi.webhooks.sign:sign",
        "auradefi.webhooks.sign:verify_signature",
        "auradefi.webhooks.deliver:Deliverer",
        "auradefi.webhooks.replay:replay",
    )),
)

#: Per-parameter prose, which no amount of introspection can invent.
#: A name here that is not in the real signature fails the reference gate.
PARAM_DOCS: dict[str, dict[str, str]] = {
    "Auradefi.__init__": {
        "ledger": "Where transactions are stored. Four methods, tenant-scoped.",
        "source": "Your chain data. Must satisfy BOTH seams: `balances` and "
                  "`fetch_txlist`: or binding raises immediately.",
        "prices": "Your price feed. Returning nothing for an asset is allowed "
                  "and means unpriced, never zero.",
        "clock": "`None` means `SystemClock()`. Time is a port so quota "
                 "windows and throttling are testable.",
        "settings": "`None` means `Settings()`. Carries the sync interval and "
                    "the project id that tenant ids derive under.",
        "sync_state": "Connections and cursors. `None` means in-process, which "
                      "forgets every connection on restart.",
        "decoder": "Row-format seam. `None` binds the EVM txlist decoder lazily.",
        "sync_page_size": "How many rows to ask a source for per page.",
    },
    "Auradefi.sandbox": {
        "connect": "Whether to return with the sandbox address already "
                   "connected. `False` to call `connect_address` yourself.",
        "overrides": "Any port, by keyword, replacing that default.",
    },
    "Auradefi.from_env": {
        "overrides": "Any port, by keyword. `ledger=` is the one most hosts "
                     "set, since the default is not durable.",
    },
    "Auradefi.sync": {
        "budget": "Maximum source pages this ONE call may spend across every "
                  "connection. Cursors make the next call resume.",
    },
    "Auradefi.user": {
        "external_user_id": "Your opaque id for the person. The tenant id is "
                            "derived from it; `@` is refused because this is "
                            "bearer-equivalent.",
    },
    "UserHandle.connect_address": {
        "chain": "CAIP-2 id, e.g. `eip155:1`. A vendor name like `ethereum` "
                 "is refused, as is a chain the registry does not hold.",
        "address": "Checksummed or lowercase; stored lowercased. The `0x` "
                   "prefix is not case-insensitive.",
    },
    "EtherscanSource.__init__": {
        "client": "Injected `httpx.Client`. Nothing is opened here.",
        "api_key": "`None` omits the `apikey` param entirely: the keyless "
                   "tier, not an empty key.",
        "base_url": "Override to point at a proxy or a compatible endpoint.",
        "page_size": "Token-discovery page size for `balances`.",
    },
    "EtherscanSource.from_key": {
        "api_key": "Optional. One key covers every `eip155:*` chain.",
        "timeout_s": "Applied to the client this builds for you.",
        "base_url": "Override to point at a proxy.",
        "page_size": "Token-discovery page size for `balances`.",
    },
    "EtherscanSource.fetch_txlist": {
        "chain_id": "CAIP-2 id; converted to Etherscan's numeric `chainid`.",
        "address": "The account whose history this page covers.",
        "start_block": "Inclusive lower bound the ENGINE chose.",
        "end_block": "Inclusive upper bound the ENGINE chose.",
        "page": "1-based page within that window.",
        "offset": "Rows per page. A shorter page means the window drained.",
        "sort": "`asc` or `desc`. The engine anchors desc and walks live asc.",
    },
    "pnl_at": {
        "events": "Acquisitions and disposals, in any order.",
        "method": "Costing method.",
        "at_ms": "The instant to answer for. Any instant is answerable; "
                 "nothing is pre-computed.",
        "marks": "Prices to value open lots at. Absent means unrealised is "
                 "not computed for that asset.",
    },
    "verify_signature": {
        "secret": "The endpoint secret, shown once at registration.",
        "timestamp_ms": "The `X-Auradefi-Timestamp` header value.",
        "body": "The RAW request body. Re-serialising breaks the signature.",
        "signature": "The `X-Auradefi-Signature` header value.",
        "now_ms": "Your current time, for the staleness window.",
    },
}

#: Enum-ish parameters worth spelling out, Plaid's "Possible values:".
POSSIBLE_VALUES: dict[str, str] = {
    "pnl_at.method": '"fifo", "lifo", "hifo", "acb"',
    "EtherscanSource.fetch_txlist.sort": '"asc", "desc"',
}

_RAISES = re.compile(r"\b([A-Z][A-Za-z]*(?:Error|Miss))\b")
_ERROR_LINK = "../errors.html"


def _load(target: str) -> tuple[object, str]:
    module_name, _, qualname = target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, qualname), qualname


#: RST inline literal: these docstrings predate the site and use ``x``.
_LITERAL = re.compile(r"``([^`]+)``")
#: A Sphinx role: :func:`~pkg.mod.name` / :class:`Name`.
_ROLE = re.compile(r":(?:func|class|meth|data|attr|mod|exc):`~?([^`]+)`")
_CODE_BLOCK = re.compile(r"\.\. code-block:: \w+\n+")


def _clean(text: str | None) -> str:
    """A docstring as markdown: dedented, with RST markup translated.

    `inspect.cleandoc` rather than `textwrap.dedent`, because a docstring's
    first line carries no indentation and every later line does: dedent
    finds no common prefix and leaves the body indented, which markdown
    then renders as a code block.
    """
    body = inspect.cleandoc(text or "")
    body = _CODE_BLOCK.sub("", body)
    body = _ROLE.sub(lambda match: f"`{match.group(1).rsplit('.', 1)[-1]}`", body)
    return _LITERAL.sub(lambda match: f"`{match.group(1)}`", body).strip()


def _type_of(parameter: inspect.Parameter) -> str:
    if parameter.annotation is inspect.Parameter.empty:
        return "any"
    annotation = parameter.annotation
    return annotation if isinstance(annotation, str) else getattr(
        annotation, "__name__", str(annotation))


def _field_rows(obj: object) -> list[tuple[str, str, str]]:
    """`(name, type, note)` for a dataclass, NamedTuple or Protocol."""
    rows: list[tuple[str, str, str]] = []
    if dataclasses.is_dataclass(obj):
        for field in dataclasses.fields(obj):  # type: ignore[arg-type]
            annotation = field.type
            name = annotation if isinstance(annotation, str) else getattr(
                annotation, "__name__", str(annotation))
            note = "" if field.default is dataclasses.MISSING else f"default {field.default!r}"
            rows.append((field.name, name, note))
        return rows
    annotations = getattr(obj, "__annotations__", {})
    for name, annotation in annotations.items():
        rendered = annotation if isinstance(annotation, str) else getattr(
            annotation, "__name__", str(annotation))
        rows.append((name, rendered, ""))
    return rows


def _properties(obj: object) -> list[tuple[str, str, str]]:
    """Read-only properties, which behave like fields to a caller."""
    return [
        (name, "property", _clean(getattr(value, "__doc__", "")).split("\n")[0])
        for name, value in vars(obj).items()
        if isinstance(value, property) and not name.startswith("_")
    ]


def _fields_block(title: str, rows: list[tuple[str, str, str]]) -> str:
    if not rows:
        return ""
    items = "".join(
        f'<div class="field"><code class="fname">{escape(name)}</code>'
        f'<span class="ftype">{escape(kind)}</span>'
        + (f'<span class="fnote">{escape(note)}</span>' if note else "")
        + "</div>"
        for name, kind, note in rows
    )
    return f'<h3>{escape(title)}</h3><div class="fields">{items}</div>'


def _params_block(func: object, key: str) -> str:
    try:
        signature = inspect.signature(func)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    docs = PARAM_DOCS.get(key, {})
    items = []
    for name, parameter in signature.parameters.items():
        if name in {"self", "cls"}:
            continue
        required = parameter.default is inspect.Parameter.empty
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            required = False
        bits = ["required" if required else "optional", _type_of(parameter)]
        rendered = ", ".join(bits)
        if not required and parameter.default is not inspect.Parameter.empty:
            rendered += f", default <code>{escape(repr(parameter.default))}</code>"
        possible = POSSIBLE_VALUES.get(f"{key.split('.')[-1]}.{name}") or \
            POSSIBLE_VALUES.get(f"{key}.{name}")
        note = docs.get(name, "")
        extra = f"<br>Possible values: <code>{escape(possible)}</code>" if possible else ""
        items.append(
            f'<div class="param"><code class="pname">{escape(name)}</code>'
            f'<span class="ptype">{rendered}</span>'
            f'<p class="pdoc">{markdown().renderInline(note) if note else "none"}{extra}</p>'
            "</div>"
        )
    if not items:
        return ""
    return '<h3>Parameters</h3><div class="params">' + "".join(items) + "</div>"


def _raises_block(doc: str) -> str:
    names = sorted({name for name in _RAISES.findall(doc) if name.endswith(("Error", "Miss"))})
    if not names:
        return ""
    links = ", ".join(
        f'<a href="{_ERROR_LINK}#{name.lower()}"><code>{escape(name)}</code></a>'
        for name in names
    )
    return f'<h3>Raises</h3><p class="raises">{links}</p>'


def _signature_of(func: object) -> str:
    """`(a: int, b: str = "x")`, without the quotes PEP 563 adds."""
    try:
        rendered = str(inspect.signature(func))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    return rendered.replace("'", "")


def _member_html(owner: str, name: str, member: object) -> str:
    doc = _clean(getattr(member, "__doc__", ""))
    signature = _signature_of(member)
    key = f"{owner}.{name}"
    anchor = f"{owner}.{name}".replace(".", "-").lower()
    return (
        f'<div class="member" id="{anchor}">'
        f"<h2><code>{escape(name)}</code></h2>"
        f'<pre class="sig">{escape(name + signature)}</pre>'
        f"{markdown().render(doc)}"
        f"{_params_block(member, key)}"
        f"{_raises_block(doc)}"
        "</div>"
    )


def symbol_page(target: str) -> tuple[str, str, str, list]:
    """`(page path, title, body html, outline)` for one public symbol."""
    obj, qualname = _load(target)
    module_name = target.partition(":")[0]
    doc = _clean(getattr(obj, "__doc__", ""))
    source_path = module_name.replace(".", "/") + ".py"

    chunks = [
        f"<h1><code>{escape(qualname)}</code></h1>",
        f'<p class="meta">{escape(module_name)} · '
        f'<a href="{BLOB}/src/{source_path}">source</a></p>',
        markdown().render(doc),
    ]

    if inspect.isclass(obj):
        rows = _field_rows(obj) + _properties(obj)
        chunks.append(_fields_block("Fields", rows))
        for name, member in vars(obj).items():
            if name.startswith("_") and name != "__init__":
                continue
            if isinstance(member, property):
                continue
            unwrapped = member.__func__ if isinstance(member, classmethod) else member
            if not callable(unwrapped):
                continue
            chunks.append(_member_html(qualname, name, unwrapped))
    else:
        chunks.append(_params_block(obj, qualname))
        chunks.append(_raises_block(doc))
        returns = inspect.signature(obj).return_annotation  # type: ignore[arg-type]
        if returns is not inspect.Signature.empty:
            rendered = returns if isinstance(returns, str) else getattr(
                returns, "__name__", str(returns))
            chunks.append(f"<h3>Returns</h3><p><code>{escape(rendered)}</code></p>")

    body, outline = anchored("\n".join(part for part in chunks if part))
    return f"reference/{qualname}.html", qualname, body, outline


def index_html() -> str:
    """The reference landing page: every symbol, grouped, one line each."""
    chunks = ["<h1>API reference</h1>",
              "<p>The surface a host actually touches, in the order it meets "
              "it. Signatures, types, defaults and field lists are generated "
              "from the code, so they cannot drift from it.</p>"]
    for title, blurb, targets in SECTIONS:
        chunks.append(f"<h2>{escape(title)}</h2><p>{escape(blurb)}</p>")
        rows = []
        for target in targets:
            obj, qualname = _load(target)
            summary = _clean(getattr(obj, "__doc__", "")).split("\n")[0]
            rows.append(
                f'<tr><td><a href="{qualname}.html"><code>'
                f"{escape(qualname)}</code></a></td>"
                f"<td>{escape(summary)}</td></tr>"
            )
        chunks.append("<table><tbody>" + "".join(rows) + "</tbody></table>")
    return "\n".join(chunks)


def targets() -> list[str]:
    return [target for _, _, group in SECTIONS for target in group]
