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
from auradefi.embed.models import ConnectionRecord, SyncState
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
    """A recording ``PageFetcher`` over an in-memory block history."""

    def __init__(
        self, blocks: Sequence[int] = (), *, fail_on_call: int | None = None
    ) -> None:
        self._rows = [_row(block) for block in blocks]
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
) -> Harness:
    """Build the engine and its fakes; call this INSIDE a test body."""
    fetcher = FakeFetcher(blocks, fail_on_call=fail_on_call)
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


def test_the_anchor_page_is_the_first_live_window():
    harness = _harness(blocks=range(100, 107))

    report = harness.engine.sync_connection(TENANT, CONNECTION, 2)

    assert harness.fetcher.calls == [
        _call(0, HEAD_BLOCK, 1, "desc"),
        _call(0, 104, 1, "desc"),
    ]
    assert (report.live_pages, report.backfill_pages, report.pages_fetched) == (1, 1, 2)
    assert report.transactions_ingested == 4
    assert (report.live_cursor, report.backfill_cursor, report.backfill_complete) == (
        106,
        103,
        False,
    )
    assert harness.stored() == SyncState(106, 103, False, T0)
    assert _ledger_blocks(harness.ledger) == [103, 104, 105, 106]


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


def test_resuming_drains_the_live_window_then_backfills_to_completion():
    harness = _harness(blocks=range(100, 107))
    harness.engine.sync_connection(TENANT, CONNECTION, 2)
    harness.clock.advance(INTERVAL_MS)

    report = harness.engine.sync_connection(TENANT, CONNECTION, 3)

    assert harness.fetcher.calls[2:] == [
        _call(107, HEAD_BLOCK, 1, "asc"),
        _call(0, 102, 1, "desc"),
        _call(0, 100, 1, "desc"),
    ]
    assert (report.live_pages, report.backfill_pages, report.pages_fetched) == (1, 2, 3)
    assert report.transactions_ingested == 3
    assert harness.stored() == SyncState(106, 100, True, T0 + INTERVAL_MS)
    assert _ledger_blocks(harness.ledger) == [100, 101, 102, 103, 104, 105, 106]


def test_a_completed_backfill_is_never_walked_again():
    harness = _harness(blocks=range(100, 107))
    harness.engine.sync_connection(TENANT, CONNECTION, 2)
    harness.clock.advance(INTERVAL_MS)
    harness.engine.sync_connection(TENANT, CONNECTION, 3)
    harness.clock.advance(INTERVAL_MS)

    report = harness.engine.sync_connection(TENANT, CONNECTION, 5)

    assert harness.fetcher.calls[5:] == [_call(107, HEAD_BLOCK, 1, "asc")]
    assert (report.live_pages, report.backfill_pages) == (1, 0)
    assert report.transactions_ingested == 0
    assert harness.stored() == SyncState(106, 100, True, T0 + 2 * INTERVAL_MS)


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

    assert harness.stored() == SyncState(106, 105, False, T0)
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
