"""Dependency container, auth resolution, quota headers (SPEC §7.1–§7.3).

Rule #11 made structural: the HTTP shell owns no state. Everything a route
needs arrives in one frozen :class:`Deps` — no module globals, no import-time
I/O, no singletons. Two apps in one process never share a tenant store.

Two collaborators are declared here as *structural* Protocols rather than
imported, deliberately:

* :class:`HoldingsProvider` — ``ALLOWED_IMPORTS['api']`` omits ``portfolio``
  (tests/style/test_layering.py). ``portfolio.holdings.HoldingsService``
  conforms without this module knowing it exists. The precedent is
  ``portfolio/holdings.py``'s own ``BalanceSource``.
* :class:`WebhookSink` — ``webhooks/`` is a same-wave sibling; a runtime
  import would couple the two orders. ``webhooks.deliver``'s store conforms
  structurally.

``signing_secret_for`` exists because Phase 2's committed ``TenancyStore``
exposes no project getter and this phase may not edit Phase 2 files: the
host hands us ``project_id -> signing secret | None``.

Pinned in docs/DECISIONS.md ("Quota headers"): nine headers,
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


@runtime_checkable
class WebhookSink(Protocol):
    """Structural seam onto the project-scoped webhook store (SPEC §7.3).

    Never an import of ``auradefi.webhooks``: the sibling order owns that
    module. Any object with these six methods is a sink.
    """

    def register_endpoint(
        self,
        project_id: str,
        url: str,
        events: Iterable[str],
        clock: Clock,
    ) -> object:
        """Register (or re-register) one endpoint for ``project_id``."""
        raise NotImplementedError

    def endpoints(self, project_id: str) -> Sequence[object]:
        """This project's endpoints — never another project's."""
        raise NotImplementedError

    def emit(
        self,
        project_id: str,
        name: str,
        data: Mapping[str, object],
        clock: Clock,
    ) -> object:
        """Emit event ``name`` to every endpoint subscribed to it."""
        raise NotImplementedError

    def deliveries(self, project_id: str) -> Sequence[object]:
        """Every delivery for this project, in creation order."""
        raise NotImplementedError

    def dead_letter(self, project_id: str) -> Sequence[object]:
        """Deliveries that exhausted the pinned retry schedule."""
        raise NotImplementedError

    def get_delivery(self, project_id: str, delivery_id: str) -> object:
        """One delivery inside this project's scope."""
        raise NotImplementedError

    def get_event(self, project_id: str, event_id: str) -> object:
        """The event a delivery carries, inside this project's scope.

        Declared because the delivery wire exposes ``event_name`` while a
        stored delivery holds only ``event_id`` — so the admin routes MUST
        read the event back. Leaving it off the Protocol while calling it
        anyway would make a conforming custom sink fail with
        ``AttributeError`` at request time; a seam has to promise
        everything its consumer actually uses.
        """
        raise NotImplementedError


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

    Never raises, never trusts: anything but a three-segment token whose
    payload is a JSON object with a ``str`` ``project_id`` reads ``None``,
    and every claim is re-checked by ``tokens.verify_token``.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError):  # binascii.Error is a ValueError
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
    select a secret via :func:`_signing_secret`. A malformed token, an
    unparseable payload, a missing or non-``str`` ``project_id``, and an
    unknown project — whether the resolver answers ``None`` or raises an
    unknown-id error (``NotFoundError``/``KeyError``) — all raise plain
    :class:`auradefi.errors.AuthError` with one identical
    message — NEVER :class:`auradefi.errors.NotFoundError`, so token
    probing cannot enumerate project ids.

    Verification then runs through Phase 2's untouched
    ``tokens.verify_token`` (rejection order malformed → bad signature →
    expired → revoked, signature BEFORE expiry) with
    ``deps.revocations``. ``request.state.project_id`` is set from the
    verified claims, then ``tokens.require_scope`` applies ``scope``.
    """
    token = _bearer_credential(request)
    secret = _signing_secret(deps, _peek_project_id(token))
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
