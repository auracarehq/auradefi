"""Append-only audit log for token mints (SPEC §7.2 — Vezgo has nothing).

Pinned record shape (docs/DECISIONS.md "Audit record shape"):
``{seq (per-project, from 1), event: "token.minted", project_id,
external_user_id, key_id, ip, at_ms}`` — append-only, no
delete/update/clear. Only ``token.minted`` is recorded tonight.
"""

from __future__ import annotations

from dataclasses import dataclass

from auradefi.clock import Clock


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One immutable audit entry; ``at_ms`` is ms epoch."""

    seq: int
    event: str
    project_id: str
    external_user_id: str
    key_id: str
    ip: str
    at_ms: int


class AuditLog:
    """Project-scoped, append-only audit log; all state on the instance.

    Deliberately exposes NO mutation surface: no remove, delete, clear,
    or update. ``entries()`` returns a fresh tuple, never a live view.
    """

    def __init__(self) -> None:
        """Start empty; sequences are per-project and start at 1."""
        self._records: dict[str, list[AuditRecord]] = {}

    def record_token_mint(
        self,
        project_id: str,
        external_user_id: str,
        key_id: str,
        ip: str,
        clock: Clock,
    ) -> AuditRecord:
        """Append and return a record for one token mint.

        ``seq`` is per-project, starting at 1. ``event`` is exactly
        ``"token.minted"``. ``at_ms`` comes from ``clock.now_ms()``.
        """
        records = self._records.setdefault(project_id, [])
        record = AuditRecord(
            seq=len(records) + 1,
            event="token.minted",
            project_id=project_id,
            external_user_id=external_user_id,
            key_id=key_id,
            ip=ip,
            at_ms=clock.now_ms(),
        )
        records.append(record)
        return record

    def entries(self, project_id: str) -> tuple[AuditRecord, ...]:
        """This project's records as a fresh tuple, in insertion order.

        An unknown project yields ``()`` — never an error, so the log is
        not a tenant-existence probe surface.
        """
        return tuple(self._records.get(project_id, ()))
