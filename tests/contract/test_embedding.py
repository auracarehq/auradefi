"""THE PHASE 5 GATE (SPEC §11 Phase 5: "a host can import, bind a session,
and sync on its own tick"; SPEC §8; SPEC §13's no-op contract).

The REAL stack — ``from auradefi import Auradefi`` over
``tests/cassettes/embed_gate.json`` — with the HOST owning everything a
host owns:

* its OWN sqlite engine (StaticPool, in-memory) and its OWN
  ``create_all`` against ``ledger.backends.models.metadata``; the library
  emits no DDL and opens no connection we did not hand it. It then reads
  its rows back THROUGH ITS OWN session — the storage-is-a-port proof.
* its OWN source adapter satisfying both seams: ``EtherscanV2`` for
  balances, raw ``txlist`` pages for history.
* a ``CountingTransport`` wrapper, because the §13 no-op contract is
  proven by COUNTING requests, never by timing.

Golden vectors derived independently via ``python3 -c`` from the pinned
formulas in docs/DECISIONS.md, never regenerated from the code:

    usr_  = "usr_"  + sha256("embed|host-user-1")[:16]
    conn_ = "conn_" + sha256("embed|usr_…|address|eip155:1|0x1111…")[:16]
    txn_  = "txn_"  + sha256("eip155:1|0x…0001|conn_d0327e21d9b0ea55")[:16]

The conn_ preimage gained its ``eip155:1`` segment in 0.1.1 (§5 #26): an
id without it let one address be connected on only ONE chain and made two
chains share a sync cursor. Every conn_ and txn_ literal below therefore
changed, and 0.1.0 data is not portable — see docs/DECISIONS.md.

Holdings: 2 ETH @ 2500 + 25 USDC @ 1 = 5025 USD exactly (Decimal, never
float). Transactions: 7, one per hour from 1700000000 (UTC hour 22)
onward — hours 22, 23, 00, 01, 02, 03, 04.

Everything runs offline under the autouse socket guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, select

from auradefi import Auradefi
from auradefi.chains.evm import chain_id_from_caip2
from auradefi.clock import FrozenClock
from auradefi.config import Settings
from auradefi.errors import SourceError
from auradefi.ledger.backends.models import LedgerTransactionRow, metadata
from auradefi.ledger.backends.sqlmodel import SqlModelLedger
from auradefi.money.fiat import Money
from auradefi.prices.inquirer import Inquirer
from auradefi.prices.oracles.defillama import DefiLlamaOracle
from auradefi.sources.evm.etherscan import EtherscanV2

T0 = 1_754_000_000_000
INTERVAL_MS = 60_000
PAGE_SIZE = 2
CHAIN = "eip155:1"
ADDRESS = "0x1111111111111111111111111111111111111111"
EXTERNAL_USER_ID = "host-user-1"
BASE_URL = "https://api.etherscan.io/v2/api"
NO_TRANSACTIONS = "No transactions found"

TENANT = "usr_1e63721d071ea2d9"
CONNECTION_ID = "conn_d0327e21d9b0ea55"
# One id per cassette hash 0x…0001 .. 0x…0007, ascending by block 100..106.
TXN_IDS = (
    "txn_b3618169bbd2dd6b",  # block 100, 0x…0001
    "txn_66000dfdcbfd1e1a",  # block 101, 0x…0002
    "txn_ae616c0eebd4f70c",  # block 102, 0x…0003
    "txn_5b99cc91bcd22381",  # block 103, 0x…0004
    "txn_b3e6f44232f94ed1",  # block 104, 0x…0005
    "txn_13e7d71bab054127",  # block 105, 0x…0006
    "txn_0edc0dca1d17ef58",  # block 106, 0x…0007
)
GOLDEN_TOTAL_USD = Money(Decimal("5025"), "USD")
ACTIVE_HOURS = (22, 23, 0, 1, 2, 3, 4)


class CountingTransport(httpx.BaseTransport):
    """Counts every request that leaves the client, then replays it."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self._inner.handle_request(request)


