"""Token mint/revoke and end-user reads (SPEC §7.1, §7.2).

Vezgo's best idea, kept verbatim: ``POST /auth/token`` answers exactly
``{"token": ...}``. The body names an ``external_user_id`` chosen by the
HOST from its own session — a hostile browser cannot express which user
it wants — and the privilege rule below closes Vezgo's remaining hole,
an unscoped god key that can mint anything.

``ApiKeyStore.authenticate``/``tokens.verify_token`` keep their
one-message rejection by DELEGATION rather than re-statement, so probing
learns nothing. Quota is consumed HERE, right after authentication and
before the privilege check, exactly as the three sibling handlers do: an
authenticated caller must not be able to drive unlimited refused mints —
each walking and HMAC-comparing every stored key — for free
(RELEASE_0.1.1 §4 #36). ``mint_user_token`` is therefore called without
``quota``, so a mint is charged exactly once, and a refusal is charged
without ever auditing a mint that did not happen.

``POST /auth/revoke`` verifies under the CALLER'S OWN project secret, so
no property of another project's token — authentic, expired, live or
unknown — is observable through it (§4 #33).
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
from auradefi.errors import AuthError, ScopeError
from auradefi.tenancy.models import EndUser, Scope

# ``_REJECTED`` is imported, never copied: the ONE rejection message must
# have exactly one definition, or the indistinguishability #33 buys decays
# the first time either copy is reworded.
from auradefi.tenancy.tokens import _REJECTED, verify_token


class TokenRequest(BaseModel):
    """``POST /auth/token`` body — exactly two keys, extras forbidden.

    ``scopes`` OMITTED or ``null`` means "everything this key has"; ``[]``
    is a request for a ZERO-privilege token and is honoured as one. The
    distinction is why the handler tests ``is not None`` rather than
    truthiness (RELEASE_0.1.1 §4 #20).
    """

    model_config = ConfigDict(extra="forbid")

    external_user_id: str
    scopes: list[str] | None = None


class RevokeRequest(BaseModel):
    """``POST /auth/revoke`` body — exactly ``{"token": str}``."""

    model_config = ConfigDict(extra="forbid")

    token: str


def _client_ip(request: Request, trusted_hops: int) -> tuple[str, str]:
    """The caller's IP for the audit row, and WHERE it came from (§4 #30).

    ``trusted_hops`` is ``Deps.trusted_proxy_hops``: how many rightmost
    ``X-Forwarded-For`` hops this deployment's own proxies append. It
    defaults to 0, and at 0 the header is NOT CONSULTED AT ALL — the
    socket peer is the only verified source. With N > 0 the Nth hop from
    the RIGHT is the address our own outermost proxy observed, and it is
    the only header value trusted; every hop left of it is caller-written.
    A chain shorter than N did not traverse those proxies, so nothing in
    it is trusted and the peer is used instead.

    The chain is read from EVERY ``X-Forwarded-For`` field line, joined:
    HTTP lets the field repeat, RFC 9110 §5.3 makes the repeated lines one
    comma-joined value, and a proxy appending its own line rather than
    extending the caller's is an ordinary wire form. Reading only
    ``headers.get``'s FIRST line counted the caller's own line as the
    trusted rightmost hop, so a forged address landed in the permanent
    audit row stamped ``"forwarded"`` — the fix below is bypassed without
    this join in exactly the single-proxy deployment it documents.

    Returns ``(ip, ip_source)``. ``ip_source`` is ``"forwarded"`` for a
    trusted header hop, ``"peer"`` for the socket peer, and ``"unknown"``
    when there is neither — an audit row is permanent and unmodifiable, so
    an unknown IP is DECLARED, never guessed, and never a reason to fail a
    mint.
    """
    if trusted_hops > 0:
        forwarded = ", ".join(request.headers.getlist("x-forwarded-for"))
        hops = [hop.strip() for hop in forwarded.split(",")]
        if len(hops) >= trusted_hops and hops[-trusted_hops]:
            return hops[-trusted_hops], "forwarded"
    if request.client is not None and request.client.host:
        return request.client.host, "peer"
    return "", "unknown"


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

    * ``POST /auth/token`` — api key + ``users:admin``, quota, then the
      PINNED privilege rule ``requested <= {str(s) for s in key.scopes}``,
      else :class:`~auradefi.errors.ScopeError`: a key can never mint a
      token more powerful than itself. ``requested`` is ``body.scopes``
      whenever it was sent — ``[]`` included, which asks for a
      ZERO-privilege token — and the key's own scopes only when the field
      was omitted or ``null``. Body is exactly ``{"token": "<jwt>"}``.
    * ``POST /auth/revoke`` — api key + ``users:admin``, quota, then
      verify under the CALLER'S OWN project secret and demand
      ``claims.project_id == key.project_id``. A token this project did
      not mint cannot verify here, so foreign-and-live,
      foreign-and-expired, foreign-and-unknown and outright forged all
      answer with ONE 401 :class:`~auradefi.errors.AuthError` carrying
      ``tokens._REJECTED`` — indistinguishable from each other, and from a
      token that never existed, in status, type, message and work done.
      The caller's own token keeps its own error class (an expired one is
      still a ``TokenExpiredError``); nothing there is another tenant's
      secret to leak. Idempotent — ``revoked`` is checked nowhere, so
      re-revoking answers 200 again.
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
        # Charged after authentication and BEFORE the privilege check, as
        # the three siblings below do: a refused mint has already walked and
        # HMAC-compared every stored key, so it must not be free. The mint
        # is then called WITHOUT `quota` — charging both here and there
        # would bill a success twice — and a ScopeError below raises before
        # the mint, so a refusal is charged without ever auditing a mint
        # that never happened.
        consume_quota(deps, key.project_id)
        # `str(scope)`, not `scope.value`: a key rehydrated from JSON or SQL
        # holds plain strings, and it authenticates everywhere else because
        # Scope is a StrEnum. Both sides of the comparison are read exactly
        # as tolerantly, or minting alone 500s for that key.
        granted = {str(scope) for scope in key.scopes}
        # `is not None`, not `or`: an explicitly empty list asks for a
        # ZERO-privilege token, and `or` would read it as "omitted" and mint
        # every scope the key itself holds.
        asked = key.scopes if body.scopes is None else body.scopes
        requested = {str(scope) for scope in asked}
        if not requested <= granted:
            raise ScopeError(
                "a key cannot mint a token more powerful than itself; not held: "
                f"{sorted(requested - granted)}"
            )
        ip, ip_source = _client_ip(request, deps.trusted_proxy_hops)
        return {
            "token": deps.tenancy.mint_user_token(
                project_id=key.project_id,
                external_user_id=body.external_user_id,
                scopes=sorted(requested),
                ttl_ms=deps.token_ttl_ms,
                ip=ip,
                key_id=key.id,
                clock=deps.clock,
                audit=deps.audit,
                ip_source=ip_source,
            )
        }

    @api.post("/auth/revoke")
    def revoke_user_token(body: RevokeRequest, request: Request) -> dict[str, Any]:
        """Revoke one jti. Idempotent; an unowned token is one plain 401."""
        key = require_api_key(deps, request, Scope.USERS_ADMIN)
        consume_quota(deps, key.project_id)
        # The secret is the CALLER'S OWN project's, NEVER the one named by
        # the token's unverified `project_id` claim. Resolving it from the
        # claim verified a captured JWT under its real owner's secret and
        # then answered three different ways — genuine-and-live 404,
        # forged 401 AuthError, expired 401 TokenExpiredError — which let
        # anyone holding a free project of their own sort captured tokens
        # into "authentic and still live" without the victim's secret
        # (RELEASE_0.1.1 §4 #33). Verified here against our own secret, a
        # token we did not mint simply fails the signature, so every
        # unowned case is verify_token's single AuthError.
        secret = _signing_secret(deps, key.project_id)
        # Unreadable HERE is unverifiable THERE — the same collapse
        # `require_user_token` makes. `_peek_project_id` bounds the length
        # and catches the RecursionError ~10,000 nested arrays raise (§4
        # #34); `tokens.verify_token` runs the same base64/JSON decode but
        # catches only ValueError, so bytes we refused to parse are handed
        # on as the empty credential — also one plain AuthError. Without
        # this, malformed input is an unformatted 500: a failure path on
        # this route that is trivially distinguishable from all the others.
        # The peek is a READABILITY gate only; it never selects the secret.
        token = body.token if _peek_project_id(body.token) is not None else ""
        claims = verify_token(
            token,
            # An unknown project still pays for a real verification
            # against a one-shot secret: no failure path returns early, so
            # none is separable by timing either.
            signing_secret=secret or secrets.token_hex(32),
            clock=deps.clock,
        )
        if claims.project_id != key.project_id:
            # Only reachable for a token signed with THIS project's secret
            # while claiming another's — i.e. one this caller minted the
            # hard way. `revocations` is not tenant-scoped, so accepting it
            # would let a caller revoke a jti belonging to a project they
            # cannot name. Same error, same message as every other refusal.
            raise AuthError(_REJECTED)
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
