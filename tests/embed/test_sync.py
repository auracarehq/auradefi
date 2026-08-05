"""Contract tests for auradefi.embed.sync (SPEC §8, §13).

The budgeted two-phase sync engine, driven entirely through fakes: a
recording ``PageFetcher`` over an in-memory block history, a recording
decoder, ``MemoryLedger`` and ``MemorySyncState``, and a ``FrozenClock``
at T0 = 1_754_000_000_000 with a 60_000 ms throttle window.

Everything asserted here is a NUMBER or an exact call argument list —
the request windows, the page counts, the two cursors, the ingested
count. "It synced" is not an assertion; "it fetched
``[0, 99999999] desc page 1 offset 2`` and moved the live cursor to 106"
is.

The engine is constructed INSIDE test bodies so a stub fails with
NotImplementedError instead of erroring during collection.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from auradefi.clock import FrozenClock
from auradefi.embed.models import ConnectionRecord, ConnectionSyncReport, SyncState
from auradefi.embed.state import MemorySyncState
from auradefi.embed.sync import HEAD_BLOCK, PageFetcher, SyncEngine
from auradefi.errors import SourceError, ValidationError
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.ledger.models import LedgerTransaction, transaction_id
from auradefi.ledger.port import LedgerPort

T0 = 1_754_000_000_000
INTERVAL_MS = 60_000
CHAIN = "eip155:1"
ADDRESS = "0x1111111111111111111111111111111111111111"
SENDER = "0x9999999999999999999999999999999999999999"
TENANT = "usr_1e63721d071ea2d9"
CONNECTION = ConnectionRecord(
    id="conn_b116094c537a85e6",
    chain_id=CHAIN,
    address=ADDRESS,
    created_at_ms=T0,
)


def _row(block: int) -> dict:
    """One Etherscan-shaped txlist row for ``block`` — every field a str."""
    return {
        "blockNumber": str(block),
        "timeStamp": str(1_700_000_000 + (block - 100) * 3600),
        "hash": "0x" + f"{block:064x}",
        "from": SENDER,
        "to": ADDRESS,
        "value": "1000000000000000000",
        "gasUsed": "21000",
        "gasPrice": "10000000000",
        "isError": "0",
    }


def _row_in_block(block: int, slot: int) -> dict:
    """One of SEVERAL rows in ``block``, told apart by its own ``hash``.

    Same block, different transaction: only the hash (and therefore the
    ``transaction_id``) distinguishes ``slot`` 0, 1 and 2, which is
    exactly the case a block-number cursor cannot tell apart.
    """
    return {**_row(block), "hash": "0x" + f"{block:056x}{slot:08x}"}


def _call(
    start_block: int, end_block: int, page: int, sort: str, offset: int = 2
) -> dict:
    """The expected shape of one recorded ``fetch_txlist`` call."""
    return {
        "chain_id": CHAIN,
        "address": ADDRESS,
        "start_block": start_block,
        "end_block": end_block,
        "page": page,
        "offset": offset,
        "sort": sort,
    }


class FakeFetcher:
    """A recording ``PageFetcher`` over an in-memory block history.

    ``blocks`` is the shorthand for one transaction per block; ``rows``
    takes the raw rows verbatim instead, which is how a history with
    SEVERAL transactions in one block is built.
    """

    def __init__(
        self,
        blocks: Sequence[int] = (),
        *,
        fail_on_call: int | None = None,
        rows: Sequence[dict] | None = None,
    ) -> None:
        self._rows = [_row(block) for block in blocks] if rows is None else list(rows)
        self.calls: list[dict] = []
        self._fail_on_call = fail_on_call

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
        self.calls.append(
            {
                "chain_id": chain_id,
                "address": address,
                "start_block": start_block,
                "end_block": end_block,
                "page": page,
                "offset": offset,
                "sort": sort,
            }
        )
        if self._fail_on_call is not None and len(self.calls) == self._fail_on_call:
            raise SourceError("explorer unavailable")
        window = [
            row
            for row in self._rows
            if start_block <= int(row["blockNumber"]) <= end_block
        ]
        window.sort(key=lambda row: int(row["blockNumber"]), reverse=sort == "desc")
        return window[(page - 1) * offset : page * offset]


class RawFetcher:
    """Serves one canned page whatever the window (malformed-row cases)."""

    def __init__(self, rows: Sequence[dict]) -> None:
        self._rows = list(rows)
        self.calls = 0

    def fetch_txlist(self, chain_id: str, address: str, **kwargs) -> list[dict]:
        self.calls += 1
        return list(self._rows)


class RecordingDecoder:
    """A ``Decoder`` recording ``(chain, address, account_id, row count)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int]] = []

    def __call__(
        self,
        chain_id: str,
        address: str,
        account_id: str,
        rows: Sequence[dict],
    ) -> list[LedgerTransaction]:
        self.calls.append((chain_id, address, account_id, len(rows)))
        return [
            LedgerTransaction(
                id=transaction_id(chain_id, row["hash"], account_id),
                chain_id=chain_id,
                tx_hash=row["hash"],
                account_id=account_id,
                block_number=int(row["blockNumber"]),
                initiated_at=int(row["timeStamp"]) * 1000,
                confirmed_at=int(row["timeStamp"]) * 1000,
                entries=(),
            )
            for row in rows
        ]


