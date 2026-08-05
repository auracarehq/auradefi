"""Dependency container, auth resolution, quota headers (SPEC §7.1–§7.3).

Rule #11 made structural: the HTTP shell owns no state. Everything a route
needs arrives in one frozen :class:`Deps` — no module globals, no import-time
I/O, no singletons. Two apps in one process never share a tenant store.

Collaborators are declared as *structural* Protocols rather than imported,
deliberately:

* :class:`HoldingsProvider` — ``ALLOWED_IMPORTS['api']`` omits ``portfolio``
  (tests/style/test_layering.py). ``portfolio.holdings.HoldingsService``
  conforms without this module knowing it exists. The precedent is
  ``portfolio/holdings.py``'s own ``BalanceSource``.
* :class:`WebhookSink` — re-exported from ``api/sinks.py``, where it moved
  when stating its return shapes honestly outgrew this module's line
  budget (RELEASE_0.1.1 §5 Wave C). Imported here so ``from
  auradefi.api.deps import WebhookSink`` keeps working; the seam itself,
  and the row Protocols a host implements, live over there.

``signing_secret_for`` exists because Phase 2's committed ``TenancyStore``
exposes no project getter and this phase may not edit Phase 2 files: the
host hands us ``project_id -> signing secret | None``.

Pinned in docs/internal/DECISIONS.md ("Quota headers"): nine headers,
``X-RateLimit-{Limit,Remaining,Reset}-{Second,Day,Month}``, every value a
decimal string, ``Reset`` a MS-EPOCH int (§4.4 ms-everywhere) while
``Retry-After`` stays in whole seconds per RFC 9110.

``api`` is the only domain permitted to import a web framework.
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from fastapi import FastAPI, Request, Response

from auradefi.api.sinks import WebhookSink
from auradefi.chains.registry import ChainRegistry
from auradefi.clock import Clock
from auradefi.errors import NotFoundError, ScopeError
from auradefi.ledger.port import LedgerPort
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.keys import ApiKeyStore, has_scope
from auradefi.tenancy.models import ApiKey, EndUser, Scope
from auradefi.tenancy.quota import QuotaCounter, WindowSnapshot
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import (
    RevocationSet,
    TokenClaims,
    require_scope,
    verify_token,
)

#: The three quota windows, in header order (DECISIONS "Quota windows").
_WINDOWS = ("second", "day", "month")

#: Longest bearer credential this shell decodes at all. Every claim of the
#: pinned wire form is bounded — 36-char header, 43-char signature, 13-digit
#: ms-epoch ``iat``/``exp``, 21-char ``project_id``, 32-hex ``jti``, all FOUR
#: ``Scope`` members (an omitted ``scopes`` mints every scope the key holds,
#: so ``sync:trigger`` counts) and a 128-char ``external_user_id`` — so the
#: largest token this system can MINT is 36+1+456+1+43 = 537 chars, and 1 KiB
#: is ~1.9x that. Beyond it NOTHING is decoded: 26 KB of nested arrays cost a
#: worker a 10,000-frame RecursionError unwind and a leaked stack trace, per
#: request, unauthenticated (§4 #34).
MAX_TOKEN_CHARS = 1_024


@runtime_checkable
class HoldingsProvider(Protocol):
    """Structural seam onto holdings assembly (api may not import portfolio)."""

    def holdings(self, chain_id: str, address: str) -> object:
        """Priced holdings for ``address`` on ``chain_id``."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Deps:
    """Everything the HTTP shell needs, injected once at app construction.

    Frozen and slotted: a route can read a collaborator but can never
    rebind one mid-request. Constructing a ``Deps`` performs NO I/O and
    touches no collaborator — it is a record, not a bootstrap.

    ``signing_secret_for(project_id)`` returns that project's JWT signing
    secret, or ``None`` when the project is unknown. An unknown project
    must never become a 404 — whether the resolver returns ``None`` or
    raises the way this repo's other lookups do (see
    :func:`_signing_secret` and :func:`require_user_token`).

    ``trusted_proxy_hops`` is how many rightmost ``X-Forwarded-For`` hops
    this deployment's own proxies append, and so how far back a client IP
    may be trusted. It defaults to **0**: no proxy is trusted and the
    socket peer is the only verified source. This is the injection point
    for ``Settings.trusted_proxy_hops``; a host that terminates TLS behind
    one proxy passes 1. An audit row attributed to a caller-supplied
    header is permanently wrong and indistinguishable from a real one, so
    the default never reads the header at all.
    """

    tenancy: TenancyStore
    keys: ApiKeyStore
    quota: QuotaCounter
    audit: AuditLog
    revocations: RevocationSet
    ledger: LedgerPort
    webhooks: WebhookSink
    chains: ChainRegistry
    clock: Clock
    signing_secret_for: Callable[[str], str | None]
    holdings: HoldingsProvider | None = None
    delete_connection: Callable[[str, str], None] | None = None
    capabilities: Mapping[str, frozenset[str]] = field(default_factory=dict)
    token_ttl_ms: int = 600_000
    sync_limit_default: int = 100
    sync_limit_max: int = 500
    batch_max_items: int = 100
    trusted_proxy_hops: int = 0


