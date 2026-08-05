"""SEAM AUDIT — wave 0.1.1-wave2: the backfill window meets the ledger.

Order ``embed-backfill`` owns ``src/auradefi/embed/sync.py`` and nothing
else. Its declared seams say two things this file tests from OUTSIDE the
module:

1. "SyncEngine keys sync state by (tenant_id, connection.id) … your
   cursor keying must keep working" — the cursor is the ONLY thing that
   survives a call, and the new backfill walks pages 1, 2, … of ONE
   window inside a single call. ``SyncState``
   (``src/auradefi/embed/models.py``, order ``embed-ids-loop``) has no
   field for an intra-window page position, so the page counter resets
   to 1 on every call. A tick whose budget only affords ONE backfill
   page therefore re-requests page 1 of the same window forever.
2. "The ledger write path is ``src/auradefi/ledger/`` which you do not
   own. Dedup by transaction id MUST be done on your side of that
   boundary or reported as a finding." ``sync.py``'s new docstring
   delegates it across the boundary instead — "deduplicated by
   TRANSACTION ID at the ledger, where an id-and-payload-identical
   redelivery emits no ADDED event". ``LedgerPort.upsert``
   (``src/auradefi/ledger/port.py``) declares the OPPOSITE for one case:
   an identical redelivery of a REMOVED row is RESURRECTED with an ADDED
   event. So the every-tick re-delivery of the boundary block silently
   undoes a reorg removal.

Plus the boundary between the window formula and the RECORDED FIXTURE
that the offline suite is built on (house rule: "recorded fixtures
only").

Nothing here reads the inside of ``sync.py``; every fake is written from
the ``PageFetcher`` docstring alone and every assertion is about a value
crossing a module boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

from auradefi.clock import FrozenClock
from auradefi.embed.models import ConnectionRecord
from auradefi.embed.state import MemorySyncState
from auradefi.embed.sync import SyncEngine
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.ledger.models import Direction, Entry, LedgerTransaction
from auradefi.money.quantity import Quantity

CASSETTES = Path(__file__).resolve().parents[3] / "tests" / "cassettes"
CHAIN = "eip155:1"
ADDRESS = "0x1111111111111111111111111111111111111111"
TENANT = "usr_seam_backfill"
NATIVE = "eip155:1/slip44:60"

#: Four transactions, THREE of them in block 100 — a page of 2 cannot
#: end on a block boundary. Newest first, the order a desc page serves.
ROWS = (
    {"blockNumber": "100", "hash": "0xcc", "timeStamp": "1700000003"},
    {"blockNumber": "100", "hash": "0xbb", "timeStamp": "1700000002"},
    {"blockNumber": "100", "hash": "0xaa", "timeStamp": "1700000001"},
    {"blockNumber": "50", "hash": "0x99", "timeStamp": "1700000000"},
)


class PartitioningFetcher:
    """A ``PageFetcher`` written from its declared docstring, only.

    It honours every stated requirement including the one this wave
    added: "successive pages of ONE window must PARTITION it … the row
    order must be total and stable across page requests — including
    BETWEEN transactions of the SAME block". The total order is
    ``(block_number, hash)``, which is stable by construction.
    """

    def __init__(self) -> None:
        self.windows: list[tuple[int, int, int, str]] = []

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
        """One page of the total order over ``[start_block, end_block]``."""
        self.windows.append((start_block, end_block, page, sort))
        window = [
            row
            for row in ROWS
            if start_block <= int(row["blockNumber"]) <= end_block
        ]
        window.sort(
            key=lambda row: (int(row["blockNumber"]), row["hash"]),
            reverse=sort == "desc",
        )
        first = (page - 1) * offset
        return window[first : first + offset]


def _decode(
    chain_id: str, address: str, account_id: str, rows
) -> list[LedgerTransaction]:
    """A ``Decoder`` written from its declared signature, only."""
    return [
        LedgerTransaction(
            id="txn_" + row["hash"],
            chain_id=chain_id,
            tx_hash=row["hash"],
            account_id=account_id,
            block_number=int(row["blockNumber"]),
            initiated_at=int(row["timeStamp"]) * 1000,
            confirmed_at=int(row["timeStamp"]) * 1000,
            entries=(Entry(NATIVE, Quantity(1, 18), Direction.IN),),
        )
        for row in rows
    ]


def _engine(ledger, state, fetcher) -> SyncEngine:
    """A ``SyncEngine`` over the four injected ports, ``page_size=2``."""
    return SyncEngine(
        ledger, state, fetcher, _decode, FrozenClock(1_000_000), 0, page_size=2
    )


def _connection() -> ConnectionRecord:
    """The one watched connection every test in this file syncs."""
    return ConnectionRecord(
        id="conn_seam", chain_id=CHAIN, address=ADDRESS, created_at_ms=0
    )


def _stored_ids(ledger: MemoryLedger) -> set[str]:
    """Every LIVE transaction id in the tenant, read back through the port."""
    return {
        event.transaction.id
        for event in ledger.sync(TENANT, None, 1000).events
        if not event.transaction.removed
    }


class TestCursorSeam:
    """The window formula vs the state schema that must resume it."""

    def test_a_split_boundary_block_drains_when_one_backfill_page_fits(self):
        """A tick that affords ONE backfill page must still make progress.

        The seam: ``SyncState`` carries ``backfill_cursor`` and nothing
        else, so ``_backfill``'s page counter is per-CALL. With the
        window now INCLUSIVE of the cursor block, a host whose budget
        affords one live page plus one backfill page re-requests
        ``[0, 100] page=1`` on every tick, so the third transaction in
        block 100 — and everything older — never arrives, and the
        request is spent again every tick, forever.
        """
        ledger, state, fetcher = MemoryLedger(), MemorySyncState(), PartitioningFetcher()
        engine = _engine(ledger, state, fetcher)
        connection = _connection()
        for _ in range(12):
            engine.sync_connection(TENANT, connection, 2)
        assert _stored_ids(ledger) == {
            "txn_0xaa",
            "txn_0xbb",
            "txn_0xcc",
            "txn_0x99",
        }, (
            "the backfill never drained the split boundary block: it "
            f"re-requested {fetcher.windows[-4:]} and the ledger holds "
            f"only {sorted(_stored_ids(ledger))}"
        )

    def test_the_backfill_does_not_repeat_one_window_forever(self):
        """Twelve ticks must not spend twelve identical page requests.

        A budget spent on a window that can never advance is a silent,
        permanent stall — no exception, no log, and the flag stays
        ``False`` forever.
        """
        ledger, state, fetcher = MemoryLedger(), MemorySyncState(), PartitioningFetcher()
        engine = _engine(ledger, state, fetcher)
        connection = _connection()
        for _ in range(12):
            engine.sync_connection(TENANT, connection, 2)
        backfills = [w for w in fetcher.windows if w[3] == "desc" and w[1] != 99_999_999]
        assert len(set(backfills)) == len(backfills), (
            "the backfill re-requested the SAME (start, end, page) window "
            f"{len(backfills) - len(set(backfills))} extra times: {backfills}"
        )


class TestLedgerDedupSeam:
    """Who deduplicates the re-delivered boundary block, and what it costs."""

    def test_a_reorg_removal_survives_the_next_backfill_tick(self):
        """The ledger's declared dedup is NOT identity for a removed row.

        ``LedgerPort.upsert`` (src/auradefi/ledger/port.py:37-42)
        declares that an identical redelivery of a REMOVED row is
        re-added with ``removed=False`` and an ADDED event. The new
        inclusive backfill re-delivers the boundary block on EVERY tick,
        so a transaction the host legitimately orphaned through
        ``mark_removed`` is silently resurrected by the next tick.
        """
        ledger, state, fetcher = MemoryLedger(), MemorySyncState(), PartitioningFetcher()
        engine = _engine(ledger, state, fetcher)
        connection = _connection()
        engine.sync_connection(TENANT, connection, 2)
        assert "txn_0xbb" in _stored_ids(ledger), "fixture: the row must be ingested"

        ledger.mark_removed(TENANT, ["txn_0xbb"])
        assert ledger.get(TENANT, "txn_0xbb").removed is True

        engine.sync_connection(TENANT, connection, 2)
        assert ledger.get(TENANT, "txn_0xbb").removed is True, (
            "the backfill re-delivered the boundary block and the ledger's "
            "declared re-add semantics resurrected a transaction the host "
            "had removed — the removal did not survive one tick"
        )

    def test_a_tick_that_only_redelivers_a_live_row_does_not_claim_an_ingest(self):
        """A redelivery of a still-LIVE unchanged row is not work.

        This originally asserted 0 for a redelivery that RESURRECTED a
        removed row too, and that reading is wrong: it contradicts #22,
        where a row orphaned by an earlier reorg and now back on-chain
        unchanged MUST be re-added and MUST emit ADDED. A resurrection is
        a real ledger state change, so counting it is honest.

        The report-honesty concern is real for the other case, and that is
        what is pinned here: when every redelivered row is already live
        and unchanged, the tick must claim nothing.
        """
        ledger, state, fetcher = MemoryLedger(), MemorySyncState(), PartitioningFetcher()
        engine = _engine(ledger, state, fetcher)
        connection = _connection()
        # Drain first. A tick taken while the backfill is still walking
        # ingests genuinely NEW history, so it is not a redelivery-only tick
        # and asserting 0 against it would be asserting the wrong thing.
        for _ in range(10):
            if state.get_state(TENANT, connection.id).backfill_complete:
                break
            engine.sync_connection(TENANT, connection, 4)
        assert state.get_state(TENANT, connection.id).backfill_complete, (
            "the fixture must reach the drained state the pin is about"
        )

        report = engine.sync_connection(TENANT, connection, 4)

        assert report.transactions_ingested == 0, (
            "every row was already live and unchanged, yet the tick reported "
            f"transactions_ingested={report.transactions_ingested}"
        )

    def test_an_unchanged_redelivery_of_a_removed_row_resurrects_it(self):
        """The other half of the seam, at the boundary that decides it.

        This file's concern was that the backfill's every-tick redelivery
        of the boundary block would silently UNDO a reorg removal. With a
        fixed window and a stored page, a drained backfill stops
        redelivering, so that path is closed — but the ledger contract
        itself still has to hold, because it is what #22 turns on: an
        identical redelivery of a REMOVED row is a resurrection with an
        ADDED event, not a no-op. Pinned here at the port, since a
        completed backfill can no longer reach it through the engine.
        """
        ledger, state, fetcher = MemoryLedger(), MemorySyncState(), PartitioningFetcher()
        engine = _engine(ledger, state, fetcher)
        connection = _connection()
        for _ in range(10):
            if state.get_state(TENANT, connection.id).backfill_complete:
                break
            engine.sync_connection(TENANT, connection, 4)
        stored = _stored_ids(ledger)
        assert stored, "the fixture stored nothing to remove"
        victim = sorted(stored)[0]
        ledger.mark_removed(TENANT, [victim])
        assert victim not in _stored_ids(ledger)

        redelivered = [
            txn
            for txn in _decode(CHAIN, ADDRESS, connection.id, ROWS)
            if txn.id == victim
        ]
        events = ledger.upsert(TENANT, redelivered)

        assert [event.kind.name for event in events] == ["ADDED"], (
            "an unchanged redelivery of a REMOVED row must be re-added — "
            "leaving it removed is the #22 defect"
        )
        assert victim in _stored_ids(ledger)


_URL = (
    "https://api.etherscan.io/v2/api?chainid=1&module=account&action=txlist"
    f"&address={ADDRESS}"
    "&startblock={start}&endblock={end}&page={page}&offset={offset}&sort={sort}"
)


class _CassetteRows:
    """A ``PageFetcher`` over the rows the embed-gate cassette recorded.

    Formula-agnostic: it answers ANY window from the recorded row set and
    records the URL each request would have needed, so the assertion is
    "every window the engine asks for is recorded", never "the window is
    the one I expected".
    """

    def __init__(self, rows: tuple[dict, ...]) -> None:
        self.rows = rows
        self.urls: list[str] = []

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
        """One page of the recorded rows; the URL is recorded as a side effect."""
        self.urls.append(
            _URL.format(
                start=start_block,
                end=end_block,
                page=page,
                offset=offset,
                sort=sort,
            )
        )
        window = [
            row
            for row in self.rows
            if start_block <= int(row["blockNumber"]) <= end_block
        ]
        window.sort(
            key=lambda row: (int(row["blockNumber"]), row["hash"]),
            reverse=sort == "desc",
        )
        first = (page - 1) * offset
        return window[first : first + offset]


class TestRecordedFixtureSeam:
    """The window formula vs the cassette the offline suite replays.

    House rule: "The suite runs offline: no network, no API keys,
    recorded fixtures only." A change to the window arithmetic in
    ``embed/sync.py`` changes the URL sequence, and the recording in
    ``tests/cassettes/embed_gate.json`` is owned by NEITHER order in this
    wave — so the two sides can drift with nothing to catch it inside
    either module.
    """

    def test_every_window_the_engine_requests_is_recorded(self):
        """Replay the gate's own rows through the engine and check the URLs.

        Nothing here encodes what the window SHOULD be: the fetcher
        answers whatever it is asked, so this fails only when the engine
        asks the cassette for a request the recording cannot answer.
        """
        interactions = json.loads(
            (CASSETTES / "embed_gate.json").read_text(encoding="utf-8")
        )["interactions"]
        recorded = {i["request"]["url"] for i in interactions}
        by_hash: dict[str, dict] = {}
        for interaction in interactions:
            if "action=txlist" not in interaction["request"]["url"]:
                continue
            for row in interaction["response"]["json"].get("result", []):
                by_hash[row["hash"]] = row
        assert by_hash, "the embed gate cassette records no txlist rows"

        fetcher = _CassetteRows(tuple(by_hash.values()))
        engine = _engine(MemoryLedger(), MemorySyncState(), fetcher)
        connection = _connection()
        for _ in range(4):
            engine.sync_connection(TENANT, connection, 5)

        missing = sorted({url for url in fetcher.urls if url not in recorded})
        assert not missing, (
            "the engine asked the offline fixture for windows it never "
            "recorded — the suite can only answer these with a live "
            "request:\n" + "\n".join(missing)
        )
