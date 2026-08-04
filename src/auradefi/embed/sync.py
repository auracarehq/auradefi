"""Budgeted, resumable, self-throttling two-phase sync (SPEC §8).

The host owns scheduling; we own throttling. Many hosts run one fixed
interval tick across every integration with no per-integration cadence,
so :meth:`SyncEngine.sync_connection` called more often than
``min_interval_ms`` is a cheap no-op — zero requests, zero writes (SPEC
§13's "sync() twice in quick succession is a no-op the second time").

One shared budget of page requests per call, spent in two phases:

* the LIVE window walks forward from ``live_cursor`` to the head;
* the BACKFILL walks history backwards from ``backfill_cursor``, behind
  the live window, so a decade-old wallet never leaves the dashboard
  empty while old blocks drain.

Both cursors persist after EVERY page, so a crash resumes at page
granularity. The live cursor advances ONLY when its window drains: if
the budget cuts the window mid-way the pages beyond the cut would be
stranded behind an advanced cursor, so it stays put and the next call
refetches them — free, because a payload-identical redelivery emits no
event (``ledger.models.payload_equal``).

Transport is a port: :class:`PageFetcher` is a ``runtime_checkable``
``Protocol`` over RAW explorer rows, and :data:`Decoder` turns those rows
into ``LedgerTransaction`` values. This module therefore performs no
HTTP, imports no client, and knows nothing about Etherscan's envelope.

All timestamps are ms-epoch ints; block numbers are read from the row's
``blockNumber`` STRING, never from a JSON number (rule #2).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from auradefi.clock import Clock
from auradefi.embed.models import (
    ConnectionRecord,
    ConnectionSyncReport,
    SyncState,
)
from auradefi.embed.state import SyncStatePort
from auradefi.errors import SourceError, ValidationError
from auradefi.ledger.models import LedgerTransaction, SyncEventKind
from auradefi.ledger.port import LedgerPort

#: Etherscan's "no upper bound" sentinel block, pinned so the live window
#: and the anchor page request an identical, cassette-stable URL.
HEAD_BLOCK = 99_999_999


@runtime_checkable
class PageFetcher(Protocol):
    """Structural seam: ONE page of raw explorer rows for one window.

    A host satisfies it by shape (rule #12) — no base class, no
    registration. Rows are the explorer's RAW dicts (Etherscan txlist
    rows); parsing is the decoder's job, not this seam's.
    """

    def fetch_txlist(
        self,
        chain_id: str,
        address: str,
        *,
        start_block: int,
        end_block: int,
        page: int,
        offset: int,
        sort: str,
    ) -> list[dict]:
        """Raw rows in ``[start_block, end_block]``, ``sort`` asc|desc.

        ``page`` is 1-based and ``offset`` is the page size. An empty
        history is ``[]`` — Etherscan's status-0 "No transactions found"
        is NOT an error (SPEC §3.3); a real failure raises
        ``auradefi.errors.SourceError``.
        """
        raise NotImplementedError


#: ``(chain_id, address, account_id, rows) -> LedgerTransactions``.
#: The seam between raw explorer rows and the ledger: parsing, decoding
#: and projection all live behind it, so this module stays transport- and
#: format-free.
Decoder = Callable[[str, str, str, Sequence[dict]], Sequence[LedgerTransaction]]


@dataclass(slots=True)
class _Run:
    """Mutable per-call scratch: the budget being spent and the cursors.

    Never escapes :meth:`SyncEngine.sync_connection`; the immutable
    ``SyncState``/``ConnectionSyncReport`` values are derived from it.
    """

    tenant_id: str
    connection: ConnectionRecord
    now_ms: int
    remaining: int
    live_pages: int = 0
    backfill_pages: int = 0
    ingested: int = 0
    live_cursor: int = 0
    backfill_cursor: int | None = None
    backfill_complete: bool = False


def _block_numbers(rows: Sequence[dict]) -> list[int]:
    """Block numbers of one raw page, read from the STRING (rule #2).

    Cursor arithmetic must never trust a JSON number: a row whose
    ``blockNumber`` is missing, non-``str``, or not an unsigned base-10
    digit string raises ``auradefi.errors.SourceError`` BEFORE the page
    is ingested, so a malformed page never moves a cursor.
    """
    blocks: list[int] = []
    for row in rows:
        value = row.get("blockNumber")
        if type(value) is not str or not (value.isascii() and value.isdigit()):
            raise SourceError(
                f"row has no unsigned base-10 'blockNumber' string: {value!r}"
            )
        blocks.append(int(value))
    return blocks


class SyncEngine:
    """Two-phase budgeted sync for one connection at a time (SPEC §8).

    Collaborators are injected and the constructor performs no I/O:
    ``ledger`` and ``state`` are ports, ``fetcher`` is the transport
    seam, ``decoder`` the format seam, ``clock`` the time port.
    ``min_interval_ms`` is the self-throttle window, ``page_size`` the
    ``offset`` of every request (and therefore the short-page signal
    that ends a window), ``head_block`` the upper bound of the live
    window and the anchor page.
    """

    def __init__(
        self,
        ledger: LedgerPort,
        state: SyncStatePort,
        fetcher: PageFetcher,
        decoder: Decoder,
        clock: Clock,
        min_interval_ms: int,
        page_size: int = 1000,
        head_block: int = HEAD_BLOCK,
    ) -> None:
        """Bind the ports; no request, no read, no write happens here.

        Raises ``auradefi.errors.ValidationError`` when ``page_size < 1``:
        every phase ends on "a page shorter than ``page_size``", which no
        page can ever be when the size is zero or negative.
        """
        if page_size < 1:
            raise ValidationError(f"page_size must be >= 1, got {page_size}")
        self._ledger = ledger
        self._state = state
        self._fetcher = fetcher
        self._decoder = decoder
        self._clock = clock
        self._min_interval_ms = min_interval_ms
        self._page_size = page_size
        self._head_block = head_block

    def sync_connection(
        self, tenant_id: str, connection: ConnectionRecord, budget: int
    ) -> ConnectionSyncReport:
        """Spend at most ``budget`` page requests on one connection.

        ``budget`` is a count of page REQUESTS, not of transactions.
        ``budget < 1`` raises ``auradefi.errors.ValidationError`` before
        anything — no clock read, no fetch, no state write.

        THROTTLE — with ``now = clock.now_ms()``, a stored
        ``last_sync_at_ms`` closer than ``min_interval_ms`` returns a
        no-op report (every count 0, the stored cursors echoed) having
        made ZERO fetcher calls and written NO state. Otherwise the run
        persists ``last_sync_at_ms = now`` with every state write.

        ANCHOR (``backfill_cursor is None``, i.e. never synced) — ONE
        desc page over ``[0, head_block]``: the newest page IS the first
        live window, so it counts as a live page. ``live_cursor`` becomes
        its max block, ``backfill_cursor`` its min (both ``0`` for an
        empty page), and ``backfill_complete`` is True iff the page was
        short. The live phase is skipped this call — the anchor already
        covered the head.

        LIVE — ``[live_cursor + 1, head_block]`` ascending, pages
        1, 2, … while budget remains, each ingested IMMEDIATELY. A short
        page means the window DRAINED: ``live_cursor`` becomes the
        highest block seen this phase (unchanged if the phase saw none).
        If the budget runs out mid-window the cursor does NOT advance and
        the backfill is SKIPPED this call — otherwise the pages the
        budget cut would sit behind an advanced cursor forever. Their
        refetch next call is event-free.

        BACKFILL — while budget remains and the backfill is incomplete:
        one desc page over ``[0, backfill_cursor - 1]``;
        ``backfill_cursor`` becomes its min block, and the backfill
        completes on a short page or once the window would start below
        block 0.

        Every page ingests through ``decoder`` then ``ledger.upsert`` and
        writes state, so a crash resumes at page granularity;
        ``transactions_ingested`` counts ADDED events, so an idempotent
        redelivery contributes 0.
        """
        if budget < 1:
            raise ValidationError(f"budget must be >= 1, got {budget}")
        stored = self._state.get_state(tenant_id, connection.id)
        now_ms = self._clock.now_ms()
        if now_ms - stored.last_sync_at_ms < self._min_interval_ms:
            return ConnectionSyncReport(
                connection_id=connection.id,
                no_op=True,
                pages_fetched=0,
                live_pages=0,
                backfill_pages=0,
                transactions_ingested=0,
                live_cursor=stored.live_cursor,
                backfill_cursor=stored.backfill_cursor,
                backfill_complete=stored.backfill_complete,
            )
        run = _Run(
            tenant_id=tenant_id,
            connection=connection,
            now_ms=now_ms,
            remaining=budget,
            live_cursor=stored.live_cursor,
            backfill_cursor=stored.backfill_cursor,
            backfill_complete=stored.backfill_complete,
        )
        if run.backfill_cursor is None:
            self._anchor(run)
        elif not self._live(run):
            return self._report(run)  # cut mid-window: the backfill waits
        self._backfill(run)
        return self._report(run)

    def _anchor(self, run: _Run) -> None:
        """The never-synced case: one desc head page, counted as live."""
        rows, blocks = self._page(
            run, start_block=0, end_block=self._head_block, page=1, sort="desc"
        )
        run.live_pages += 1
        run.ingested += self._ingest(run, rows)
        run.live_cursor = max(blocks) if blocks else 0
        run.backfill_cursor = min(blocks) if blocks else 0
        run.backfill_complete = len(rows) < self._page_size
        self._persist(run)

    def _live(self, run: _Run) -> bool:
        """Walk the live window; True iff it drained inside the budget."""
        page = 1
        highest: int | None = None
        while run.remaining > 0:
            rows, blocks = self._page(
                run,
                start_block=run.live_cursor + 1,
                end_block=self._head_block,
                page=page,
                sort="asc",
            )
            run.live_pages += 1
            run.ingested += self._ingest(run, rows)
            if blocks:
                highest = max(blocks) if highest is None else max(highest, max(blocks))
            drained = len(rows) < self._page_size
            if drained and highest is not None:
                run.live_cursor = highest
            self._persist(run)
            if drained:
                return True
            page += 1
        return False

    def _backfill(self, run: _Run) -> None:
        """Walk history backwards until complete or out of budget.

        ``backfill_cursor`` is the LOWEST block ingested so far, so an
        empty page leaves it where it was — there is no older block to
        move it to, and the page's shortness completes the phase anyway.
        """
        while run.remaining > 0 and not run.backfill_complete:
            cursor = 0 if run.backfill_cursor is None else run.backfill_cursor
            if cursor - 1 < 0:
                # Nothing older than block 0 can exist.
                run.backfill_complete = True
                self._persist(run)
                return
            rows, blocks = self._page(
                run, start_block=0, end_block=cursor - 1, page=1, sort="desc"
            )
            run.backfill_pages += 1
            run.ingested += self._ingest(run, rows)
            reached = min(blocks) if blocks else cursor
            run.backfill_cursor = reached
            run.backfill_complete = len(rows) < self._page_size or reached - 1 < 0
            self._persist(run)

    def _page(
        self, run: _Run, *, start_block: int, end_block: int, page: int, sort: str
    ) -> tuple[list[dict], list[int]]:
        """One page request; spends one unit of budget.

        Returns ``(rows, block_numbers)`` — the block numbers are parsed
        eagerly so a malformed page raises before anything is written.
        """
        rows = list(
            self._fetcher.fetch_txlist(
                run.connection.chain_id,
                run.connection.address,
                start_block=start_block,
                end_block=end_block,
                page=page,
                offset=self._page_size,
                sort=sort,
            )
        )
        run.remaining -= 1
        return rows, _block_numbers(rows)

    def _ingest(self, run: _Run, rows: Sequence[dict]) -> int:
        """Decode + upsert one page; returns the count of ADDED events.

        An empty page never reaches the decoder or the ledger: it
        ingests nothing by definition.
        """
        if not rows:
            return 0
        txns = self._decoder(
            run.connection.chain_id,
            run.connection.address,
            run.connection.id,
            rows,
        )
        events = self._ledger.upsert(run.tenant_id, list(txns))
        return sum(1 for event in events if event.kind is SyncEventKind.ADDED)

    def _persist(self, run: _Run) -> None:
        """Write the run's cursors and ``last_sync_at_ms`` for this page."""
        self._state.put_state(
            run.tenant_id,
            run.connection.id,
            SyncState(
                live_cursor=run.live_cursor,
                backfill_cursor=run.backfill_cursor,
                backfill_complete=run.backfill_complete,
                last_sync_at_ms=run.now_ms,
            ),
        )

    def _report(self, run: _Run) -> ConnectionSyncReport:
        """The run's :class:`ConnectionSyncReport` — never a no-op.

        A non-throttled call always spends at least one page, so
        ``no_op`` is False and ``pages_fetched`` is the two phases' sum.
        """
        return ConnectionSyncReport(
            connection_id=run.connection.id,
            no_op=False,
            pages_fetched=run.live_pages + run.backfill_pages,
            live_pages=run.live_pages,
            backfill_pages=run.backfill_pages,
            transactions_ingested=run.ingested,
            live_cursor=run.live_cursor,
            backfill_cursor=run.backfill_cursor,
            backfill_complete=run.backfill_complete,
        )
