"""Scoped API keys: issue, hash-at-rest, authenticate, rotate, revoke.

SPEC §7.2 ("Scoped keys", "Key rotation"): separate keys per environment,
independent rotation with an overlap window, hashed at rest. The wire
format is PINNED in docs/internal/DECISIONS.md ("API key format"):

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
the new one does. An expiry is only ever SHORTENED — rotating a key that
already dies sooner than ``now_ms + overlap_ms`` leaves the earlier
expiry standing, and the fresh key inherits the window it was rotated out
of, so no rotation buys a dying credential more time (RELEASE_0.1.1 #25b).
A revoked or expired key cannot be rotated at all: minting from one would
carry its FULL SCOPE SET onto a live key, so a bulk rotation job would
silently re-privilege what an operator deliberately revoked (#25a).

``rotate`` and ``revoke`` are tenant-gated on ``project_id`` (#25c) and
answer another project's key id EXACTLY as they answer an id that exists
nowhere — see ``_NOT_FOUND``.

Scopes are coerced to :class:`~auradefi.tenancy.models.Scope` members at
this boundary (#35); an unrecognised scope string is refused. So are the
two millisecond arguments: a non-int ``expires_at``/``overlap_ms``, and an
``expires_at`` at or before ``now_ms``, are refused rather than stored —
the store never holds an instant ``authenticate`` cannot compare, nor
issues a credential born unable to authenticate.

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
from auradefi.errors import AuthError, ConflictError, NotFoundError, ValidationError
from auradefi.tenancy.models import ApiKey, Environment, Scope, new_key_id

# One message for every AuthError: unknown, wrong-secret, revoked, and
# expired must be indistinguishable to a probing caller (SPEC §7.2).
_REJECTED = "api key failed authentication"

# One CONSTANT message for every NotFoundError this store raises. The class
# is NotFoundError, not AuthError or TenantIsolationError, because the
# caller's own credential authenticated fine and the failure is a scoped
# lookup miss — the idiom tenancy/store.py states for every tenant-scoped
# read: "an entity that exists under another project is INDISTINGUISHABLE
# from one that does not exist at all". Unlike store.py this message
# interpolates NOTHING: the smuggled id and the absent id are different
# strings, so ``f"api key not found: {key_id}"`` would make the two answers
# differ byte-for-byte, and that difference IS the tenant-existence probe
# (RELEASE_0.1.1 #25c).
_NOT_FOUND = "api key not found"


def _hexdigest(plaintext: str) -> str:
    """The stored form: sha256 hexdigest of the UTF-8 plaintext."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _coerce_scopes(scopes: Iterable[Scope | str]) -> frozenset[Scope]:
    """``scopes`` as a frozenset of :class:`Scope`, or ValidationError.

    Coercion belongs at the store boundary because :class:`Scope` is a
    ``StrEnum``: a plain ``"accounts:read"`` satisfies ``scope in
    key.scopes``, so a key rehydrated from JSON or SQL authenticates
    everywhere and only breaks later, where a member-only attribute
    access turns into an unformatted 500. An unrecognised scope string
    is REFUSED — the store never keeps a privilege it cannot name
    (RELEASE_0.1.1 #35, store half).
    """
    coerced: set[Scope] = set()
    for scope in scopes:
        try:
            coerced.add(Scope(scope))
        except ValueError as exc:
            raise ValidationError(f"unknown api key scope: {scope!r}") from exc
    return frozenset(coerced)