@dataclass
class Harness:
    """One engine and every collaborator it was built with."""

    engine: SyncEngine
    fetcher: FakeFetcher
    ledger: MemoryLedger
    state: MemorySyncState
    clock: FrozenClock
    decoder: RecordingDecoder = field(default_factory=RecordingDecoder)

    def stored(self) -> SyncState:
        return self.state.get_state(TENANT, CONNECTION.id)


def _harness(
    *,
    blocks: Sequence[int] = (),
    page_size: int = 2,
    min_interval_ms: int = INTERVAL_MS,
    fail_on_call: int | None = None,
    rows: Sequence[dict] | None = None,
) -> Harness:
    """Build the engine and its fakes; call this INSIDE a test body."""
    fetcher = FakeFetcher(blocks, fail_on_call=fail_on_call, rows=rows)
    ledger = MemoryLedger()
    state = MemorySyncState()
    clock = FrozenClock(T0)
    decoder = RecordingDecoder()
    engine = SyncEngine(
        ledger,
        state,
        fetcher,
        decoder,
        clock,
        min_interval_ms,
        page_size,
    )
    return Harness(engine, fetcher, ledger, state, clock, decoder)


def _ledger_blocks(ledger: LedgerPort) -> list[int]:
    """Every live block number in the tenant's ledger, ascending."""
    blocks: list[int] = []
    cursor: str | None = None
    while True:
        page = ledger.sync(TENANT, cursor, 100)
        blocks.extend(
            event.transaction.block_number
            for event in page.events
            if not event.transaction.removed
        )
        cursor = page.next_cursor
        if not page.has_more:
            return sorted(blocks)


def _ledger_ids(ledger: LedgerPort) -> list[str]:
    """Every live transaction id in the tenant's ledger, sorted."""
    ids: list[str] = []
    cursor: str | None = None
    while True:
        page = ledger.sync(TENANT, cursor, 100)
        ids.extend(
            event.transaction.id
            for event in page.events
            if not event.transaction.removed
        )
        cursor = page.next_cursor
        if not page.has_more:
            return sorted(ids)


def _expected_ids(rows: Sequence[dict]) -> list[str]:
    """The ids ``rows`` MUST have become — the DECISIONS-pinned formula.

    Derived from ``CONNECTION.id`` rather than hardcoded so this stays
    true when the connection id gains its chain component.
    """
    return sorted(transaction_id(CHAIN, row["hash"], CONNECTION.id) for row in rows)


# --------------------------------------------------------------------
# budget guard
# --------------------------------------------------------------------


@pytest.mark.parametrize("budget", [0, -1, -100])
def test_a_budget_below_one_raises_before_any_fetch_or_write(budget):
    harness = _harness(blocks=(100, 101))
    with pytest.raises(ValidationError):
        harness.engine.sync_connection(TENANT, CONNECTION, budget)
    assert harness.fetcher.calls == []
    assert harness.stored() == SyncState()


@pytest.mark.parametrize("page_size", [0, -1])
def test_a_page_size_below_one_is_rejected_at_construction(page_size):
    with pytest.raises(ValidationError):
        SyncEngine(
            MemoryLedger(),
            MemorySyncState(),
            FakeFetcher(),
            RecordingDecoder(),
            FrozenClock(T0),
            INTERVAL_MS,
            page_size,
        )


# --------------------------------------------------------------------
# self-throttling (SPEC §13: the second call in quick succession)
# --------------------------------------------------------------------


def test_the_first_sync_spends_a_page_and_stamps_last_sync_at():
    harness = _harness(blocks=(100, 101, 102))
    report = harness.engine.sync_connection(TENANT, CONNECTION, 3)
    assert report.no_op is False
    assert report.pages_fetched >= 1
    assert harness.stored().last_sync_at_ms == T0


