"""Tenancy value objects, deterministic ids, opaque-id invariant.

The tenant graph of SPEC §3.1, ``Organisation → Project`` (with api
keys) ``→ EndUser`` (via ``external_user_id``) ``→ Connection``, plus
the deterministic-id algorithms pinned in docs/internal/DECISIONS.md and the
SPEC §7.2 hardening: scoped keys, per-environment keys, and opaque ids
enforced as an invariant (the ``external_user_id`` charset excludes
``@``, so email-shaped input is structurally impossible, it is a
bearer-equivalent secret, and an email is guessable).

``Project.signing_secret`` is the per-project JWT HMAC secret: the
isolation root. All timestamps are ms-epoch ints. stdlib only.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from auradefi.errors import ValidationError

_EXTERNAL_USER_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class Scope(StrEnum):
    """API-key scopes (SPEC §7.2, exact wire strings).

    A read-only analytics service must not be able to mint tokens or
    delete accounts.
    """

    ACCOUNTS_READ = "accounts:read"
    ACCOUNTS_WRITE = "accounts:write"
    SYNC_TRIGGER = "sync:trigger"
    USERS_ADMIN = "users:admin"


class Environment(StrEnum):
    """Per-environment keys, independent rotation (SPEC §7.2).

    Both values are exactly 4 characters: the API-key wire format
    (``adk_{env}_{body}``, total length 57) depends on that.
    """

    LIVE = "live"
    TEST = "test"


class ConnectionKind(StrEnum):
    """What a Connection watches: an address, an xpub, an exchange key
    (SPEC §3.1)."""

    ADDRESS = "address"
    XPUB = "xpub"
    EXCHANGE = "exchange"


@dataclass(frozen=True, slots=True)
class Organisation:
    """Billing + quota boundary (SPEC §3.1). ``created_at`` is ms-epoch."""

    id: str
    name: str
    created_at: int


@dataclass(frozen=True, slots=True)
class Project:
    """The tenant boundary; nothing crosses it (SPEC §3.1, §7).

    ``signing_secret`` is the per-project JWT HMAC secret: the
    isolation root: a token minted under one project can never verify
    under another. ``environment`` separates live from test keys.
    """

    id: str
    org_id: str
    name: str
    environment: Environment
    signing_secret: str
    created_at: int


@dataclass(frozen=True, slots=True)
class EndUser:
    """A host user inside one project, keyed by opaque ``external_user_id``.

    ``id`` is deterministic. See :func:`end_user_id`. The
    ``external_user_id`` has already passed
    :func:`validate_external_user_id` (SPEC §7.2: opaque, never PII).
    """

    id: str
    project_id: str
    external_user_id: str
    created_at: int


@dataclass(frozen=True, slots=True)
class Connection:
    """One credentialed-or-watched source (≡ Plaid Item, SPEC §3.1).

    ``id`` is deterministic. See :func:`connection_id`. ``descriptor``
    is stored in normalized form (:func:`normalize_descriptor`).
    """

    id: str
    project_id: str
    end_user_id: str
    kind: ConnectionKind
    descriptor: str
    created_at: int


@dataclass(frozen=True, slots=True)
class ApiKey:
    """A scoped, per-environment project key (SPEC §7.2).

    Only ``prefix`` (plaintext[:17]) and ``secret_hash`` (sha256 hex of
    the plaintext) are stored at rest, never the plaintext. ``scopes``
    is a frozenset of :class:`Scope`. ``expires_at``/``revoked_at`` are
    ms-epoch ints, ``None`` while unset.
    """

    id: str
    project_id: str
    environment: Environment
    prefix: str
    secret_hash: str
    scopes: frozenset[Scope]
    created_at: int
    expires_at: int | None = None
    revoked_at: int | None = None


def validate_external_user_id(value: str) -> str:
    """Return ``value`` unchanged iff it fullmatches ``[A-Za-z0-9._:-]{1,128}``.

    Raises ``auradefi.errors.ValidationError`` otherwise. The charset
    excludes ``@``, so email-shaped input is structurally impossible
    (SPEC §7.2: an external_user_id is a bearer-equivalent secret and
    an email is guessable, Vezgo's own OpenAPI example,
    ``user@example.dev``, is rejected by name).
    """
    if not _EXTERNAL_USER_ID_RE.fullmatch(value):
        raise ValidationError(
            f"external_user_id must fullmatch [A-Za-z0-9._:-]{{1,128}}: {value!r}"
        )
    return value


def normalize_descriptor(kind: ConnectionKind, descriptor: str) -> str:
    """``descriptor.strip()``, then lowercased iff ``kind`` is ADDRESS
    and the stripped value starts with ``"0x"`` (DECISIONS pinned).

    XPUB and EXCHANGE descriptors are never lowercased. Xpubs are
    base58, where case is significant.
    """
    stripped = descriptor.strip()
    if kind is ConnectionKind.ADDRESS and stripped.startswith("0x"):
        return stripped.lower()
    return stripped


def end_user_id(project_id: str, external_user_id: str) -> str:
    """Deterministic end-user id (DECISIONS pinned).

    ``"usr_" + sha256(f"{project_id}|{external_user_id}".encode())
    .hexdigest()[:16]``. Same (project, external id) pair → same id;
    the same external id under another project → a different id.
    """
    digest = hashlib.sha256(f"{project_id}|{external_user_id}".encode()).hexdigest()
    return "usr_" + digest[:16]


def connection_id(
    project_id: str,
    end_user_id: str,
    kind: ConnectionKind,
    descriptor: str,
) -> str:
    """Deterministic connection id (DECISIONS pinned).

    ``"conn_" + sha256(f"{project_id}|{end_user_id}|{kind}|{normalized}"
    .encode()).hexdigest()[:16]`` where ``normalized`` is
    :func:`normalize_descriptor` applied to ``descriptor``, so a
    mixed-case and a lowercase EVM address yield the SAME id.
    """
    normalized = normalize_descriptor(kind, descriptor)
    digest = hashlib.sha256(
        f"{project_id}|{end_user_id}|{kind}|{normalized}".encode()
    ).hexdigest()
    return "conn_" + digest[:16]


def new_org_id(entropy: Callable[[int], str] = secrets.token_hex) -> str:
    """``"org_" + entropy(8)``: 16 hex chars from 8 random bytes."""
    return "org_" + entropy(8)


def new_project_id(entropy: Callable[[int], str] = secrets.token_hex) -> str:
    """``"proj_" + entropy(8)``: 16 hex chars from 8 random bytes."""
    return "proj_" + entropy(8)


def new_key_id(entropy: Callable[[int], str] = secrets.token_hex) -> str:
    """``"key_" + entropy(8)``: 16 hex chars from 8 random bytes."""
    return "key_" + entropy(8)
