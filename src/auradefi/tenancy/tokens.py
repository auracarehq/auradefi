"""HS256 JWT mint/verify and jti revocation, stdlib only (SPEC §7.1/§7.2).

Wire form is PINNED in docs/internal/DECISIONS.md ("JWT wire form") and honoured
verbatim:

* each segment is ``base64.urlsafe_b64encode(json.dumps(obj,
  separators=(",", ":"), sort_keys=True).encode("utf-8"))`` with ``"="``
  padding stripped;
* header is exactly ``{"alg": "HS256", "typ": "JWT"}``;
* signature = base64url-no-pad of ``hmac.new(secret.encode("utf-8"),
  f"{header_b64}.{payload_b64}".encode("ascii"), hashlib.sha256).digest()``;
* claims are exactly ``{exp, external_user_id, iat, jti, project_id,
  scopes}`` with ``scopes`` a sorted de-duplicated JSON list;
* ``iat``/``exp`` are MS-EPOCH ints: a deliberate, documented deviation
  from RFC 7519 NumericDate seconds (SPEC §4.4: "ms epoch, everywhere,
  always"; these tokens are consumed only by our own verify path).

Rejection order in :func:`verify_token` is exact: malformed → bad
signature (``hmac.compare_digest``) → expired (``now_ms >= exp``,
exclusive) → revoked. Signature comes BEFORE expiry so a probing caller
holding a forged token learns nothing from the error class. ``alg: none``
is closed: any header that is not byte-for-byte the pinned one is
malformed.

Stdlib only: hmac, hashlib, base64, json, secrets, never PyJWT, never
``time.time()`` (time arrives through a :class:`~auradefi.clock.Clock`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Iterable
from dataclasses import dataclass

from auradefi.clock import Clock
from auradefi.errors import (
    AuthError,
    ScopeError,
    TokenExpiredError,
    TokenRevokedError,
)

# One message for every plain AuthError: malformed and bad-signature must
# be indistinguishable to a probing caller (SPEC §7.2).
_REJECTED = "token failed authentication"

# Payload keys, pre-sorted: json.dumps(sort_keys=True) emits this order.
_CLAIM_KEYS = ("exp", "external_user_id", "iat", "jti", "project_id", "scopes")


def _encode_segment(obj: object) -> str:
    """Base64url-no-pad over compact sorted-key JSON (the pinned form)."""
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign(signing_secret: str, signing_input: str) -> str:
    """Base64url-no-pad HMAC-SHA256 of ``signing_input`` under the secret."""
    digest = hmac.new(
        signing_secret.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


_HEADER_B64 = _encode_segment({"alg": "HS256", "typ": "JWT"})


def _decode_json_segment(segment: str) -> object:
    """Strictly decode one base64url segment to JSON, or raise AuthError.

    ``RecursionError`` is caught alongside the value errors deliberately.
    It is a ``RuntimeError``, so the obvious ``(ValueError,
    UnicodeDecodeError)`` pair misses it, and ~10,000 nested arrays reach
    ``json.loads`` from every caller that verifies a caller-supplied
    bearer token, not only from the peek that selects a secret. Letting
    it escape turns a pinned 401 into an unformatted 500 and leaks a
    stack trace, so the malformed-input answer must be the same here as
    everywhere else on this surface (RELEASE_0.1.1 §4 #34).
    """
    padded = segment + "=" * (-len(segment) % 4)
    try:
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
    # Both Unicode*Error classes descend from ValueError (via UnicodeError),
    # so naming either beside it caught nothing while implying the taxonomy
    # was wider than it is. binascii.Error is a ValueError as well; a
    # RecursionError is the one root here that is not. Do not add them back.
    except ValueError as exc:
        raise AuthError(_REJECTED) from exc
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise AuthError(_REJECTED) from exc


def _validate_payload(payload: object) -> dict:
    """Structural gate: exactly the six pinned claims, correctly typed."""
    if not isinstance(payload, dict) or sorted(payload) != list(_CLAIM_KEYS):
        raise AuthError(_REJECTED)
    for key in ("external_user_id", "jti", "project_id"):
        if not isinstance(payload[key], str):
            raise AuthError(_REJECTED)
    for key in ("exp", "iat"):
        if not isinstance(payload[key], int) or isinstance(payload[key], bool):
            raise AuthError(_REJECTED)
    scopes = payload["scopes"]
    if not isinstance(scopes, list) or not all(
        isinstance(scope, str) for scope in scopes
    ):
        raise AuthError(_REJECTED)
    return payload


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Verified claims of one token; frozen. A credential is not mutable.

    ``iat``/``exp`` are ms-epoch ints; ``scopes`` is the sorted
    de-duplicated tuple exactly as serialised on the wire.
    """

    external_user_id: str
    project_id: str
    scopes: tuple[str, ...]
    iat: int
    exp: int
    jti: str


class RevocationSet:
    """In-memory set of revoked jti values (SPEC §7.2).

    Deliberately a class, not a bare set: Phase 5 extracts a port from
    this interface so a host can bind a shared store.
    """

    def __init__(self) -> None:
        self._revoked: set[str] = set()

    def revoke(self, jti: str) -> None:
        """Add ``jti`` to the revocation set. Idempotent."""
        self._revoked.add(jti)

    def is_revoked(self, jti: str) -> bool:
        """Return True iff ``jti`` has been revoked."""
        return jti in self._revoked


def mint_token(
    *,
    signing_secret: str,
    project_id: str,
    external_user_id: str,
    scopes: Iterable[str],
    ttl_ms: int,
    clock: Clock,
    jti: str | None = None,
) -> str:
    """Mint a compact HS256 JWT in the pinned wire form.

    ``jti`` defaults to ``secrets.token_hex(16)`` (injectable for tests);
    ``iat = clock.now_ms()``; ``exp = iat + ttl_ms``; payload keys are
    exactly ``{exp, external_user_id, iat, jti, project_id, scopes}``
    with ``scopes`` sorted and de-duplicated. The result has exactly two
    dots and no ``"="`` padding anywhere.
    """
    if jti is None:
        jti = secrets.token_hex(16)
    iat = clock.now_ms()
    payload = {
        "exp": iat + ttl_ms,
        "external_user_id": external_user_id,
        "iat": iat,
        "jti": jti,
        "project_id": project_id,
        "scopes": sorted(set(scopes)),
    }
    payload_b64 = _encode_segment(payload)
    signature = _sign(signing_secret, f"{_HEADER_B64}.{payload_b64}")
    return f"{_HEADER_B64}.{payload_b64}.{signature}"


def verify_token(
    token: str,
    *,
    signing_secret: str,
    clock: Clock,
    revoked: RevocationSet | None = None,
) -> TokenClaims:
    """Verify ``token`` and return its claims, or raise.

    EXACT rejection order:

    1. structurally malformed (not 3 dot-segments, bad base64/JSON,
       header not exactly the pinned one) → :class:`AuthError`;
    2. signature mismatch via ``hmac.compare_digest`` → :class:`AuthError`
       (signature BEFORE expiry);
    3. ``clock.now_ms() >= exp`` (exclusive) → :class:`TokenExpiredError`;
    4. ``revoked`` given and ``revoked.is_revoked(jti)`` →
       :class:`TokenRevokedError`.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError(_REJECTED)
    header_b64, payload_b64, signature_b64 = parts
    if header_b64 != _HEADER_B64:
        raise AuthError(_REJECTED)
    payload = _validate_payload(_decode_json_segment(payload_b64))
    expected = _sign(signing_secret, f"{header_b64}.{payload_b64}")
    if not hmac.compare_digest(
        expected.encode("ascii"), signature_b64.encode("utf-8")
    ):
        raise AuthError(_REJECTED)
    if clock.now_ms() >= payload["exp"]:
        raise TokenExpiredError("token expired")
    if revoked is not None and revoked.is_revoked(payload["jti"]):
        raise TokenRevokedError("token revoked")
    return TokenClaims(
        external_user_id=payload["external_user_id"],
        project_id=payload["project_id"],
        scopes=tuple(payload["scopes"]),
        iat=payload["iat"],
        exp=payload["exp"],
        jti=payload["jti"],
    )


def require_scope(claims: TokenClaims, scope: str) -> None:
    """Raise :class:`ScopeError` unless ``scope`` is in ``claims.scopes``."""
    if scope not in claims.scopes:
        raise ScopeError(f"missing required scope: {scope}")