def test_an_immediate_second_sync_is_a_no_op_that_touches_nothing():
    harness = _harness(blocks=(100, 101, 102))
    harness.engine.sync_connection(TENANT, CONNECTION, 3)
    before = harness.stored()
    calls = len(harness.fetcher.calls)

    report = harness.engine.sync_connection(TENANT, CONNECTION, 3)

    assert report.no_op is True
    assert (
        report.pages_fetched,
        report.live_pages,
        report.backfill_pages,
        report.transactions_ingested,
    ) == (0, 0, 0, 0)
    assert (report.live_cursor, report.backfill_cursor, report.backfill_complete) == (
        before.live_cursor,
        before.backfill_cursor,
        before.backfill_complete,
    )
    assert len(harness.fetcher.calls) == calls
    assert harness.stored() == before


def test_the_throttle_still_holds_one_millisecond_early():
    harness = _harness(blocks=(100, 101, 102))
    harness.engine.sync_connection(TENANT, CONNECTION, 3)
    calls = len(harness.fetcher.calls)

    harness.clock.advance(INTERVAL_MS - 1)
    report = harness.engine.sync_connection(TENANT, CONNECTION, 3)

    assert report.no_op is True
    assert len(harness.fetcher.calls) == calls


def test_the_throttle_releases_exactly_at_the_interval():
    harness = _harness(blocks=(100, 101, 102))
    harness.engine.sync_connection(TENANT, CONNECTION, 3)
    calls = len(harness.fetcher.calls)

    harness.clock.advance(INTERVAL_MS)
    report = harness.engine.sync_connection(TENANT, CONNECTION, 3)

    assert report.no_op is False
    assert len(harness.fetcher.calls) == calls + 1
    assert harness.stored().last_sync_at_ms == T0 + INTERVAL_MS


def test_a_zero_minimum_interval_never_throttles():
    harness = _harness(blocks=(100, 101, 102), min_interval_ms=0)
    harness.engine.sync_connection(TENANT, CONNECTION, 3)
    report = harness.engine.sync_connection(TENANT, CONNECTION, 3)
    assert report.no_op is False


# --------------------------------------------------------------------
# the anchor page
# --------------------------------------------------------------------


# pins: a never-synced connection spends its first page on ONE desc window
#       over [0, head_block] counted as a LIVE page, and the backfill that
#       follows re-enters the anchor's lowest block INCLUSIVELY — the second
#       window is [0, 105], not [0, 104].
def test_the_anchor_page_is_the_first_live_window():
    harness = _harness(blocks=range(100, 107))

    report = harness.engine.sync_connection(TENANT, CONNECTION, 2)

    assert harness.fetcher.calls == [
        _call(0, HEAD_BLOCK, 1, "desc"),
        _call(0, 105, 1, "desc"),
    ]
    assert (report.live_pages, report.backfill_pages, report.pages_fetched) == (1, 1, 2)
    assert report.transactions_ingested == 3
    assert (report.live_cursor, report.backfill_cursor, report.backfill_complete) == (
        106,
        104,
        False,
    )
    # backfill_end is FIXED at 105 by the anchor and backfill_page records
    # that page 1 of that window is drained, so the next tick resumes at
    # page 2 instead of re-reading page 1 (RELEASE_0.1.1 §5 #18).
    assert harness.stored() == SyncState(106, 104, False, T0, 105, 1)
    assert _ledger_blocks(harness.ledger) == [104, 105, 106]


def test_an_empty_anchor_page_zeroes_both_cursors_and_completes():
    harness = _harness(blocks=())

    report = harness.engine.sync_connection(TENANT, CONNECTION, 3)

    assert harness.fetcher.calls == [_call(0, HEAD_BLOCK, 1, "desc")]
    assert (report.pages_fetched, report.live_pages, report.backfill_pages) == (1, 1, 0)
    assert report.transactions_ingested == 0
    assert harness.stored() == SyncState(0, 0, True, T0)


def test_a_short_anchor_page_completes_the_backfill_immediately():
    harness = _harness(blocks=(100,))

    report = harness.engine.sync_connection(TENANT, CONNECTION, 5)

    assert harness.fetcher.calls == [_call(0, HEAD_BLOCK, 1, "desc")]
    assert report.transactions_ingested == 1
    assert harness.stored() == SyncState(100, 100, True, T0)


