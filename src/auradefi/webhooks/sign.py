"""Webhook signing and verification: the v1 scheme (SPEC §7.3, rule #8).

Vezgo authenticates its webhooks by SOURCE-IP ALLOWLIST, which is
unusable behind most PaaS ingress and proves nothing about the payload.
This is the replacement, and it is the whole authentication story: there
is no IP allowlist and no manual URL whitelisting anywhere in auradefi.

Pinned in docs/internal/DECISIONS.md ("Webhook signature (v1)"):

    X-Auradefi-Signature: v1=<hex>
    <hex> = hmac.new(secret.encode("utf-8"),
                     f"{timestamp_ms}.{body}".encode("utf-8"),
                     hashlib.sha256).hexdigest()
    X-Auradefi-Timestamp: str(timestamp_ms)      # ms epoch

The signed ``body`` is the EXACT string POSTed: the deliverer sends
``content=body.encode("utf-8")``, never ``json=``, because a
re-serialisation would change the bytes the receiver hashes.

Verification is ``hmac.compare_digest`` plus a replay window (default
300_000 ms, boundary INCLUSIVE). A bad signature and a stale timestamp
raise the SAME class with the SAME message, plain
:class:`auradefi.errors.AuthError`, so a probing receiver cannot tell
which check it failed.

Stdlib only; no httpx here.
"""

from __future__ import annotations

import hashlib
import hmac

from auradefi.errors import AuthError

#: Scheme version; the signature header value is ``f"{VERSION}={hex}"``.
SIGNATURE_VERSION = "v1"

SIGNATURE_HEADER = "X-Auradefi-Signature"
TIMESTAMP_HEADER = "X-Auradefi-Timestamp"
EVENT_HEADER = "X-Auradefi-Event"
DELIVERY_HEADER = "X-Auradefi-Delivery"

#: Replay window, milliseconds; ``abs(now_ms - timestamp_ms) >`` this is
#: rejected, so the boundary itself is accepted.
DEFAULT_TOLERANCE_MS = 300_000

# One message for every rejection: a mismatched signature and a stale
# timestamp must be indistinguishable to a probing receiver.
_REJECTED = "webhook signature verification failed"


def sign(secret: str, timestamp_ms: int, body: str) -> str:
    """Return the ``X-Auradefi-Signature`` value for ``body``.

    ``f"v1={hmac_sha256(secret, f'{timestamp_ms}.{body}')}"``: 67
    characters: the ``v1=`` prefix plus 64 lowercase hex. Both the
    secret and the signed string are encoded UTF-8.
    """
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp_ms}.{body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify_signature(
    secret: str,
    timestamp_ms: int,
    body: str,
    signature: str,
    now_ms: int,
    tolerance_ms: int = DEFAULT_TOLERANCE_MS,
) -> None:
    """Return ``None`` iff ``signature`` is valid AND fresh.

    Raises plain :class:`auradefi.errors.AuthError`: one class, one
    message, for every failure: a mismatched signature, a missing or
    wrong ``v1=`` prefix, a mutated body, a wrong secret, and a
    timestamp outside the window are INDISTINGUISHABLE to the caller.

    Freshness is ``abs(now_ms - timestamp_ms) <= tolerance_ms``
    (inclusive at both edges, past and future). Comparison is
    ``hmac.compare_digest``, never ``str.__eq__``.
    """
    expected = sign(secret, timestamp_ms, body)
    # Both checks always run, and one raise site serves both: neither the
    # exception nor the time taken says which of them failed.
    matched = hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))
    fresh = abs(now_ms - timestamp_ms) <= tolerance_ms
    if not (matched and fresh):
        raise AuthError(_REJECTED)
