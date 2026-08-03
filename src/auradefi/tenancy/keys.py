"""Scoped API keys: issue, hash-at-rest, authenticate, rotate, revoke.

SPEC §7.2 ("Scoped keys", "Key rotation"): separate keys per environment,
independent rotation with an overlap window, hashed at rest. The wire
format is PINNED in docs/DECISIONS.md ("API key format"):

* plaintext = ``f"adk_{environment}_{body}"``, environment ∈
  {``live``, ``test``} (both 4 chars), body = ``entropy(24)`` = 48
  lowercase hex chars (default ``secrets.token_hex``), total length 57;
* stored ``prefix`` = ``plaintext[:17]``; stored ``secret_hash`` =
  ``sha256(plaintext.encode("utf-8")).hexdigest()`` — the plaintext is
  returned exactly once at issue/rotate and never stored;
* authentication compares sha256 hexdigests via ``hmac.compare_digest``,
  never ``str.__eq__``.

Unknown key, wrong secret, revoked, and expired all raise the SAME
class — plain :class:`auradefi.errors.AuthError` — so a probing caller
cannot distinguish failure modes.

Rotation issues a fresh key with the same project/environment/scopes and
sets the old key's ``expires_at = now_ms + overlap_ms``: during the
overlap window BOTH plaintexts authenticate; at and after ``expires_at``
(``now_ms >= expires_at``, exclusive of the last live millisecond) only
the new one does.

In-memory instance dicts — Phase 5 extracts a port from this interface.
stdlib only; time arrives through a :class:`~auradefi.clock.Clock`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import secrets
from collections.abc import Callable, Iterable

from auradefi.clock import Clock
from auradefi.errors import AuthError, NotFoundError
from auradefi.tenancy.models import ApiKey, Environment, Scope, new_key_id

# One message for every AuthError: unknown, wrong-secret, revoked, and
# expired must be indistinguishable to a probing caller (SPEC §7.2).
_REJECTED = "api key failed authentication"


def _hexdigest(plaintext: str) -> str:
    """The stored form: sha256 hexdigest of the UTF-8 plaintext."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class ApiKeyStore:
    """In-memory store of scoped, per-environment API keys (SPEC §7.2).

    ``entropy`` is injectable for deterministic tests; it must mirror
    ``secrets.token_hex``: ``entropy(n)`` returns ``2 * n`` lowercase
    hex characters.
    """

    def __init__(self, entropy: Callable[[int], str] = secrets.token_hex) -> None:
        self._entropy = entropy
        self._keys: dict[str, ApiKey] = {}

    def issue(
        self,
        project_id: str,
        environment: Environment,
        scopes: Iterable[Scope],
        clock: Clock,
    ) -> tuple[ApiKey, str]:
        """Issue a new key; return ``(record, plaintext)``.

        The record is a :class:`~auradefi.tenancy.models.ApiKey` with
        ``id = "key_" + entropy(8)``, ``prefix = plaintext[:17]``,
        ``secret_hash = sha256(plaintext).hexdigest()``, ``scopes`` a
        frozenset, ``created_at = clock.now_ms()``, and
        ``expires_at``/``revoked_at`` unset. The plaintext
        (``adk_{environment}_{entropy(24)}``, length 57) is returned
        exactly once, here — it is never stored.
        """
        plaintext = f"adk_{environment.value}_{self._entropy(24)}"
        record = ApiKey(
            id=new_key_id(self._entropy),
            project_id=project_id,
            environment=environment,
            prefix=plaintext[:17],
            secret_hash=_hexdigest(plaintext),
            scopes=frozenset(scopes),
            created_at=clock.now_ms(),
        )
        self._keys[record.id] = record
        return record, plaintext

    def authenticate(self, plaintext: str, clock: Clock) -> ApiKey:
        """Return the record for ``plaintext``, or raise plain AuthError.

        Comparison is ``hmac.compare_digest`` over sha256 hexdigests —
        never ``str.__eq__``. Unknown/garbage/wrong-secret, revoked
        (``revoked_at`` set), and expired (``expires_at is not None and
        clock.now_ms() >= expires_at``) all raise the SAME class,
        :class:`auradefi.errors.AuthError` — probing cannot distinguish.
        """
        candidate = _hexdigest(plaintext).encode("ascii")
        matched: ApiKey | None = None
        for record in self._keys.values():
            # No early exit: every stored digest is compared, so a miss
            # costs the same regardless of where a near-match sits.
            if hmac.compare_digest(candidate, record.secret_hash.encode("ascii")):
                matched = record
        if matched is None:
            raise AuthError(_REJECTED)
        if matched.revoked_at is not None:
            raise AuthError(_REJECTED)
        if matched.expires_at is not None and clock.now_ms() >= matched.expires_at:
            raise AuthError(_REJECTED)
        return matched

    def rotate(self, key_id: str, overlap_ms: int, clock: Clock) -> tuple[ApiKey, str]:
        """Rotate ``key_id``; return the fresh ``(record, plaintext)``.

        The fresh key shares the old key's project_id, environment, and
        scopes, with a new id and plaintext. The old key gets
        ``expires_at = clock.now_ms() + overlap_ms``: both plaintexts
        authenticate during the overlap window; after it, only the new
        one. Unknown ``key_id`` raises
        :class:`auradefi.errors.NotFoundError`.
        """
        old = self._keys.get(key_id)
        if old is None:
            raise NotFoundError(f"api key not found: {key_id}")
        fresh, plaintext = self.issue(old.project_id, old.environment, old.scopes, clock)
        self._keys[key_id] = dataclasses.replace(
            old, expires_at=clock.now_ms() + overlap_ms
        )
        return fresh, plaintext

    def revoke(self, key_id: str, clock: Clock) -> None:
        """Set ``revoked_at = clock.now_ms()`` on ``key_id``. Immediate:
        authentication fails from this instant. Idempotent: revoking an
        already-revoked key is a no-op (the first ``revoked_at`` stands).
        Unknown ``key_id`` raises :class:`auradefi.errors.NotFoundError`.
        """
        record = self._keys.get(key_id)
        if record is None:
            raise NotFoundError(f"api key not found: {key_id}")
        if record.revoked_at is not None:
            return
        self._keys[key_id] = dataclasses.replace(record, revoked_at=clock.now_ms())

    def keys_for(self, project_id: str) -> tuple[ApiKey, ...]:
        """All records for ``project_id`` — and no other project's."""
        return tuple(
            record
            for record in self._keys.values()
            if record.project_id == project_id
        )


def has_scope(key: ApiKey, scope: Scope) -> bool:
    """True iff ``scope`` is in ``key.scopes`` (exact member, not prefix)."""
    return scope in key.scopes