class HostSource:
    """The HOST's adapter: one object, both seams (SPEC §8).

    ``balances`` delegates to the real ``EtherscanV2``; ``fetch_txlist``
    issues the windowed txlist GET the sync engine asks for and hands
    back the RAW rows — parsing belongs to the decoder, not here.
    """

    def __init__(self, client: httpx.Client, page_size: int) -> None:
        self._client = client
        self._balances = EtherscanV2(client, api_key=None, page_size=page_size)

    def balances(self, chain_id: str, address: str):
        return self._balances.balances(chain_id, address)

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
        response = self._client.get(
            BASE_URL,
            params={
                "chainid": str(chain_id_from_caip2(chain_id)),
                "module": "account",
                "action": "txlist",
                "address": address,
                "startblock": str(start_block),
                "endblock": str(end_block),
                "page": str(page),
                "offset": str(offset),
                "sort": sort,
            },
        )
        if response.status_code != 200:
            raise SourceError(f"etherscan txlist HTTP {response.status_code}")
        envelope = response.json()
        if envelope.get("status") == "0" and envelope.get("message") == NO_TRANSACTIONS:
            return []
        if envelope.get("status") != "1":
            raise SourceError(f"etherscan txlist error: {envelope.get('message')!r}")
        return list(envelope["result"])


@dataclass
class Host:
    """Everything the embedding host owns, plus the library facade."""

    auradefi: Auradefi
    transport: CountingTransport
    clock: FrozenClock
    session_factory: object


def _host(cassette) -> Host:
    """Bind the real stack the way a host would; no library-owned state."""
    transport = CountingTransport(cassette("embed_gate").transport())
    client = httpx.Client(transport=transport)

    # The HOST's database: its own engine, its own DDL, its own sessions.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)

    def session_factory() -> Session:
        return Session(engine)

    clock = FrozenClock(T0)
    auradefi = Auradefi(
        SqlModelLedger(session_factory=session_factory),
        HostSource(client, PAGE_SIZE),
        Inquirer([DefiLlamaOracle(client)]),
        clock,
        Settings(sync_min_interval_s=60),
        sync_page_size=PAGE_SIZE,
    )
    return Host(auradefi, transport, clock, session_factory)


def _connected(cassette) -> Host:
    """A host with the gate address connected."""
    host = _host(cassette)
    host.auradefi.user(EXTERNAL_USER_ID).connect_address(CHAIN, ADDRESS)
    return host


def _fully_synced(cassette) -> Host:
    """Connected, anchored+backfilled, throttled, then resumed to done."""
    host = _connected(cassette)
    host.auradefi.sync(budget=2)
    host.auradefi.sync(budget=2)
    host.clock.advance(INTERVAL_MS)
    host.auradefi.sync(budget=3)
    return host


def _stored_rows(host: Host) -> list[LedgerTransactionRow]:
    """Every ledger row for the tenant, read through the HOST's session."""
    with host.session_factory() as session:
        return list(
            session.exec(
                select(LedgerTransactionRow)
                .where(LedgerTransactionRow.tenant_id == TENANT)
                .order_by(LedgerTransactionRow.block_number)
            )
        )


def test_connect_costs_exactly_one_request_and_derives_the_pinned_id(cassette):
    host = _host(cassette)

    record = host.auradefi.user(EXTERNAL_USER_ID).connect_address(CHAIN, ADDRESS)

    assert host.transport.calls == 1
    assert record.id == CONNECTION_ID
    assert record.chain_id == CHAIN
    assert record.address == ADDRESS
    assert record.created_at_ms == T0


def test_the_first_sync_anchors_then_backfills_inside_its_budget(cassette):
    host = _connected(cassette)
    before = host.transport.calls

    report = host.auradefi.sync(budget=2)

    assert report.no_op is False
    assert report.pages_fetched == 2
    assert (report.live_pages, report.backfill_pages) == (1, 1)
    # 3, not the 4 the exclusive walk reported: the first backfill page
    # deliberately OVERLAPS the anchor's lowest block, because the anchor page
    # may have cut that block in half and nothing here can know whether it
    # did (§5 #18). One redelivered row per connection buys never losing the
    # remainder of a split block; the redelivery adds no event.
    assert report.transactions_ingested == 3
    assert host.transport.calls == before + 2
    row = report.connections[0]
    assert (row.live_cursor, row.backfill_cursor, row.backfill_complete) == (
        106,
        104,
        False,
    )


