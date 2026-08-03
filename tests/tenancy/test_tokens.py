"""HS256 JWT mint/verify and jti revocation (SPEC §7.1/§7.2).

The token literals below were derived INDEPENDENTLY via a ``python3 -c``
scratch script implementing the algorithm pinned in docs/DECISIONS.md
("JWT wire form"): base64url-no-pad segments over
``json.dumps(obj, separators=(",", ":"), sort_keys=True)``, header exactly
``{"alg":"HS256","typ":"JWT"}``, HMAC-SHA256 signature, iat/exp as
MS-EPOCH ints. They are hardcoded on purpose: the wire form is a
stability contract, and a stability contract is a literal, not a call to
the function under test.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import re

import pytest

from auradefi.clock import FrozenClock
from auradefi.errors import (
    AuthError,
    ScopeError,
    TokenExpiredError,
    TokenRevokedError,
)
from auradefi.tenancy.tokens import (
    RevocationSet,
    TokenClaims,
    mint_token,
    require_scope,
    verify_token,
)

SECRET = "test-secret"
IAT_MS = 1_767_225_600_000
TTL_MS = 600_000
EXP_MS = 1_767_226_200_000  # IAT_MS + TTL_MS
JTI = "0123456789abcdef0123456789abcdef"

# Golden vector — single scope, jti injected. Derived independently
# (see module docstring); matches the work order literal byte-for-byte.
GOLDEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJleHAiOjE3NjcyMjYyMDAwMDAsImV4dGVybmFsX3VzZXJfaWQiOiJob3N0LXVzZXItMSIs"
    "ImlhdCI6MTc2NzIyNTYwMDAwMCwianRpIjoiMDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlh"
    "YmNkZWYiLCJwcm9qZWN0X2lkIjoicHJval9hIiwic2NvcGVzIjpbImFjY291bnRzOnJlYWQi"
    "XX0.mVBJ5b8-UXh6vnoFfJ0rFOv-QTFd300QNTIdt86qWzw"
)

# Same claims but scopes minted from ['sync:trigger', 'accounts:read',
# 'accounts:read'] — wire scopes must be ["accounts:read","sync:trigger"].
GOLDEN_MULTISCOPE = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJleHAiOjE3NjcyMjYyMDAwMDAsImV4dGVybmFsX3VzZXJfaWQiOiJob3N0LXVzZXItMSIs"
    "ImlhdCI6MTc2NzIyNTYwMDAwMCwianRpIjoiMDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlh"
    "YmNkZWYiLCJwcm9qZWN0X2lkIjoicHJval9hIiwic2NvcGVzIjpbImFjY291bnRzOnJlYWQi"
    "LCJzeW5jOnRyaWdnZXIiXX0.TtUl1C9wpL8JJVwaNecQqDQd_JMLx92h1QVRM0U1084"
)

# Header {"alg":"none","typ":"JWT"}, correctly HMAC-signed segments under
# SECRET — still malformed, because the header is not byte-for-byte the
# pinned one (alg-none is closed at the structure gate, not the key gate).
ALG_NONE_TOKEN = (
    "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
    "eyJleHAiOjE3NjcyMjYyMDAwMDAsImV4dGVybmFsX3VzZXJfaWQiOiJob3N0LXVzZXItMSIs"
    "ImlhdCI6MTc2NzIyNTYwMDAwMCwianRpIjoiMDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlh"
    "YmNkZWYiLCJwcm9qZWN0X2lkIjoicHJval9hIiwic2NvcGVzIjpbImFjY291bnRzOnJlYWQi"
    "XX0.7J1OlsOzfgb1VpMxm_2SM2Nd8PoK0mJlDwWFQbqN7Ps"
)

GOLDEN_CLAIMS = TokenClaims(
    external_user_id="host-user-1",
    project_id="proj_a",
    scopes=("accounts:read",),
    iat=IAT_MS,
    exp=EXP_MS,
    jti=JTI,
)


def _mint(scopes=("accounts:read",), jti=JTI, ttl_ms=TTL_MS, now_ms=IAT_MS):
    return mint_token(
        signing_secret=SECRET,
        project_id="proj_a",
        external_user_id="host-user-1",
        scopes=list(scopes),
        ttl_ms=ttl_ms,
        clock=FrozenClock(now_ms),
        jti=jti,
    )


def _decode_payload(token: str) -> dict:
    segment = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


# --- mint: the pinned wire form -----------------------------------------------


def test_mint_matches_the_golden_vector_byte_for_byte():
    assert _mint() == GOLDEN


def test_mint_sorts_and_deduplicates_scopes_multiscope_golden():
    token = _mint(scopes=["sync:trigger", "accounts:read", "accounts:read"])
    assert token == GOLDEN_MULTISCOPE
    assert _decode_payload(token)["scopes"] == ["accounts:read", "sync:trigger"]


def test_minted_token_has_no_padding_and_exactly_two_dots():
    token = _mint()
    assert "=" not in token
    assert token.count(".") == 2


def test_payload_keys_are_exactly_the_pinned_six_in_sorted_order():
    payload = _decode_payload(_mint())
    assert list(payload) == [
        "exp",
        "external_user_id",
        "iat",
        "jti",
        "project_id",
        "scopes",
    ]


def test_iat_and_exp_are_ms_epoch_ints_from_the_clock():
    payload = _decode_payload(_mint())
    assert payload["iat"] == IAT_MS
    assert payload["exp"] == EXP_MS
    assert isinstance(payload["iat"], int)
    assert isinstance(payload["exp"], int)


def test_default_jti_is_32_hex_chars_and_unique_per_mint():
    first = _decode_payload(_mint(jti=None))["jti"]
    second = _decode_payload(_mint(jti=None))["jti"]
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert re.fullmatch(r"[0-9a-f]{32}", second)
    assert first != second


# --- verify: happy path and the expiry boundary -------------------------------


def test_verify_returns_the_exact_claims_one_ms_before_exp():
    claims = verify_token(
        GOLDEN, signing_secret=SECRET, clock=FrozenClock(1_767_226_199_999)
    )
    assert claims == GOLDEN_CLAIMS
    assert claims.scopes == ("accounts:read",)
    assert isinstance(claims.scopes, tuple)


def test_verify_raises_expired_exactly_at_exp_exclusive():
    with pytest.raises(TokenExpiredError):
        verify_token(GOLDEN, signing_secret=SECRET, clock=FrozenClock(EXP_MS))


def test_verify_raises_expired_long_after_exp():
    with pytest.raises(TokenExpiredError):
        verify_token(
            GOLDEN, signing_secret=SECRET, clock=FrozenClock(EXP_MS + 10**12)
        )


def test_zero_ttl_token_is_expired_at_its_own_iat():
    token = _mint(ttl_ms=0)
    with pytest.raises(TokenExpiredError):
        verify_token(token, signing_secret=SECRET, clock=FrozenClock(IAT_MS))


def test_huge_ttl_survives_the_round_trip():
    token = _mint(ttl_ms=10**18)
    claims = verify_token(
        token, signing_secret=SECRET, clock=FrozenClock(1_767_226_199_999)
    )
    assert claims.exp == IAT_MS + 10**18


# --- verify: rejection order — signature BEFORE expiry -------------------------


def test_wrong_secret_raises_plain_autherror_even_when_also_expired():
    with pytest.raises(AuthError) as excinfo:
        verify_token(
            GOLDEN, signing_secret="other-secret", clock=FrozenClock(EXP_MS + 1)
        )
    assert type(excinfo.value) is AuthError  # not TokenExpiredError: sig first


def test_wrong_secret_raises_autherror_on_a_live_token():
    with pytest.raises(AuthError) as excinfo:
        verify_token(
            GOLDEN, signing_secret="other-secret", clock=FrozenClock(IAT_MS)
        )
    assert type(excinfo.value) is AuthError


# --- verify: tampering and malformed input ------------------------------------


def test_one_flipped_payload_char_raises_autherror():
    header, payload, signature = GOLDEN.split(".")
    flipped = "A" if payload[10] != "A" else "B"
    tampered = ".".join([header, payload[:10] + flipped + payload[11:], signature])
    assert tampered != GOLDEN
    with pytest.raises(AuthError) as excinfo:
        verify_token(tampered, signing_secret=SECRET, clock=FrozenClock(IAT_MS))
    assert type(excinfo.value) is AuthError


@pytest.mark.parametrize(
    "junk",
    [
        "a.b",
        "not-a-jwt",
        "",
        "..",
        "a.b.c.d",
        GOLDEN + ".extra",
        "!!!.???.***",
    ],
)
def test_structurally_malformed_tokens_raise_autherror(junk):
    with pytest.raises(AuthError) as excinfo:
        verify_token(junk, signing_secret=SECRET, clock=FrozenClock(IAT_MS))
    assert type(excinfo.value) is AuthError


def test_alg_none_header_is_closed():
    with pytest.raises(AuthError) as excinfo:
        verify_token(
            ALG_NONE_TOKEN, signing_secret=SECRET, clock=FrozenClock(IAT_MS)
        )
    assert type(excinfo.value) is AuthError


# --- revocation (SPEC §7.2) ----------------------------------------------------


def test_revoked_jti_raises_token_revoked_error():
    revoked = RevocationSet()
    revoked.revoke(JTI)
    with pytest.raises(TokenRevokedError):
        verify_token(
            GOLDEN, signing_secret=SECRET, clock=FrozenClock(IAT_MS), revoked=revoked
        )


def test_revoking_a_different_jti_leaves_the_token_valid():
    revoked = RevocationSet()
    revoked.revoke("f" * 32)
    claims = verify_token(
        GOLDEN, signing_secret=SECRET, clock=FrozenClock(IAT_MS), revoked=revoked
    )
    assert claims == GOLDEN_CLAIMS


def test_revocation_set_membership_via_public_api():
    revoked = RevocationSet()
    assert revoked.is_revoked(JTI) is False
    revoked.revoke(JTI)
    assert revoked.is_revoked(JTI) is True
    revoked.revoke(JTI)  # idempotent
    assert revoked.is_revoked(JTI) is True
    assert revoked.is_revoked("f" * 32) is False


def test_expired_beats_revoked_in_the_rejection_order():
    revoked = RevocationSet()
    revoked.revoke(JTI)
    with pytest.raises(TokenExpiredError):
        verify_token(
            GOLDEN, signing_secret=SECRET, clock=FrozenClock(EXP_MS), revoked=revoked
        )


def test_bad_signature_beats_revoked_in_the_rejection_order():
    revoked = RevocationSet()
    revoked.revoke(JTI)
    with pytest.raises(AuthError) as excinfo:
        verify_token(
            GOLDEN,
            signing_secret="other-secret",
            clock=FrozenClock(IAT_MS),
            revoked=revoked,
        )
    assert type(excinfo.value) is AuthError


# --- require_scope --------------------------------------------------------------


def test_require_scope_present_returns_none():
    assert require_scope(GOLDEN_CLAIMS, "accounts:read") is None


def test_require_scope_missing_raises_scope_error():
    with pytest.raises(ScopeError):
        require_scope(GOLDEN_CLAIMS, "users:admin")


def test_require_scope_is_exact_match_not_prefix():
    with pytest.raises(ScopeError):
        require_scope(GOLDEN_CLAIMS, "accounts")


# --- TokenClaims immutability ----------------------------------------------------


def test_token_claims_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        GOLDEN_CLAIMS.exp = 0  # type: ignore[misc]


def test_token_claims_is_slotted():
    assert not hasattr(GOLDEN_CLAIMS, "__dict__")