def _checked_ms(value: object, label: str) -> int:
    """``value`` unchanged iff it is a plain ``int``, else ValidationError.

    Project rule: "All timestamps are millisecond-epoch integers." A
    ``float`` or ``str`` that gets past this boundary is stored happily and
    only fails later, on the authentication hot path, where
    ``clock.now_ms() >= expires_at`` raises a ``builtins.TypeError`` — an
    undeclared exception class and exactly the unformatted 500 that
    RELEASE_0.1.1 #34/#35 exist to remove. ``bool`` is excluded
    explicitly: it satisfies ``isinstance(_, int)`` and is never an
    instant or a duration.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{label} must be an integer of milliseconds: {value!r}")
    return value


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
        scopes: Iterable[Scope | str],
        clock: Clock,
        expires_at: int | None = None,
    ) -> tuple[ApiKey, str]:
        """Issue a new key; return ``(record, plaintext)``.

        The record is a :class:`~auradefi.tenancy.models.ApiKey` with
        ``id = "key_" + entropy(8)``, ``prefix = plaintext[:17]``,
        ``secret_hash = sha256(plaintext).hexdigest()``, ``scopes`` a
        frozenset of :class:`Scope` MEMBERS (wire strings are coerced,
        unknown strings refused — see :func:`_coerce_scopes`),
        ``created_at = clock.now_ms()``, and ``revoked_at`` unset.

        ``expires_at`` is an absolute ms-epoch instant or ``None`` for a
        key that never expires; a key with a finite expiry is what
        rotation must never widen (RELEASE_0.1.1 #25b), and
        :meth:`rotate` uses it to hand the fresh key the window it
        inherited. It must be an ``int`` STRICTLY after ``clock.now_ms()``
        — ``authenticate`` treats ``now_ms >= expires_at`` as dead, so an
        earlier instant would issue a credential that can never
        authenticate yet still shows up in :meth:`keys_for`. Either
        violation raises :class:`auradefi.errors.ValidationError`.

        The plaintext (``adk_{environment}_{entropy(24)}``, length 57) is
        returned exactly once, here — it is never stored.
        """
        # Validated BEFORE any entropy is spent or anything is stored: a
        # refused scope or expiry leaves no key and no half-written state.
        coerced = _coerce_scopes(scopes)
        now_ms = clock.now_ms()
        if expires_at is not None:
            expires_at = _checked_ms(expires_at, "expires_at")
            if expires_at <= now_ms:
                raise ValidationError(
                    f"expires_at must be after now ({now_ms}): {expires_at}"
                )
        return self._mint(project_id, environment, coerced, now_ms, expires_at)

    def _mint(
        self,
        project_id: str,
        environment: Environment,
        scopes: frozenset[Scope],
        now_ms: int,
        expires_at: int | None,
    ) -> tuple[ApiKey, str]:
        """Store a fresh record and return ``(record, plaintext)``.

        The mint step with NO validation: both callers check their own
        inputs and read the clock exactly ONCE, so :meth:`rotate` cannot
        have the expiry it just proved live rejected by a second, later
        ``now_ms`` that overtook it mid-call.
        """
        plaintext = f"adk_{environment.value}_{self._entropy(24)}"
        record = ApiKey(
            id=new_key_id(self._entropy),
            project_id=project_id,
            environment=environment,
            prefix=plaintext[:17],
            secret_hash=_hexdigest(plaintext),
            scopes=scopes,
            created_at=now_ms,
            expires_at=expires_at,
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

    def _owned(self, project_id: str, key_id: str) -> ApiKey:
        """The record ``key_id`` under ``project_id``, else NotFoundError.

        The tenant gate for every mutation (RELEASE_0.1.1 #25c). A key
        that belongs to another project takes the SAME branch as one that
        exists nowhere, so the answer cannot be used to probe for the
        existence of another tenant's key id.
        """
        record = self._keys.get(key_id)
        if record is None or record.project_id != project_id:
            raise NotFoundError(_NOT_FOUND)
        return record

    def rotate(
        self,
        project_id: str,
        key_id: str,
        overlap_ms: int,
        clock: Clock,
    ) -> tuple[ApiKey, str]:
        """Rotate ``project_id``'s ``key_id``; return fresh ``(record,
        plaintext)``.

        The fresh key shares the old key's project_id, environment, and
        scopes, with a new id and plaintext, and INHERITS the old key's
        ``expires_at`` unchanged — a rotation cannot outlive the window it
        was rotated out of, whatever ``overlap_ms`` asks for (#25b).

        The old key gets ``expires_at = clock.now_ms() + overlap_ms``,
        never later than an expiry it already had: both plaintexts
        authenticate during the overlap window; after it, only the new
        one. An expiry is only ever shortened.

        A revoked or expired key is REFUSED with
        :class:`auradefi.errors.ConflictError` — the id is real and owned
        by the caller (so not NotFoundError) and the caller's own
        credential authenticated fine (so not AuthError); what fails is a
        precondition on existing state. Minting from a dead key would
        carry its full scope set onto a live one (#25a).

        An unknown ``key_id`` — or one belonging to another project —
        raises :class:`auradefi.errors.NotFoundError`.

        ``overlap_ms`` is a non-negative ``int`` duration; anything else
        raises :class:`auradefi.errors.ValidationError` before the store is
        touched (a ``float`` would write a float into an ms-int field, a
        negative one an expiry before ``created_at``). The check precedes
        the tenant gate deliberately: it depends on ``overlap_ms`` alone,
        so it answers identically for an owned, a smuggled and an absent
        key id and adds no probe.
        """
        overlap_ms = _checked_ms(overlap_ms, "overlap_ms")
        if overlap_ms < 0:
            raise ValidationError(f"overlap_ms must not be negative: {overlap_ms}")
        old = self._owned(project_id, key_id)
        now_ms = clock.now_ms()
        if old.revoked_at is not None:
            raise ConflictError("api key is revoked and cannot be rotated", old.id)
        if old.expires_at is not None and now_ms >= old.expires_at:
            raise ConflictError("api key is expired and cannot be rotated", old.id)
        fresh, plaintext = self._mint(
            old.project_id, old.environment, old.scopes, now_ms, old.expires_at
        )
        overlap_expiry = now_ms + overlap_ms
        if old.expires_at is not None:
            overlap_expiry = min(overlap_expiry, old.expires_at)
        self._keys[key_id] = dataclasses.replace(old, expires_at=overlap_expiry)
        return fresh, plaintext

    def revoke(self, project_id: str, key_id: str, clock: Clock) -> None:
        """Set ``revoked_at = clock.now_ms()`` on ``project_id``'s
        ``key_id``. Immediate: authentication fails from this instant.
        Idempotent: revoking an already-revoked key is a no-op (the first
        ``revoked_at`` stands). An unknown ``key_id`` — or one belonging
        to another project — raises
        :class:`auradefi.errors.NotFoundError`.
        """
        record = self._owned(project_id, key_id)
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