def test_an_immediate_second_sync_is_a_no_op_proven_by_counting(cassette):
    host = _connected(cassette)
    host.auradefi.sync(budget=2)
    calls = host.transport.calls

    report = host.auradefi.sync(budget=2)

    assert report.no_op is True
    assert report.pages_fetched == 0
    assert report.transactions_ingested == 0
    assert host.transport.calls == calls


def test_the_resumed_sync_drains_the_live_window_and_finishes_history(cassette):
    host = _connected(cassette)
    host.auradefi.sync(budget=2)
    host.auradefi.sync(budget=2)
    host.clock.advance(INTERVAL_MS)
    calls = host.transport.calls

    report = host.auradefi.sync(budget=4)

    assert report.pages_fetched == 4
    assert (report.live_pages, report.backfill_pages) == (1, 3)
    # 4: the overlap the first tick paid for is recovered here, and the
    # resumed backfill continues at the stored page rather than re-reading
    # page 1 of a moved window (§5 #18).
    #
    # The budget is 4, not 3. Confirming a window DRAINED costs one page:
    # the six rows of [0, 105] fill pages 1-3 exactly, so the only evidence
    # that nothing remains is a fourth, short page. The old exclusive walk
    # appeared to finish in three only because its last window was narrower
    # than a full page — it inferred completion from an arithmetic accident,
    # which is the same reasoning that dropped the rest of a split block.
    assert report.transactions_ingested == 4
    assert host.transport.calls == calls + 4
    row = report.connections[0]
    assert row.backfill_complete is True
    assert (row.live_cursor, row.backfill_cursor) == (106, 100)


def test_the_host_reads_every_row_back_through_its_own_session(cassette):
    host = _fully_synced(cassette)

    rows = _stored_rows(host)

    assert len(rows) == 7
    assert [row.id for row in rows] == list(TXN_IDS)
    assert [row.block_number for row in rows] == [100, 101, 102, 103, 104, 105, 106]
    assert {row.account_id for row in rows} == {CONNECTION_ID}
    assert {row.chain_id for row in rows} == {CHAIN}
    assert not any(row.removed for row in rows)
    assert rows[0].id == "txn_b3618169bbd2dd6b"
    assert rows[0].initiated_at == 1_700_000_000_000


def test_holdings_total_the_pinned_usd_value(cassette):
    host = _fully_synced(cassette)

    reports = host.auradefi.holdings()

    assert len(reports) == 1
    assert reports[0].total_value == GOLDEN_TOTAL_USD
    assert reports[0].address == ADDRESS
    assert reports[0].chain_id == CHAIN
    assert reports[0].unpriced == ()
    assert [holding.symbol for holding in reports[0].holdings] == ["ETH", "USDC"]


def test_scalar_metrics_project_the_pinned_twenty_six_values(cassette):
    host = _fully_synced(cassette)

    metrics = host.auradefi.scalar_metrics()

    assert len(metrics) == 26
    values = {metric.name: metric.value for metric in metrics}
    assert values["portfolio_value_usd"] == 5025.0
    assert values["transaction_count"] == 7.0
    for hour in range(24):
        expected = 1.0 if hour in ACTIVE_HOURS else 0.0
        assert values[f"tx_count_hour_{hour:02d}"] == expected
    assert sum(values[f"tx_count_hour_{hour:02d}"] for hour in range(24)) == 7.0


def test_the_library_never_built_the_hosts_schema(cassette):
    """A second binding over a fresh engine finds nothing until create_all."""
    transport = CountingTransport(cassette("embed_gate").transport())
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    def session_factory() -> Session:
        return Session(engine)

    Auradefi(
        SqlModelLedger(session_factory=session_factory),
        HostSource(httpx.Client(transport=transport), PAGE_SIZE),
        Inquirer([]),
        FrozenClock(T0),
        Settings(sync_min_interval_s=60),
        sync_page_size=PAGE_SIZE,
    )

    assert transport.calls == 0
    with pytest.raises(OperationalError):  # no such table: the host never ran DDL
        with session_factory() as session:
            session.exec(select(LedgerTransactionRow)).all()
