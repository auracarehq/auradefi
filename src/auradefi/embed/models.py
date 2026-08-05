"""Embed identity and sync-report value objects (SPEC §8, §7.1).

Single-tenant embedding: the tenant id derives deterministically from
the host's opaque ``external_user_id`` under a CONFIGURABLE project id
defaulting to ``"embed"``. Get-or-create, idempotent, the same string
always resolves to the same tenant (SPEC §7.1). A host that also runs
the HTTP API over one ledger sets ``Settings.project_id`` to its real
project so both surfaces derive ONE tenant (RELEASE_0.1.1 §5 #19).

Duplication waiver (docs/internal/DECISIONS.md): :func:`derive_tenant_id` is a
value-identical local copy of the pinned ``tenancy.models.end_user_id``
formula: the layer contract forbids embed→tenancy imports.
Tests/golden/test_embed_ids.py cross-pins both sides to the same bytes,
so drift is a red test, not a debate. The CONNECTION-ID half of that
waiver was retired in 0.1.1: :func:`derive_connection_id` hashes
``chain_id`` too and is deliberately no longer byte-equal to
``tenancy.connection_id`` (§5 #26).

All timestamps are ms-epoch ints. stdlib + auradefi.errors only; NO
table classes.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from auradefi.errors import ValidationError

EMBED_PROJECT_ID = "embed"


def derive_tenant_id(
    external_user_id: str, project_id: str = EMBED_PROJECT_ID
) -> str:
    """Deterministic single-tenant id (DECISIONS-pinned formula).

    Validates the pinned opaque-id invariant VERBATIM,
    ``re.fullmatch(r"[A-Za-z0-9._:-]{1,128}")``, raising
    ``auradefi.errors.ValidationError`` otherwise (SPEC §7.2: the
    charset excludes ``@``, so email-shaped input is structurally
    impossible; it is a bearer-equivalent secret and an email is
    guessable). Then returns
    ``"usr_" + sha256(f"{project_id}|{external_user_id}".encode())
    .hexdigest()[:16]``: the pinned ``end_user_id`` formula.

    ``project_id`` defaults to :data:`EMBED_PROJECT_ID`, the 0.1.0
    value, so a tenant derived before 0.1.1 resolves to the same string
    and its ledger rows stay readable. A host whose HTTP API serves the
    same ledger passes its REAL project id (threaded from
    ``Settings.project_id``) so both surfaces address ONE tenant
    (RELEASE_0.1.1 §5 #19).
    """
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", external_user_id):
        raise ValidationError(
            "external_user_id must match [A-Za-z0-9._:-]{1,128}"
        )
    digest = hashlib.sha256(
        f"{project_id}|{external_user_id}".encode()
    ).hexdigest()
    return "usr_" + digest[:16]


def derive_connection_id(tenant_id: str, address: str, chain_id: str) -> str:
    """Deterministic CHAIN-SCOPED connection id (DECISIONS-pinned).

    ``"conn_" + sha256(f"embed|{tenant_id}|address|{chain_id}|
    {normalized}".encode()).hexdigest()[:16]`` where ``normalized`` is
    the pinned descriptor normalization: ``address.strip()``, lowercased
    iff the stripped value starts with ``"0x"``, so a mixed-case and a
    lowercase EVM address yield the SAME id, while base58 descriptors
    keep their case.

    ``chain_id`` is IDENTITY-BEARING (RELEASE_0.1.1 §5 #26): without it
    one address could be watched on exactly one chain, the second
    connect got a ConflictError naming an id the caller already owned,
    and the two chains would share one sync cursor, since ``SyncEngine``
    keys state by ``(tenant_id, connection.id)``. The project segment
    stays the literal ``"embed"``: ``tenant_id`` already carries the
    configured project's identity.

    ``chain_id`` is SHAPE-CHECKED, which is not defensive padding. Both
    trailing parameters are opaque strings, so swapping them produces a
    perfectly plausible ``conn_`` id and no error at all: a caller that
    passed ``(tenant, chain, address)`` would derive a stable id from the
    wrong preimage and split the namespace silently. The seam audit did
    exactly that. A CAIP-2 shape is the cheapest thing that makes the
    swap loud, since an address can never satisfy it.
    """
    if not re.fullmatch(r"[-a-z0-9]{3,8}:[-_a-zA-Z0-9]{1,32}", chain_id):
        raise ValidationError(
            f"chain_id must be CAIP-2, got {chain_id!r}: arguments are "
            "(tenant_id, address, chain_id)"
        )
    normalized = address.strip()
    if normalized.startswith("0x"):
        normalized = normalized.lower()
    digest = hashlib.sha256(
        f"{EMBED_PROJECT_ID}|{tenant_id}|address|{chain_id}|{normalized}".encode()
    ).hexdigest()
    return "conn_" + digest[:16]


def _validate_report_counts(
    no_op: bool,
    pages_fetched: int,
    live_pages: int,
    backfill_pages: int,
    transactions_ingested: int,
) -> None:
    """Shared report invariants (SPEC §8).

    Three rules, checked in order: every count is ``>= 0``; a no-op did
    NOTHING, so ALL FOUR counts are ``0``, a tick that ingested
    transactions is not "a cheap no-op" whichever count records the
    work; and ``pages_fetched`` is the ONE shared budget spent by the
    call, partitioned exactly into its two phases,
    ``pages_fetched == live_pages + backfill_pages``. A page is spent
    on the live window or on the backfill; there is no third bucket, so
    a report whose halves do not sum to the whole is incoherent and
    raises ``auradefi.errors.ValidationError``.
    """
    counts = {
        "pages_fetched": pages_fetched,
        "live_pages": live_pages,
        "backfill_pages": backfill_pages,
        "transactions_ingested": transactions_ingested,
    }
    for name, value in counts.items():
        if value < 0:
            raise ValidationError(f"{name} must be >= 0, got {value}")
    if no_op:
        for name, value in counts.items():
            if value != 0:
                raise ValidationError(
                    f"a no_op report cannot have {name} != 0, got {value}"
                )
    if pages_fetched != live_pages + backfill_pages:
        raise ValidationError(
            "pages_fetched must equal live_pages + backfill_pages, got "
            f"{pages_fetched} != {live_pages} + {backfill_pages}"
        )


@dataclass(frozen=True, slots=True)
class ConnectionRecord:
    """One watched address bound to a tenant (SPEC §8, §3.1).

    ``id`` is deterministic. See :func:`derive_connection_id`.
    ``chain_id`` is a CAIP-2 string; ``created_at_ms`` is ms-epoch.
    """

    id: str
    chain_id: str
    address: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class SyncState:
    """Per-connection cursor pair for budgeted two-phase sync (SPEC §8).

    ``live_cursor`` advances forward from the head; ``backfill_cursor``
    walks history backwards behind it (``None`` until backfill starts).
    ``last_sync_at_ms`` drives self-throttling: calling ``sync()`` more
    often than the minimum interval is a cheap no-op.

    ``backfill_end`` and ``backfill_page`` are the backfill's RESUME
    POSITION, and they exist because a block is not a page
    (RELEASE_0.1.1 §5 #18). A cursor derived from a page's own
    ``min(block)`` is coarser than the unit being paginated, so neither
    boundary choice works on its own: an exclusive one drops the rest of
    a block the page cut in half, and an inclusive one re-reads page 1 of
    a moving window every tick, and can never advance at all once one
    block holds more rows than ``page_size``. Fixing the window END for
    the whole phase and remembering WHICH PAGE was drained makes the walk
    monotonic: pages slice a stable ordered list, so a block spanning a
    page boundary is covered by the next page, and no tick refetches what
    an earlier one already drained.

    ``backfill_cursor`` stays the lowest block SEEN, for reporting.
    Fresh default: ``SyncState() == SyncState(0, None, False, 0, None, 0)``.
    """

    live_cursor: int = 0
    backfill_cursor: int | None = None
    backfill_complete: bool = False
    last_sync_at_ms: int = 0
    backfill_end: int | None = None
    backfill_page: int = 0


@dataclass(frozen=True, slots=True)
class ConnectionSyncReport:
    """What one connection's slice of a ``sync()`` call did (SPEC §8).

    ``pages_fetched`` is this connection's share of the one shared
    budget, partitioned into the two phases it was spent on:
    ``pages_fetched == live_pages + backfill_pages``, always.

    Raises ``auradefi.errors.ValidationError`` when any count
    (``pages_fetched``, ``live_pages``, ``backfill_pages``,
    ``transactions_ingested``) is negative, when ``no_op`` is True and
    ANY of those four is non-zero, a no-op that fetched pages or
    ingested transactions is a lie, or when the two halves do not sum
    to ``pages_fetched``.

    ``failed`` defaults to False, so every row written before the field
    existed still means "this went fine". It is True when the
    connection's sync raised an ``auradefi.errors.AuradefiError`` that
    the tick contained (RELEASE_0.1.1 §5 #24), and it is MUTUALLY
    EXCLUSIVE with ``no_op``: "nothing needed doing" and "I could not do
    it" are different answers, and a row claiming both raises
    ``ValidationError`` rather than being believed. A failed row still
    obeys every count invariant. Failure is not a licence to emit an
    incoherent partition.
    """

    connection_id: str
    no_op: bool
    pages_fetched: int
    live_pages: int
    backfill_pages: int
    transactions_ingested: int
    live_cursor: int
    backfill_cursor: int | None
    backfill_complete: bool
    failed: bool = False

    def __post_init__(self) -> None:
        if self.failed and self.no_op:
            raise ValidationError(
                "a report cannot be both failed and a no_op: 'I could "
                "not do it' is not 'nothing needed doing'"
            )
        _validate_report_counts(
            self.no_op,
            self.pages_fetched,
            self.live_pages,
            self.backfill_pages,
            self.transactions_ingested,
        )

    @classmethod
    def failure(
        cls, connection_id: str, state: SyncState
    ) -> ConnectionSyncReport:
        """The row for a connection whose sync RAISED; pinned shape.

        Every count is 0, work a tick could not observe is never claimed
        as done, while the cursors are ECHOED from ``state``, the last
        thing the store persisted, so the row says WHERE the connection
        stands as well as that it is stuck. ``no_op`` is False, which the
        failed/no-op exclusion requires anyway: a caller must never read
        a contained failure as a quiet tick (RELEASE_0.1.1 §5 #24).
        """
        return cls(
            connection_id=connection_id,
            no_op=False,
            pages_fetched=0,
            live_pages=0,
            backfill_pages=0,
            transactions_ingested=0,
            live_cursor=state.live_cursor,
            backfill_cursor=state.backfill_cursor,
            backfill_complete=state.backfill_complete,
            failed=True,
        )


def _validate_breakdown_agreement(report: SyncReport) -> None:
    """A supplied breakdown pins every aggregate field (SPEC §8).

    One tick spends one shared budget across its connections, so each
    aggregate count is the sum over ``report.connections`` and the tick
    is a no-op only when every row is. Any disagreement raises
    ``auradefi.errors.ValidationError`` naming the offending field.
    An aggregate and its own rows must not report two different truths.
    Only called when the breakdown is non-empty; ``()`` means "not
    reported", not "zero connections".
    """
    rows = report.connections
    for name in (
        "pages_fetched",
        "live_pages",
        "backfill_pages",
        "transactions_ingested",
    ):
        total = sum(getattr(row, name) for row in rows)
        stated = getattr(report, name)
        if stated != total:
            raise ValidationError(
                f"{name} must equal the sum over connections, got "
                f"{stated} != {total}"
            )
    derived_no_op = all(row.no_op for row in rows)
    if report.no_op != derived_no_op:
        raise ValidationError(
            f"no_op must be {derived_no_op}: it is True exactly when "
            f"every connection is a no-op, got {report.no_op}"
        )


@dataclass(frozen=True, slots=True)
class SyncReport:
    """Aggregate result of one ``sync()`` tick (SPEC §8).

    Same invariants as :class:`ConnectionSyncReport`: negative counts
    raise ``auradefi.errors.ValidationError``, as does ``no_op=True``
    with any non-zero count and any split where
    ``pages_fetched != live_pages + backfill_pages``. The tick's whole
    budget is the sum of the two phases it was spent on.

    ``connections`` carries the per-connection breakdown, defaulting to
    ``()``. When it is non-empty it PINS the aggregate: each of the
    four counts must equal the sum over the rows and ``no_op`` must be
    True exactly when every row is a no-op. An aggregate that
    contradicts its own breakdown raises ``ValidationError`` rather
    than reporting two different truths. Prefer :meth:`assemble`, which
    derives all five from the rows so they cannot disagree.

    :attr:`failed_connections` is likewise derived from the rows, so a
    partial failure is nameable rather than hidden behind an aggregate
    that reads like a clean tick (RELEASE_0.1.1 §5 #24).
    """

    no_op: bool
    pages_fetched: int
    live_pages: int
    backfill_pages: int
    transactions_ingested: int
    connections: tuple[ConnectionSyncReport, ...] = ()

    def __post_init__(self) -> None:
        _validate_report_counts(
            self.no_op,
            self.pages_fetched,
            self.live_pages,
            self.backfill_pages,
            self.transactions_ingested,
        )
        if self.connections:
            _validate_breakdown_agreement(self)

    @property
    def failed_connections(self) -> tuple[str, ...]:
        """Ids of exactly the rows that failed, in breakdown order.

        DERIVED, never stored, so it can never contradict the breakdown:
        ``()`` when no row failed and, vacuously, when there is no
        breakdown at all. A host branches on this instead of scanning
        the rows itself (RELEASE_0.1.1 §5 #24).
        """
        return tuple(row.connection_id for row in self.connections if row.failed)

    @classmethod
    def assemble(
        cls, connections: Iterable[ConnectionSyncReport]
    ) -> SyncReport:
        """Assemble a tick aggregate from its rows; pinned algorithm.

        * each of ``pages_fetched``, ``live_pages``, ``backfill_pages``
          and ``transactions_ingested`` is the exact ``int`` sum over
          ``connections``: the tick spent one shared budget, so the
          rows ARE the total.
        * ``no_op`` is True iff every row is a no-op; a tick that did
          work anywhere did work. Zero connections is a no-op tick.
        * ``connections`` stored as a tuple in input order.

        The result satisfies every :class:`SyncReport` invariant by
        construction, so the aggregate can never be typed out of
        agreement with its breakdown.
        """
        rows = tuple(connections)
        return cls(
            no_op=all(row.no_op for row in rows),
            pages_fetched=sum(row.pages_fetched for row in rows),
            live_pages=sum(row.live_pages for row in rows),
            backfill_pages=sum(row.backfill_pages for row in rows),
            transactions_ingested=sum(
                row.transactions_ingested for row in rows
            ),
            connections=rows,
        )
