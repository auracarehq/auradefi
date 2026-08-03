"""Tenant-scoped org/project/user/connection store with an audited mint flow.

SPEC §3.1: the Project is the tenant boundary — nothing crosses it. Every
read and write on this store is keyed by ``project_id`` first, and an
entity that exists under another project is INDISTINGUISHABLE from one
that does not exist at all (``NotFoundError``, same class, same shape of
message — never a hint that the id was real somewhere else).

SPEC §7.1: ``external_user_id`` is the entire tenancy model. There is no
user-creation endpoint; a user exists as a side effect of minting a token
— get-or-create, idempotent, the same string always resolves to the same
user. Duplicate connections answer 409-style with the existing id
(``ConflictError.existing_id``).

SPEC §7.2: users are project-scoped (there is deliberately NO method that
lists across projects), email-shaped external ids are rejected
(``ValidationError``), and every successful token mint is audited.

``Project.signing_secret`` — ``entropy(32)``, 64 hex chars, unique per
project — is THE isolation root: a token minted under one project can
never verify under another.

This module MUST NOT import ``auradefi.tenancy.keys``: ``key_id`` is a
passed-in datum here, and Phase 8 wires key auth to the mint flow.
Imports: tenancy.models/tokens/audit/quota + foundation; stdlib only.
All state lives in instance dicts; all timestamps are ms-epoch ints.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable

from auradefi.clock import Clock
from auradefi.errors import ConflictError, NotFoundError
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.models import (
    Connection,
    ConnectionKind,
    EndUser,
    Environment,
    Organisation,
    Project,
    connection_id,
    end_user_id,
    new_org_id,
    new_project_id,
    normalize_descriptor,
    validate_external_user_id,
)
from auradefi.tenancy.quota import QuotaCounter
from auradefi.tenancy.tokens import mint_token


class TenancyStore:
    """In-memory tenant store; the Phase 2 reference for the ledger port.

    All state is held in instance dicts — two stores never share state.
    ``entropy`` is injectable for deterministic tests and defaults to
    ``secrets.token_hex`` (n bytes → 2n lowercase hex chars).
    """

    def __init__(self, entropy: Callable[[int], str] = secrets.token_hex) -> None:
        """Start empty; bind the entropy source used for all random ids."""
        self._entropy = entropy
        self._orgs: dict[str, Organisation] = {}
        self._projects: dict[str, Project] = {}
        # project_id -> end-user id -> EndUser, in creation order.
        self._users: dict[str, dict[str, EndUser]] = {}
        # project_id -> connection id -> Connection, in creation order.
        self._connections: dict[str, dict[str, Connection]] = {}

    def _require_project(self, project_id: str) -> Project:
        """Return the project or raise ``NotFoundError`` — the tenant gate.

        Every tenant-scoped method passes through here first, so an
        unknown project fails identically everywhere.
        """
        project = self._projects.get(project_id)
        if project is None:
            raise NotFoundError(f"project not found: {project_id!r}")
        return project

    def create_organisation(self, name: str, clock: Clock) -> Organisation:
        """Create and return an Organisation (SPEC §3.1: billing boundary).

        ``id = models.new_org_id(entropy)``; ``created_at`` is
        ``clock.now_ms()``.
        """
        org = Organisation(
            id=new_org_id(self._entropy),
            name=name,
            created_at=clock.now_ms(),
        )
        self._orgs[org.id] = org
        return org

    def create_project(
        self,
        org_id: str,
        name: str,
        environment: Environment,
        clock: Clock,
    ) -> Project:
        """Create and return a Project under ``org_id`` (the tenant boundary).

        ``id = models.new_project_id(entropy)``; ``signing_secret =
        entropy(32)`` — 64 hex chars, unique per project, THE isolation
        root. Unknown ``org_id`` raises
        ``auradefi.errors.NotFoundError``.
        """
        if org_id not in self._orgs:
            raise NotFoundError(f"organisation not found: {org_id!r}")
        project = Project(
            id=new_project_id(self._entropy),
            org_id=org_id,
            name=name,
            environment=environment,
            signing_secret=self._entropy(32),
            created_at=clock.now_ms(),
        )
        self._projects[project.id] = project
        self._users[project.id] = {}
        self._connections[project.id] = {}
        return project

    def get_or_create_user(
        self,
        project_id: str,
        external_user_id: str,
        clock: Clock,
    ) -> EndUser:
        """Get-or-create the project's user for ``external_user_id`` (§7.1).

        Validates via ``models.validate_external_user_id``
        (``ValidationError`` on email-shaped input, nothing created).
        ``id = models.end_user_id(project_id, external_user_id)``.
        IDEMPOTENT: a second call — even at a later clock time — returns
        a record equal to the first, INCLUDING the original
        ``created_at``, and never a duplicate. Unknown ``project_id``
        raises ``NotFoundError``.
        """
        self._require_project(project_id)
        validate_external_user_id(external_user_id)
        user_id = end_user_id(project_id, external_user_id)
        existing = self._users[project_id].get(user_id)
        if existing is not None:
            return existing
        user = EndUser(
            id=user_id,
            project_id=project_id,
            external_user_id=external_user_id,
            created_at=clock.now_ms(),
        )
        self._users[project_id][user_id] = user
        return user

    def users(self, project_id: str) -> tuple[EndUser, ...]:
        """This project's users as a fresh tuple, in creation order (§7.2).

        Strictly project-scoped — no cross-project list method exists on
        this class, by design. Unknown ``project_id`` raises
        ``NotFoundError``.
        """
        self._require_project(project_id)
        return tuple(self._users[project_id].values())

    def create_connection(
        self,
        project_id: str,
        end_user_id: str,
        kind: ConnectionKind,
        descriptor: str,
        clock: Clock,
    ) -> Connection:
        """Create and return a Connection for this project's user (§3.1).

        A user that is not in THIS project raises ``NotFoundError`` —
        another tenant's user is indistinguishable from a missing one.
        ``id = models.connection_id(...)`` over the NORMALIZED
        descriptor, and the stored ``descriptor`` is the normalized
        form. A duplicate (same project + user + kind + normalized
        descriptor — case-differing EVM addresses collide on purpose)
        raises ``auradefi.errors.ConflictError`` with ``existing_id``
        set to the existing connection's id (§7.1's 409).
        """
        self._require_project(project_id)
        if end_user_id not in self._users[project_id]:
            raise NotFoundError(f"end user not found: {end_user_id!r}")
        new_id = connection_id(project_id, end_user_id, kind, descriptor)
        existing = self._connections[project_id].get(new_id)
        if existing is not None:
            raise ConflictError(
                f"connection already exists: {existing.id!r}",
                existing_id=existing.id,
            )
        connection = Connection(
            id=new_id,
            project_id=project_id,
            end_user_id=end_user_id,
            kind=kind,
            descriptor=normalize_descriptor(kind, descriptor),
            created_at=clock.now_ms(),
        )
        self._connections[project_id][new_id] = connection
        return connection

    def get_connection(self, project_id: str, connection_id: str) -> Connection:
        """Return this project's connection by id.

        Cross-tenant and absent are the SAME failure: ``NotFoundError``,
        with a message that never mentions any other project.
        """
        self._require_project(project_id)
        connection = self._connections[project_id].get(connection_id)
        if connection is None:
            raise NotFoundError(f"connection not found: {connection_id!r}")
        return connection

    def connections(
        self,
        project_id: str,
        end_user_id: str,
    ) -> tuple[Connection, ...]:
        """This project user's connections as a fresh tuple, in creation order."""
        self._require_project(project_id)
        return tuple(
            connection
            for connection in self._connections[project_id].values()
            if connection.end_user_id == end_user_id
        )

    def mint_user_token(
        self,
        project_id: str,
        external_user_id: str,
        scopes: Iterable[str],
        ttl_ms: int,
        ip: str,
        key_id: str,
        clock: Clock,
        audit: AuditLog,
        quota: QuotaCounter | None = None,
        jti: str | None = None,
    ) -> str:
        """Mint a user token under the project's signing secret (§7.1/§7.2).

        Exact order:

        1. ``quota.hit(project_id)`` FIRST when ``quota`` is given —
           ``QuotaExceededError`` propagates with nothing minted,
           nothing created, nothing audited;
        2. ``get_or_create_user`` (§7.1: the user exists as a side
           effect of minting);
        3. ``tokens.mint_token`` with the project's ``signing_secret``;
        4. ``audit.record_token_mint`` — EVERY successful mint is
           audited; failures never are.

        ``key_id`` and ``ip`` are passed-in data recorded to the audit
        log (key auth arrives in Phase 8 — this module never imports
        ``tenancy.keys``). Unknown ``project_id`` raises
        ``NotFoundError``; email-shaped ``external_user_id`` raises
        ``ValidationError``.
        """
        if quota is not None:
            quota.hit(project_id)
        project = self._require_project(project_id)
        self.get_or_create_user(project_id, external_user_id, clock)
        token = mint_token(
            signing_secret=project.signing_secret,
            project_id=project_id,
            external_user_id=external_user_id,
            scopes=scopes,
            ttl_ms=ttl_ms,
            clock=clock,
            jti=jti,
        )
        audit.record_token_mint(project_id, external_user_id, key_id, ip, clock)
        return token