# --------------------------------------------------------------------
# resume: live window then backfill
# --------------------------------------------------------------------


# pins: after the live window drains, ONE call keeps paging the SAME FIXED
#       backfill window [0, backfill_end] and RESUMES at the page the last
#       tick stopped on, until a short page completes it. Every block of the
#       history lands, exactly once. This previously expected the windows
#       (0,104) p1..p3 — the exclusive `cursor - 1` arithmetic that dropped
#       the remainder of a block a page had cut in half (RELEASE_0.1.1 §5
#       #18). The window end is now pinned by the anchor at 105 and the page
#       carries the resume position, so tick 1 drains page 1 and tick 2
#       continues at page 2 rather than re-reading page 1.
def test_resuming_drains_the_live_window_then_backfills_to_completion():
    harness = _harness(blocks=range(100, 107))
    harness.engine.sync_connection(TENANT, CONNECTION, 2)
    harness.clock.advance(INTERVAL_MS)

    report = harness.engine.sync_connection(TENANT, CONNECTION, 4)

    assert harness.fetcher.calls[2:] == [
        _call(107, HEAD_BLOCK, 1, "asc"),
        _call(0, 105, 2, "desc"),
        _call(0, 105, 3, "desc"),
        _call(0, 105, 4, "desc"),
    ]
    assert (report.live_pages, report.backfill_pages, report.pages_fetched) == (1, 3, 4)
    assert report.transactions_ingested == 4
    assert harness.stored() == SyncState(106, 100, True, T0 + INTERVAL_MS, 105, 4)
    assert _ledger_blocks(harness.ledger) == [100, 101, 102, 103, 104, 105, 106]


# pins: once backfill_complete is stored True, a later sync walks ZERO backfill
#       pages — it fetches the live window and nothing else.
def test_a_completed_backfill_is_never_walked_again():
    harness = _harness(blocks=range(100, 107))
    harness.engine.sync_connection(TENANT, CONNECTION, 2)
    harness.clock.advance(INTERVAL_MS)
    harness.engine.sync_connection(TENANT, CONNECTION, 4)
    # the fixture must actually reach the branch the pin names
    assert harness.stored().backfill_complete is True
    walked = len(harness.fetcher.calls)
    harness.clock.advance(INTERVAL_MS)

    report = harness.engine.sync_connection(TENANT, CONNECTION, 5)

    assert harness.fetcher.calls[walked:] == [_call(107, HEAD_BLOCK, 1, "asc")]
    assert (report.live_pages, report.backfill_pages) == (1, 0)
    assert report.transactions_ingested == 0
    assert harness.stored() == SyncState(
        106, 100, True, T0 + 2 * INTERVAL_MS, 105, 4
    )


# --------------------------------------------------------------------
# the stranded-live rule: a cut window never advances its cursor
# --------------------------------------------------------------------


def _stranded_harness() -> Harness:
    """live=100, backfill done, five unseen transactions at 101..105."""
    harness = _harness(blocks=range(101, 106))
    harness.state.put_state(
        TENANT,
        CONNECTION.id,
        SyncState(
            live_cursor=100,
            backfill_cursor=100,
            backfill_complete=True,
            last_sync_at_ms=0,
        ),
    )
    return harness


def test_a_budget_cut_live_window_does_not_advance_the_cursor():
    harness = _stranded_harness()

    report = harness.engine.sync_connection(TENANT, CONNECTION, 2)

    assert harness.fetcher.calls == [
        _call(101, HEAD_BLOCK, 1, "asc"),
        _call(101, HEAD_BLOCK, 2, "asc"),
    ]
    assert (report.live_pages, report.backfill_pages) == (2, 0)
    assert report.transactions_ingested == 4
    assert report.live_cursor == 100
    assert harness.stored() == SyncState(100, 100, True, T0)
    assert _ledger_blocks(harness.ledger) == [101, 102, 103, 104]


def test_the_next_call_refetches_the_stranded_pages_without_duplicating():
    harness = _stranded_harness()
    harness.engine.sync_connection(TENANT, CONNECTION, 2)
    harness.clock.advance(INTERVAL_MS)

    report = harness.engine.sync_connection(TENANT, CONNECTION, 3)

    assert harness.fetcher.calls[2:] == [
        _call(101, HEAD_BLOCK, 1, "asc"),
        _call(101, HEAD_BLOCK, 2, "asc"),
        _call(101, HEAD_BLOCK, 3, "asc"),
    ]
    assert report.live_pages == 3
    assert report.transactions_ingested == 1
    assert report.live_cursor == 105
    assert harness.stored() == SyncState(105, 100, True, T0 + INTERVAL_MS)
    assert _ledger_blocks(harness.ledger) == [101, 102, 103, 104, 105]


