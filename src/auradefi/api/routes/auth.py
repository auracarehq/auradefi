"""Token mint/revoke and end-user reads (SPEC §7.1, §7.2).

Vezgo's best idea, kept verbatim: ``POST /auth/token`` answers exactly
``{"token": ...}``. The body names an ``external_user_id`` chosen by the
HOST from its own session — a hostile browser cannot express which user
it wants — and the privilege rule below closes Vezgo's remaining hole,
an unscoped god key that can mint anything.

Two Phase-2 orders are preserved by DELEGATION rather than re-stated
here: ``TenancyStore.mint_user_token`` is handed ``deps.quota``, so
quota is consumed FIRST and nothing is minted, created or audited on
refusal; and ``ApiKeyStore.authenticate``/``tokens.verify_token`` keep
their one-message rejection so probing learns nothing.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from auradefi.api.deps import (
    Deps,
    _peek_project_id,
    _signing_secret,
    consume_quota,
    require_api_key,
    require_user_token,
    resolve_end_user,
)
from auradefi.errors import NotFoundError, ScopeError
from auradefi.tenancy.models import EndUser, Scope
from auradefi.tenancy.tokens import verify_token


class TokenRequest(BaseModel):
    """``POST /auth/token`` body — exactly two keys, extras forbidden.

    ``scopes`` omitted or ``null`` means "everything this key has".
    """

    model_config = ConfigDict(extra="forbid")

    external_user_id: str
    scopes: list[str] | None = None


class RevokeRequest(BaseModel):
    """``POST /auth/revoke`` body — exactly ``{"token": str}``."""

    model_config = ConfigDict(extra="forbid")

    token: str


def _client_ip(request: Request) -> str:
    """The caller's IP for the audit row: first ``X-Forwarded-For`` hop.

    Falls back to ``request.client.host`` and finally to ``""`` — an
    unknown IP is recorded as empty, never as a guess and never as a
    reason to fail a mint.
    """
    first_hop = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if first_hop:
        return first_hop
    return request.client.host if request.client is not None else ""


def _user_wire(user: EndUser) -> dict[str, Any]:
    """Project one ``EndUser``: exactly ``{id, project_id,
    external_user_id, created_at_ms}``."""
    return {
        "id": user.id,
        "project_id": user.project_id,
        "external_user_id": user.external_user_id,
        "created_at_ms": user.created_at,
    }


def router(deps: Deps) -> APIRouter:
    """Build the auth/users router over ``deps``.

    Four routes:

    * ``POST /auth/token`` — api key + ``users:admin``. PINNED privilege
      rule ``set(requested or key.scopes) <= {s.value for s in
      key.scopes}``, else :class:`~auradefi.errors.ScopeError`: a key can
      never mint a token more powerful than itself. The mint goes
      through ``tenancy.mint_user_token(..., quota=deps.quota)`` — this
      route does NOT call ``consume_quota`` itself, so Phase 2's
      quota-first order survives end to end. Body is exactly
      ``{"token": "<jwt>"}``.
    * ``POST /auth/revoke`` — api key + ``users:admin``, quota, then
      verify under the TOKEN's own project secret and demand
      ``claims.project_id == key.project_id``, else
      :class:`~auradefi.errors.NotFoundError` (404): another project's
      token is indistinguishable from a token that never existed.
      Idempotent — ``revoked`` is checked nowhere, so re-revoking answers
      200 again — while an expired token is still a 401.
    * ``GET /users/me`` — user token + ``accounts:read``, quota,
      get-or-create (SPEC §7.1: no user-creation endpoint exists).
    * ``GET /users`` — api key + ``users:admin``, quota, this project's
      users in creation order and no other project's.
    """
    api = APIRouter()

    @api.post("/auth/token")
    def mint_user_token(body: TokenRequest, request: Request) -> dict[str, Any]:
        """Mint one short-lived user JWT. Body is exactly ``{"token"}``."""
        key = require_api_key(deps, request, Scope.USERS_ADMIN)
        granted = {scope.value for scope in key.scopes}
        requested = {str(scope) for scope in (body.scopes or key.scopes)}
        if not requested <= granted:
            raise ScopeError(
                "a key cannot mint a token more powerful than itself; not held: "
                f"{sorted(requested - granted)}"
            )
        # quota goes THROUGH to Phase 2's flow (quota → user → mint →
        # audit): consuming it here would let a refusal audit a mint that
        # never happened.
        return {
            "token": deps.tenancy.mint_user_token(
                project_id=key.project_id,
                external_user_id=body.external_user_id,
                scopes=sorted(requested),
                ttl_ms=deps.token_ttl_ms,
                ip=_client_ip(request),
                key_id=key.id,
                clock=deps.clock,
                audit=deps.audit,
                quota=deps.quota,
            )
        }

    @api.post("/auth/revoke")
    def revoke_user_token(body: RevokeRequest, request: Request) -> dict[str, Any]:
        """Revoke one jti. Idempotent; another project's token is a 404."""
        key = require_api_key(deps, request, Scope.USERS_ADMIN)
        consume_quota(deps, key.project_id)
        # The secret is selected by the TOKEN's own project so a foreign
        # token verifies and then fails the ownership check as a 404,
        # rather than failing as a 401 that would confirm the signature
        # was checked against the wrong secret. deps' peek/select pair is
        # reused rather than re-implemented: token → secret has exactly
        # one implementation in the codebase.
        secret = _signing_secret(deps, _peek_project_id(body.token))
        claims = verify_token(
            body.token,
            signing_secret=secret or secrets.token_hex(32),
            clock=deps.clock,
        )
        if claims.project_id != key.project_id:
            raise NotFoundError(f"token not found: {claims.jti!r}")
        # `revoked` is deliberately NOT passed to verify_token above:
        # re-revoking must answer 200 again, not 401.
        deps.revocations.revoke(claims.jti)
        return {"revoked": True, "jti": claims.jti}

    @api.get("/users/me")
    def read_current_user(request: Request) -> dict[str, Any]:
        """The caller's own user record, created on first sight (§7.1)."""
        claims = require_user_token(deps, request, Scope.ACCOUNTS_READ)
        consume_quota(deps, claims.project_id)
        return _user_wire(resolve_end_user(deps, claims))

    @api.get("/users")
    def list_users(request: Request) -> dict[str, Any]:
        """This project's users in creation order — never another's."""
        key = require_api_key(deps, request, Scope.USERS_ADMIN)
        consume_quota(deps, key.project_id)
        users = [_user_wire(user) for user in deps.tenancy.users(key.project_id)]
        return {"users": users, "count": len(users)}

    return api