def _bearer_credential(request: Request) -> str:
    """The ``Authorization: Bearer <credential>`` value, ``""`` if absent.

    Missing header, non-``Bearer`` scheme and empty credential all collapse
    to ``""``, which no store and no verifier accepts.
    """
    header = request.headers.get("authorization", "")
    scheme, _, credential = header.partition(" ")
    return credential.strip() if scheme.lower() == "bearer" else ""


def _peek_project_id(token: str) -> str | None:
    """The UNVERIFIED ``project_id`` claim, purely to select a secret.

    Never raises, never trusts: anything but a three-segment token of at
    most :data:`MAX_TOKEN_CHARS` characters whose payload is a JSON object
    with a ``str`` ``project_id`` reads ``None``, and every claim is
    re-checked by ``tokens.verify_token``.

    The bound is applied BEFORE any decode, and ``RecursionError`` joins
    the caught parse errors — it is a ``RuntimeError``, so nested arrays
    escaped this helper, against its own contract (§4 #34).
    """
    if len(token) > MAX_TOKEN_CHARS or token.count(".") != 2:
        return None
    parts = token.split(".")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    # binascii.Error is a ValueError; a RecursionError is neither.
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    project_id = payload.get("project_id") if isinstance(payload, dict) else None
    return project_id if isinstance(project_id, str) else None


def _signing_secret(deps: Deps, project_id: str | None) -> str | None:
    """``project_id``'s signing secret, or ``None`` — this never raises.

    ``signing_secret_for`` is host-supplied, and this repo's two "unknown
    id" idioms both *raise*: ``TenancyStore._require_project`` raises
    :class:`auradefi.errors.NotFoundError`, a ``dict``-backed resolver
    raises ``KeyError``. Either escaping :func:`require_user_token` would
    answer "does this project exist?" with a 404/500 — the enumeration
    channel SPEC §7.2 closes. Exactly those two collapse to ``None``
    here, so an unknown project fails as a plain ``AuthError`` like every
    other bad token.

    Nothing else is caught — not the rest of the taxonomy, and nothing
    outside it. A ``SourceError`` from a dead KMS, a ``ConfigError`` from
    a misconfigured vault or a socket timeout says nothing about whether
    the project EXISTS, so there is no channel to close: those are our
    fault, and must surface as a 500 rather than be disguised as a
    rejected credential.
    """
    if project_id is None:
        return None
    try:
        return deps.signing_secret_for(project_id)
    except (NotFoundError, LookupError):
        return None


def require_api_key(deps: Deps, request: Request, scope: Scope) -> ApiKey:
    """Authenticate the ``Authorization: Bearer adk_...`` header.

    A missing header, a non-``Bearer`` scheme, a malformed token and a
    genuinely bad/revoked/expired key all raise plain
    :class:`auradefi.errors.AuthError` with the SAME message as
    ``ApiKeyStore.authenticate`` — a probing caller learns nothing
    (SPEC §7.2).

    On success ``request.state.project_id`` is set IMMEDIATELY — before
    the scope check — so the quota middleware still labels a 403.
    Missing ``scope`` then raises :class:`auradefi.errors.ScopeError`.
    """
    # An absent or malformed header authenticates as the empty credential:
    # the store's own rejection message is reused, never copied and re-drifted.
    key = deps.keys.authenticate(_bearer_credential(request), deps.clock)
    request.state.project_id = key.project_id
    if not has_scope(key, scope):
        raise ScopeError(f"missing required scope: {scope}")
    return key