# --------------------------------------------------------------------
# page-granular resumability
# --------------------------------------------------------------------


def test_a_mid_run_source_failure_leaves_the_previous_page_committed():
    harness = _harness(blocks=range(100, 107), fail_on_call=2)

    with pytest.raises(SourceError):
        harness.engine.sync_connection(TENANT, CONNECTION, 3)

    assert harness.stored() == SyncState(106, 105, False, T0, 105, 0)
    assert _ledger_blocks(harness.ledger) == [105, 106]


def test_a_failure_on_the_very_first_page_writes_nothing():
    harness = _harness(blocks=range(100, 107), fail_on_call=1)

    with pytest.raises(SourceError):
        harness.engine.sync_connection(TENANT, CONNECTION, 3)

    assert harness.stored() == SyncState()
    assert _ledger_blocks(harness.ledger) == []


# --------------------------------------------------------------------
# row hygiene and the seams
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "block_value",
    [106, None, "0x6a", "", " 106 ", "1_06"],
    ids=["json-int", "null", "hex", "empty", "padded", "underscored"],
)
def test_a_block_number_that_is_not_a_digit_string_is_rejected(block_value):
    row = {**_row(106), "blockNumber": block_value}
    fetcher = RawFetcher([row])
    ledger = MemoryLedger()
    state = MemorySyncState()
    engine = SyncEngine(
        ledger, state, fetcher, RecordingDecoder(), FrozenClock(T0), INTERVAL_MS, 2
    )

    with pytest.raises(SourceError):
        engine.sync_connection(TENANT, CONNECTION, 1)

    assert state.get_state(TENANT, CONNECTION.id) == SyncState()
    assert _ledger_blocks(ledger) == []


def test_a_missing_block_number_key_is_rejected():
    row = {key: value for key, value in _row(106).items() if key != "blockNumber"}
    engine = SyncEngine(
        MemoryLedger(),
        MemorySyncState(),
        RawFetcher([row]),
        RecordingDecoder(),
        FrozenClock(T0),
        INTERVAL_MS,
        2,
    )
    with pytest.raises(SourceError):
        engine.sync_connection(TENANT, CONNECTION, 1)


def test_the_decoder_is_handed_the_chain_address_and_connection_id():
    harness = _harness(blocks=(100, 101))

    harness.engine.sync_connection(TENANT, CONNECTION, 1)

    assert harness.decoder.calls == [(CHAIN, ADDRESS, CONNECTION.id, 2)]


def test_an_empty_page_never_reaches_the_decoder():
    harness = _harness(blocks=(100,))
    harness.engine.sync_connection(TENANT, CONNECTION, 1)
    harness.clock.advance(INTERVAL_MS)
    harness.decoder.calls.clear()

    report = harness.engine.sync_connection(TENANT, CONNECTION, 1)

    assert report.transactions_ingested == 0
    assert harness.decoder.calls == []


def test_page_fetcher_is_a_runtime_checkable_protocol():
    assert isinstance(FakeFetcher(), PageFetcher)
    assert isinstance(RawFetcher([]), PageFetcher)
    assert not isinstance(object(), PageFetcher)


def test_the_head_block_sentinel_is_pinned():
    assert HEAD_BLOCK == 99_999_999


# --------------------------------------------------------------------
# a page that ends INSIDE a block (#18)
#
# A block is not a page: with page_size 2 and three transactions in
# block 100, page 1 of every desc window over that history ends inside
# block 100 and one transaction is left behind. A backfill window that
# restarts strictly BELOW the lowest block it ingested never asks for
# that remainder again — and the loss is silent, because the same run
# reports backfill_complete. Nothing here asserts a request sequence:
# the pins are the ledger's CONTENTS and the report's HONESTY, so any
# window arithmetic that fetches the whole block satisfies them.
# --------------------------------------------------------------------

#: Three transactions in ONE block: slot 2 is the one a page-sized cut
#: at two rows strands.
BOUNDARY_ROWS = [_row_in_block(100, slot) for slot in range(3)]

#: The same split block with older history behind it, so the block the
#: page cuts in half is not merely the end of the chain.
DEEP_ROWS = [_row_in_block(98, 0), _row_in_block(99, 0), *BOUNDARY_ROWS]