def require_user_token(deps: Deps, request: Request, scope: str) -> TokenClaims:
    """Verify a user JWT from ``Authorization: Bearer <jwt>``.

    The payload segment is PEEKED unverified for ``project_id`` purely to
    select a secret via :func:`_signing_secret`. A malformed or over-long
    token, an unparseable payload, a missing or non-``str``
    ``project_id``, and an unknown project — whether the resolver answers
    ``None`` or raises an unknown-id error (``NotFoundError``/``KeyError``)
    — all raise plain :class:`auradefi.errors.AuthError` with one
    identical message — NEVER :class:`auradefi.errors.NotFoundError`, so
    token probing cannot enumerate project ids.

    Verification then runs through Phase 2's untouched
    ``tokens.verify_token`` (rejection order malformed → bad signature →
    expired → revoked, signature BEFORE expiry) with
    ``deps.revocations``. ``request.state.project_id`` is set from the
    verified claims, then ``tokens.require_scope`` applies ``scope``.
    """
    token = _bearer_credential(request)
    if (project_id := _peek_project_id(token)) is None:
        # Unreadable HERE is unverifiable THERE: `tokens.verify_token` runs
        # the same base64/JSON decode, so a credential we cannot peek is
        # already its one plain AuthError. Collapse it to the empty one, so
        # bytes we refused to parse never reach Phase 2's parser, which
        # catches only ValueError and not RecursionError (§4 #34).
        token = ""
    secret = _signing_secret(deps, project_id)
    if not secret:
        # No project, no secret: verify against a one-shot unguessable one,
        # so an unknown project fails as verify_token's own AuthError —
        # indistinguishable, byte for byte, from a forged signature.
        #
        # `not secret`, NOT `secret is None`: a host resolver written as
        # `vault.get(project_id, "")` — the ordinary dict/environ idiom —
        # returns "" for an unknown or not-yet-provisioned project. An
        # empty HMAC key is the maximally guessable one, so treating it as
        # live would let anyone mint a token for any project_id with any
        # scopes. Absent is absent, whether it reads None or "".
        secret = secrets.token_hex(32)
    claims = verify_token(
        token, signing_secret=secret, clock=deps.clock, revoked=deps.revocations
    )
    request.state.project_id = claims.project_id
    require_scope(claims, scope)
    return claims


def resolve_end_user(deps: Deps, claims: TokenClaims) -> EndUser:
    """Get-or-create this project's user for the token's external id.

    SPEC §7.1: there is no user-creation endpoint — a user exists as a
    side effect. Idempotent, including ``created_at``.
    """
    return deps.tenancy.get_or_create_user(
        claims.project_id, claims.external_user_id, deps.clock
    )


def consume_quota(deps: Deps, project_id: str) -> None:
    """Consume one unit from all three of the project's windows.

    Raises :class:`auradefi.errors.QuotaExceededError` without consuming
    anything when a window is exhausted.
    """
    deps.quota.hit(project_id)


def quota_headers(snapshot: Mapping[str, WindowSnapshot]) -> dict[str, str]:
    """The nine pinned quota headers for one snapshot (DECISIONS).

    Keys are ``X-RateLimit-{Limit,Remaining,Reset}-{Second,Day,Month}``;
    every value is a decimal string, and ``Reset`` is a MS-EPOCH int
    rendered as a decimal string — not seconds, not ISO-8601.
    """
    headers: dict[str, str] = {}
    for name in _WINDOWS:
        window = snapshot[name]
        suffix = name.capitalize()
        headers[f"X-RateLimit-Limit-{suffix}"] = str(window.limit)
        headers[f"X-RateLimit-Remaining-{suffix}"] = str(window.remaining)
        headers[f"X-RateLimit-Reset-{suffix}"] = str(window.reset_at_ms)
    return headers


def retry_after_seconds(snapshot: Mapping[str, WindowSnapshot], now_ms: int) -> int:
    """Whole seconds until the caller may retry (RFC 9110 units).

    Over the EXHAUSTED windows (``remaining <= 0``) take the smallest
    ``reset_at_ms``; with none exhausted fall back to
    ``snapshot['second']``. Then ``max(1, -(-(reset_at_ms - now_ms) //
    1000))`` — ceiling division, never zero, never negative.
    """
    exhausted = [w.reset_at_ms for w in snapshot.values() if w.remaining <= 0]
    reset_at_ms = min(exhausted) if exhausted else snapshot["second"].reset_at_ms
    return max(1, -(-(reset_at_ms - now_ms) // 1_000))


def install_quota_headers(app: FastAPI, deps: Deps) -> None:
    """Attach an HTTP middleware emitting the nine headers per response.

    The headers are set AFTER the response exists — success or error, so
    a 403 and a 429 carry them too — from a FRESH
    ``deps.quota.snapshot(project_id)``, and only when
    ``request.state.project_id`` was set (an unauthenticated route such
    as ``GET /coverage`` carries none). ``snapshot`` consumes nothing.
    """

    @app.middleware("http")
    async def _emit_quota_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        project_id = getattr(request.state, "project_id", None)
        if project_id is not None:
            response.headers.update(quota_headers(deps.quota.snapshot(project_id)))
        return response