def _sync_until_complete(
    harness: Harness, *, budget: int = 4, max_calls: int = 6
) -> list[ConnectionSyncReport]:
    """Sync (advancing past the throttle) until the backfill reports done.

    Returns every report made, so a caller can total the counts. Stops
    at ``max_calls`` whatever the flag says, so a backfill that never
    finishes fails an assertion instead of looping forever.
    """
    reports: list[ConnectionSyncReport] = []
    for _ in range(max_calls):
        reports.append(harness.engine.sync_connection(TENANT, CONNECTION, budget))
        if reports[-1].backfill_complete:
            break
        harness.clock.advance(INTERVAL_MS)
    return reports


# pins: when a page ends inside a block, the transactions in the rest of
#       that block are still fetched — every transaction of a
#       page-splitting block reaches the ledger.
def test_a_page_ending_inside_a_block_still_ingests_that_blocks_remainder():
    harness = _harness(rows=BOUNDARY_ROWS)

    _sync_until_complete(harness)

    assert _ledger_ids(harness.ledger) == _expected_ids(BOUNDARY_ROWS)
    assert _ledger_blocks(harness.ledger) == [100, 100, 100]


# pins: backfill_complete never reads True while a transaction is still
#       missing from the ledger — a backfill that dropped a transaction
#       does not report that it finished.
def test_backfill_complete_never_reads_true_with_a_transaction_missing():
    harness = _harness(rows=BOUNDARY_ROWS)
    expected = _expected_ids(BOUNDARY_ROWS)
    completed = False

    for _ in range(6):
        report = harness.engine.sync_connection(TENANT, CONNECTION, 4)
        landed = _ledger_ids(harness.ledger)
        if report.backfill_complete:
            completed = True
            assert landed == expected, (
                "backfill_complete is True while transactions are missing "
                f"from the ledger: {sorted(set(expected) - set(landed))}"
            )
            break
        harness.clock.advance(INTERVAL_MS)

    assert completed is True, "the backfill never reported completion"
    assert harness.stored().backfill_complete is True


# pins: the block a page cut in half is re-entered even when older
#       blocks exist behind it — walking on to older history never
#       skips the remainder of the split block.
def test_the_split_block_is_not_skipped_when_older_history_follows_it():
    harness = _harness(rows=DEEP_ROWS)

    reports = _sync_until_complete(harness)

    assert reports[-1].backfill_complete is True
    assert _ledger_ids(harness.ledger) == _expected_ids(DEEP_ROWS)
    assert _ledger_blocks(harness.ledger) == [98, 99, 100, 100, 100]


# pins: transactions_ingested totals the DISTINCT transactions of the
#       history, not the rows fetched — a window that refetches rows it
#       already ingested reports them as ingested once, not twice.
def test_the_ingested_total_counts_each_transaction_exactly_once():
    harness = _harness(rows=DEEP_ROWS)

    reports = _sync_until_complete(harness)

    assert sum(report.transactions_ingested for report in reports) == 5
    assert len(_ledger_ids(harness.ledger)) == 5


# pins: a backfill resumed with its cursor ON a split block ingests only
#       the transaction of that block it has not stored yet — dedup is
#       by transaction id, so refetched rows are not added again.
def test_resuming_on_a_split_block_adds_only_the_unseen_transaction():
    harness = _harness(rows=BOUNDARY_ROWS)
    anchor = harness.engine.sync_connection(TENANT, CONNECTION, 1)
    assert anchor.transactions_ingested == 2  # the page cut block 100 in half
    assert harness.stored() == SyncState(100, 100, False, T0, 100, 0)
    harness.clock.advance(INTERVAL_MS)

    reports = _sync_until_complete(harness)

    assert sum(report.transactions_ingested for report in reports) == 1
    assert _ledger_ids(harness.ledger) == _expected_ids(BOUNDARY_ROWS)


# pins: a sync made after the backfill genuinely completed adds no
#       transaction and walks no backfill page — the split block is
#       never re-ingested as new rows.
def test_a_sync_after_completion_adds_nothing_to_a_split_block():
    harness = _harness(rows=BOUNDARY_ROWS)
    _sync_until_complete(harness)
    harness.clock.advance(INTERVAL_MS)

    report = harness.engine.sync_connection(TENANT, CONNECTION, 4)

    assert (report.transactions_ingested, report.backfill_pages) == (0, 0)
    assert _ledger_blocks(harness.ledger) == [100, 100, 100]
