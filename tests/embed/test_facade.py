"""Contract tests for auradefi.embed.facade (SPEC §8).

The embedding surface driven through fakes: one ``FakeSource`` satisfying
BOTH seams (``balances`` for holdings, ``fetch_txlist`` for history), a
``FakePrices`` oracle, ``MemoryLedger`` + ``MemorySyncState``, and a
``FrozenClock`` at T0 = 1_754_000_000_000 with ``sync_min_interval_s=60``.

The connect-time contract is asserted by COUNTING requests, not by
timing: a bad chain, a bad address and a duplicate must each cost ZERO
requests, and a valid connect must cost EXACTLY one.

Ids are golden literals derived independently with ``python3 -c`` from
the pinned formulas in docs/internal/DECISIONS.md, never regenerated from the
code under test. The facade is constructed INSIDE test bodies so a stub
fails with NotImplementedError instead of erroring during collection.

The 0.1.1 regressions (RELEASE_0.1.1 §5) live at the foot of the file and
each pins a VALUE a caller can observe, because all four defects are
silent: the tenant rows are written under (#19), the two ids one address
gets on two chains (#26), the work a restarted worker does (#21), and the
rows a healthy connection ingests beside a broken sibling (#24).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import pytest

from fastapi.testclient import TestClient

from auradefi.api.app import create_app
from auradefi.api.deps import Deps
from auradefi.chains.registry import ChainRegistry
from auradefi.clock import FrozenClock
from auradefi.config import Settings
from auradefi.embed.facade import Auradefi
from auradefi.embed.models import ConnectionRecord
from auradefi.embed.state import MemorySyncState
from auradefi.errors import (
    CaipParseError,
    ConflictError,
    SourceError,
    UnknownChainError,
    ValidationError,
)
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.ledger.models import Direction, Entry
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.sources.evm.etherscan import BalanceRecord
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.keys import ApiKeyStore
from auradefi.tenancy.models import Environment, Scope
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import RevocationSet
from auradefi.webhooks.deliver import WebhookStore

T0 = 1_754_000_000_000
INTERVAL_MS = 60_000
CHAIN = "eip155:1"
CHAIN_POLYGON = "eip155:137"
# Arbitrum One: a well-formed CAIP-2 the seeded ChainRegistry does NOT hold.
UNSEEDED_CHAIN = "eip155:42161"
ADDR_A = "0x1111111111111111111111111111111111111111"
ADDR_B = "0x2222222222222222222222222222222222222222"
SENDER = "0x9999999999999999999999999999999999999999"
ETH = "eip155:1/slip44:60"

# Derived via python3 from the pinned formulas; NEVER from the code here.
#   usr_ = "usr_" + sha256("<project_id>|<external_user_id>")[:16]
#   conn_ = "conn_" + sha256("embed|<tenant>|address|<chain>|<normalized>")[:16]
#   txn_ = "txn_" + sha256("<chain>|<tx_hash>|<account_id>")[:16]
TENANT_1 = "usr_92f3779edb633e0b"  # embed | unit-user-1
TENANT_2 = "usr_cc1ec9058380eaac"  # embed | unit-user-2
CONN_1A = "conn_b96ce22765f9a8ef"  # unit-user-1 x eip155:1 x 0x1111…
CONN_1B = "conn_a92062257d85da8e"  # unit-user-1 x eip155:1 x 0x2222…
CONN_2A = "conn_d3c905caee693f0a"  # unit-user-2 x eip155:1 x 0x1111…
CONN_2B = "conn_7a6f79af31ac11b0"  # unit-user-2 x eip155:1 x 0x2222…
CONN_1A_POLYGON = "conn_8316fb8c9166fa96"  # unit-user-1 x eip155:137 x 0x1111…
TXN_100 = "txn_43583a69cf45329e"  # eip155:1 | 0x…064 | CONN_1A
TXN_101 = "txn_e56da9318ad266f4"
TXN_102 = "txn_39ab81bd12b3ad9e"

# RELEASE_0.1.1 §5 #19: the same library under a host's REAL project id.
PROJECT_X = "proj_9f8e7d6c5b4a3928"
TENANT_1_UNDER_X = "usr_f13ef24edb915127"  # PROJECT_X | unit-user-1
CONN_1A_UNDER_X = "conn_8aae188d384474d4"  # that tenant x eip155:1 x 0x1111…
TXN_100_UNDER_X = "txn_933467c7ed77e8cf"  # eip155:1 | 0x…064 | CONN_1A_UNDER_X


def _row(block: int, to_address: str = ADDR_A) -> dict:
    """One Etherscan-shaped txlist row: every field a string (rule #2)."""
    return {
        "blockNumber": str(block),
        "timeStamp": str(1_700_000_000 + (block % 100) * 3600),
        "hash": "0x" + f"{block:064x}",
        "from": SENDER,
        "to": to_address,
        "value": "1000000000000000000",
        "gasUsed": "21000",
        "gasPrice": "10000000000",
        "isError": "0",
    }


class FakeSource:
    """One object, both seams: ``balances`` and ``fetch_txlist``."""

    def __init__(
        self,
        histories: dict[str, Sequence[int]] | None = None,
        holdings: dict[str, Sequence[BalanceRecord]] | None = None,
        *,
        probe_error: Exception | None = None,
        corrupt: dict | None = None,
        fails_for: Sequence[str] = (),
    ) -> None:
        self._histories = {
            address.lower(): [
                {**_row(block, address.lower()), **(corrupt or {})} for block in blocks
            ]
            for address, blocks in (histories or {}).items()
        }
        self._holdings = {
            address.lower(): tuple(records)
            for address, records in (holdings or {}).items()
        }
        self._probe_error = probe_error
        # Addresses whose upstream is down once ``armed``: connect first,
        # break the source after, exactly as a live outage arrives.
        self._failing = {address.lower() for address in fails_for}
        self.armed = False
        self.txlist_calls: list[dict] = []
        self.balance_calls: list[tuple[str, str]] = []

    def balances(self, chain_id: str, address: str) -> list[BalanceRecord]:
        self.balance_calls.append((chain_id, address))
        return list(self._holdings.get(address.lower(), ()))

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
        self.txlist_calls.append(
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
        if self._probe_error is not None and offset == 1:
            raise self._probe_error
        if self.armed and address.lower() in self._failing:
            raise SourceError(f"explorer unavailable for {address}")
        window = [
            row
            for row in self._histories.get(address.lower(), [])
            if start_block <= int(row["blockNumber"]) <= end_block
        ]
        window.sort(key=lambda row: int(row["blockNumber"]), reverse=sort == "desc")
        return window[(page - 1) * offset : page * offset]


class PerChainSource:
    """Both seams, with history keyed by ``(chain_id, address)``.

    The #26 fixture: the SAME address holds a different history on each
    chain, so two connections sharing one cursor cannot pass by accident.
    """

    def __init__(self, histories: dict[tuple[str, str], Sequence[int]]) -> None:
        self._histories = {
            (chain_id, address.lower()): [
                _row(block, address.lower()) for block in blocks
            ]
            for (chain_id, address), blocks in histories.items()
        }
        self.txlist_calls: list[dict] = []

    def balances(self, chain_id: str, address: str) -> list[BalanceRecord]:
        return []

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
        self.txlist_calls.append({"chain_id": chain_id, "address": address})
        window = [
            row
            for row in self._histories.get((chain_id, address.lower()), [])
            if start_block <= int(row["blockNumber"]) <= end_block
        ]
        window.sort(key=lambda row: int(row["blockNumber"]), reverse=sort == "desc")
        return window[(page - 1) * offset : page * offset]


class BalancesOnly:
    """A source missing the history seam: a very likely host mistake."""

    def balances(self, chain_id: str, address: str) -> list[BalanceRecord]:
        return []


class HistoryOnly:
    """A source missing the balances seam."""

    def fetch_txlist(self, chain_id: str, address: str, **kwargs) -> list[dict]:
        return []


class FakePrices:
    """A ``PriceOracle`` over a fixed USD price table."""

    def __init__(self, prices: dict[str, str] | None = None) -> None:
        self._prices = prices or {}
        self.calls: list[list[str]] = []

    def usd_prices(self, caip19s: Sequence[str]) -> dict[str, Money]:
        self.calls.append(list(caip19s))
        return {
            caip19: Money(Decimal(self._prices[caip19]), "USD")
            for caip19 in caip19s
            if caip19 in self._prices
        }


def _facade(
    source: object,
    *,
    prices: FakePrices | None = None,
    clock: FrozenClock | None = None,
    page_size: int = 2,
    decoder=None,
    ledger: MemoryLedger | None = None,
    state: MemorySyncState | None = None,
    settings: Settings | None = None,
) -> Auradefi:
    """Build the facade; call this INSIDE a test body.

    ``ledger`` and ``state`` are injectable so a test can rebind a FRESH
    facade over the SAME host-owned storage. The restart the library
    must survive.
    """
    return Auradefi(
        ledger if ledger is not None else MemoryLedger(),
        source,
        prices if prices is not None else FakePrices(),
        clock if clock is not None else FrozenClock(T0),
        settings if settings is not None else Settings(sync_min_interval_s=60),
        sync_state=state if state is not None else MemorySyncState(),
        decoder=decoder,
        sync_page_size=page_size,
    )


def _probe_call(address: str = ADDR_A) -> dict:
    """The pinned connect-time probe: one row, newest first."""
    return {
        "chain_id": CHAIN,
        "address": address,
        "start_block": 0,
        "end_block": 99_999_999,
        "page": 1,
        "offset": 1,
        "sort": "desc",
    }


# --------------------------------------------------------------------
# binding
# --------------------------------------------------------------------


@pytest.mark.parametrize("source", [BalancesOnly(), HistoryOnly(), object()])
def test_a_source_missing_either_seam_is_rejected_at_bind_time(source):
    with pytest.raises(ValidationError):
        _facade(source)


def test_binding_performs_no_io():
    source = FakeSource({ADDR_A: (100,)})
    _facade(source)
    assert source.txlist_calls == []
    assert source.balance_calls == []


# --------------------------------------------------------------------
# user(): get-or-create over an opaque, bearer-equivalent id
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "external_user_id", ["user@example.dev", "", " ", "a" * 129, "user id"]
)
def test_an_id_outside_the_pinned_charset_is_rejected(external_user_id):
    facade = _facade(FakeSource())
    with pytest.raises(ValidationError):
        facade.user(external_user_id)


def test_user_is_pure_get_or_create():
    source = FakeSource()
    facade = _facade(source)

    first = facade.user("unit-user-1")
    second = facade.user("unit-user-1")

    assert first.tenant_id == second.tenant_id == TENANT_1
    assert facade.user("unit-user-2").tenant_id == TENANT_2
    assert source.txlist_calls == []


# --------------------------------------------------------------------
# connect_address(): validate NOW, not on a background tick
# --------------------------------------------------------------------


@pytest.mark.parametrize("address", ["xyz", "0x1234", "", ADDR_A[:-1]])
def test_a_bad_address_is_rejected_before_any_request(address):
    source = FakeSource()
    user = _facade(source).user("unit-user-1")

    with pytest.raises(ValidationError):
        user.connect_address(CHAIN, address)

    assert source.txlist_calls == []


@pytest.mark.parametrize("chain", ["ethereum", "eth-mainnet", "1", "EIP155:1"])
def test_a_vendor_chain_name_is_rejected_before_any_request(chain):
    source = FakeSource()
    user = _facade(source).user("unit-user-1")

    with pytest.raises(CaipParseError):
        user.connect_address(chain, ADDR_A)

    assert source.txlist_calls == []


def test_a_valid_connect_issues_exactly_one_probe():
    source = FakeSource({ADDR_A: (100,)})
    user = _facade(source).user("unit-user-1")

    record = user.connect_address(CHAIN, ADDR_A)

    assert source.txlist_calls == [_probe_call()]
    assert record.id == CONN_1A
    assert record.chain_id == CHAIN
    assert record.address == ADDR_A
    assert record.created_at_ms == T0
    assert user.connections() == (record,)


def test_a_mixed_case_address_is_stored_lowercased():
    source = FakeSource({ADDR_A: ()})
    user = _facade(source).user("unit-user-1")

    record = user.connect_address(CHAIN, "0x" + "1" * 40)

    assert record.address == ADDR_A
    assert record.id == CONN_1A


def test_an_empty_probe_result_is_a_valid_fresh_address():
    source = FakeSource()
    user = _facade(source).user("unit-user-1")

    record = user.connect_address(CHAIN, ADDR_A)

    assert record.id == CONN_1A
    assert len(source.txlist_calls) == 1


def test_a_probe_failure_propagates_and_stores_nothing():
    source = FakeSource(probe_error=SourceError("explorer unavailable"))
    user = _facade(source).user("unit-user-1")

    with pytest.raises(SourceError):
        user.connect_address(CHAIN, ADDR_A)

    assert user.connections() == ()
    assert len(source.txlist_calls) == 1


def test_reconnecting_the_same_address_conflicts_without_a_second_probe():
    source = FakeSource({ADDR_A: (100,)})
    user = _facade(source).user("unit-user-1")
    user.connect_address(CHAIN, ADDR_A)

    with pytest.raises(ConflictError) as caught:
        user.connect_address(CHAIN, ADDR_A.upper().replace("0X", "0x"))

    assert caught.value.existing_id == CONN_1A
    assert len(source.txlist_calls) == 1
    assert len(user.connections()) == 1


def test_two_users_may_watch_the_same_address():
    source = FakeSource({ADDR_A: (100,)})
    facade = _facade(source)

    first = facade.user("unit-user-1").connect_address(CHAIN, ADDR_A)
    second = facade.user("unit-user-2").connect_address(CHAIN, ADDR_A)

    assert (first.id, second.id) == (CONN_1A, CONN_2A)
    assert len(source.txlist_calls) == 2


# --------------------------------------------------------------------
# sync(): one shared budget, self-throttled
# --------------------------------------------------------------------


def test_sync_without_connections_is_a_vacuous_no_op():
    source = FakeSource()
    facade = _facade(source)
    facade.user("unit-user-1")

    report = facade.sync()

    assert report.no_op is True
    assert (report.pages_fetched, report.live_pages, report.backfill_pages) == (0, 0, 0)
    assert report.transactions_ingested == 0
    assert report.connections == ()
    assert source.txlist_calls == []


@pytest.mark.parametrize("budget", [0, -1])
def test_a_sync_budget_below_one_raises(budget):
    source = FakeSource({ADDR_A: (100,)})
    facade = _facade(source)
    facade.user("unit-user-1").connect_address(CHAIN, ADDR_A)
    probes = len(source.txlist_calls)

    with pytest.raises(ValidationError):
        facade.sync(budget)

    assert len(source.txlist_calls) == probes


def test_one_shared_budget_is_spent_across_connections_in_creation_order():
    source = FakeSource({ADDR_A: (100,), ADDR_B: (200, 201, 202)})
    facade = _facade(source)
    user = facade.user("unit-user-1")
    user.connect_address(CHAIN, ADDR_A)
    user.connect_address(CHAIN, ADDR_B)

    report = facade.sync(budget=3)

    assert [row.connection_id for row in report.connections] == [CONN_1A, CONN_1B]
    assert (report.pages_fetched, report.live_pages, report.backfill_pages) == (3, 2, 1)
    assert report.transactions_ingested == 4
    assert report.no_op is False


def test_a_connection_beyond_the_exhausted_budget_is_not_visited():
    source = FakeSource({ADDR_A: (100, 101, 102, 103, 104), ADDR_B: (200,)})
    facade = _facade(source)
    user = facade.user("unit-user-1")
    user.connect_address(CHAIN, ADDR_A)
    user.connect_address(CHAIN, ADDR_B)

    report = facade.sync(budget=2)

    assert [row.connection_id for row in report.connections] == [CONN_1A]
    assert report.pages_fetched == 2
    assert [call["address"] for call in source.txlist_calls[2:]] == [ADDR_A, ADDR_A]


def test_the_second_sync_in_quick_succession_is_a_no_op():
    source = FakeSource({ADDR_A: (100, 101, 102)})
    facade = _facade(source)
    facade.user("unit-user-1").connect_address(CHAIN, ADDR_A)
    facade.sync(budget=5)
    calls = len(source.txlist_calls)

    report = facade.sync(budget=5)

    assert report.no_op is True
    assert report.pages_fetched == 0
    assert len(source.txlist_calls) == calls


def test_a_user_handle_syncs_only_its_own_connections():
    source = FakeSource({ADDR_A: (100,), ADDR_B: (200,)})
    facade = _facade(source)
    facade.user("unit-user-1").connect_address(CHAIN, ADDR_A)
    facade.user("unit-user-2").connect_address(CHAIN, ADDR_B)

    report = facade.user("unit-user-2").sync(budget=5)

    assert [row.connection_id for row in report.connections] == [CONN_2B]
    assert [
        call["address"] for call in source.txlist_calls if call["offset"] != 1
    ] == [ADDR_B]


# --------------------------------------------------------------------
# holdings() and scalar_metrics()
# --------------------------------------------------------------------


def _eth(whole: int) -> BalanceRecord:
    return BalanceRecord(
        caip19=ETH,
        symbol="ETH",
        quantity=Quantity(whole * 10**18, 18),
        contract_address=None,
    )


def test_holdings_are_one_priced_report_per_connection_in_creation_order():
    source = FakeSource(
        {ADDR_A: (), ADDR_B: ()}, {ADDR_A: (_eth(2),), ADDR_B: (_eth(3),)}
    )
    facade = _facade(source, prices=FakePrices({ETH: "2500"}))
    user = facade.user("unit-user-1")
    user.connect_address(CHAIN, ADDR_A)
    user.connect_address(CHAIN, ADDR_B)

    reports = facade.holdings()

    assert [report.address for report in reports] == [ADDR_A, ADDR_B]
    assert reports[0].total_value == Money(Decimal("5000"), "USD")
    assert reports[1].total_value == Money(Decimal("7500"), "USD")
    assert reports[0].as_of_ms == T0


def test_scalar_metrics_project_twenty_six_values_per_connection():
    source = FakeSource({ADDR_A: (100, 101, 102)}, {ADDR_A: (_eth(2),)})
    facade = _facade(source, prices=FakePrices({ETH: "2500"}))
    facade.user("unit-user-1").connect_address(CHAIN, ADDR_A)
    facade.sync(budget=5)

    metrics = facade.scalar_metrics()

    assert len(metrics) == 26
    by_name = {metric.name: metric.value for metric in metrics}
    assert by_name["portfolio_value_usd"] == 5000.0
    assert by_name["transaction_count"] == 3.0
    assert by_name["tx_count_hour_22"] == 1.0
    assert by_name["tx_count_hour_23"] == 1.0
    assert by_name["tx_count_hour_00"] == 1.0
    assert sum(by_name[f"tx_count_hour_{hour:02d}"] for hour in range(24)) == 3.0
    assert all(metric.at_ms == T0 for metric in metrics)


def test_scalar_metrics_count_only_the_connections_own_transactions():
    source = FakeSource(
        {ADDR_A: (100, 101), ADDR_B: (200,)},
        {ADDR_A: (_eth(2),), ADDR_B: (_eth(3),)},
    )
    facade = _facade(source, prices=FakePrices({ETH: "2500"}))
    user = facade.user("unit-user-1")
    user.connect_address(CHAIN, ADDR_A)
    user.connect_address(CHAIN, ADDR_B)
    facade.sync(budget=6)

    metrics = facade.scalar_metrics()

    assert len(metrics) == 52
    assert metrics[0] == ("portfolio_value_usd", T0, 5000.0)
    assert metrics[1] == ("transaction_count", T0, 2.0)
    assert metrics[26] == ("portfolio_value_usd", T0, 7500.0)
    assert metrics[27] == ("transaction_count", T0, 1.0)


# --------------------------------------------------------------------
# the default decoder: bound lazily, the real composition
# --------------------------------------------------------------------


def test_the_default_decoder_is_the_real_txlist_decode_bridge_composition():
    source = FakeSource({ADDR_A: (100, 101, 102)})
    ledger = MemoryLedger()
    facade = Auradefi(
        ledger,
        source,
        FakePrices(),
        FrozenClock(T0),
        Settings(sync_min_interval_s=60),
        sync_page_size=2,
    )
    facade.user("unit-user-1").connect_address(CHAIN, ADDR_A)

    report = facade.sync(budget=5)

    assert report.transactions_ingested == 3
    page = ledger.sync(TENANT_1, None, 100)
    stored = {event.transaction.id: event.transaction for event in page.events}
    assert sorted(stored) == sorted([TXN_100, TXN_101, TXN_102])
    first = stored[TXN_100]
    assert first.account_id == CONN_1A
    assert first.chain_id == CHAIN
    assert first.block_number == 100
    assert first.initiated_at == 1_700_000_000_000
    assert first.entries == (
        Entry(asset_id=ETH, quantity=Quantity(10**18, 18), direction=Direction.IN),
    )


# pins: a malformed row is contained in ITS OWN connection's row and
#       declared there: it never escapes sync() to abort the tick
#       (RELEASE_0.1.1 §5 #24; supersedes the 0.1.0 propagation contract).
def test_a_malformed_row_is_reported_against_its_connection_not_raised():
    # A JSON number in a raw-amount field, never trusted (rule #2).
    source = FakeSource({ADDR_A: (100,)}, corrupt={"value": 1})
    ledger = MemoryLedger()
    facade = Auradefi(
        ledger,
        source,
        FakePrices(),
        FrozenClock(T0),
        Settings(sync_min_interval_s=60),
        sync_page_size=2,
    )
    facade.user("unit-user-1").connect_address(CHAIN, ADDR_A)

    report = facade.sync(budget=5)

    assert report.failed_connections == (CONN_1A,)
    assert [row.failed for row in report.connections] == [True]
    assert report.no_op is False
    assert report.transactions_ingested == 0
    assert ledger.sync(TENANT_1, None, 100).events == ()


# pins: a bug is not an API contract. An exception that is NOT an
#       auradefi error escapes sync() instead of being filed as a
#       per-connection failure.
def test_a_non_auradefi_exception_still_escapes_the_sync_loop():
    def exploding_decoder(chain_id, address, account_id, rows):
        raise RuntimeError("a bug in the host's decoder")

    source = FakeSource({ADDR_A: (100,)})
    facade = _facade(source, decoder=exploding_decoder)
    facade.user("unit-user-1").connect_address(CHAIN, ADDR_A)

    with pytest.raises(RuntimeError):
        facade.sync(budget=5)


# --------------------------------------------------------------------
# #19: the library and the API must address ONE ledger tenant
# --------------------------------------------------------------------


def _ledger_rows(ledger: MemoryLedger, tenant_id: str) -> dict:
    """Every transaction ``tenant_id`` can read back, keyed by id."""
    return {
        event.transaction.id: event.transaction
        for event in ledger.sync(tenant_id, None, 100).events
    }


# pins: the tenant a facade keys the ledger by comes from
#       settings.project_id, so a host running the library under its real
#       project writes rows that project's API can read.
def test_the_configured_project_id_derives_the_ledger_tenant():
    source = FakeSource({ADDR_A: (100, 101, 102)})
    ledger = MemoryLedger()
    facade = _facade(
        source,
        ledger=ledger,
        settings=Settings(sync_min_interval_s=60, project_id=PROJECT_X),
    )
    user = facade.user("unit-user-1")

    assert user.tenant_id == TENANT_1_UNDER_X
    record = user.connect_address(CHAIN, ADDR_A)
    assert record.id == CONN_1A_UNDER_X
    facade.sync(budget=5)

    rows = _ledger_rows(ledger, TENANT_1_UNDER_X)
    assert len(rows) == 3
    assert TXN_100_UNDER_X in rows
    # …and NOTHING was written under the hardcoded "embed" project.
    assert _ledger_rows(ledger, TENANT_1) == {}


# pins: with no project_id configured the derivation is byte-identical to
#       0.1.0's, so library data written before 0.1.1 stays addressable.
def test_the_default_project_id_still_derives_the_0_1_0_tenant():
    source = FakeSource({ADDR_A: (100,)})
    ledger = MemoryLedger()
    facade = _facade(source, ledger=ledger)

    user = facade.user("unit-user-1")

    assert user.tenant_id == TENANT_1
    assert Settings().project_id == "embed"
    user.connect_address(CHAIN, ADDR_A)
    facade.sync(budget=5)
    assert len(_ledger_rows(ledger, TENANT_1)) == 1


# pins: rows the facade ingests for project X come back out of
#       GET /crypto/sync for a token of project X: one derivation across
#       both surfaces, so a library write IS an HTTP read.
def test_rows_ingested_by_the_library_are_readable_over_that_projects_api():
    clock = FrozenClock(T0)
    ledger, tenancy, keys = MemoryLedger(), TenancyStore(), ApiKeyStore()
    organisation = tenancy.create_organisation("acme", clock)
    project = tenancy.create_project(organisation.id, "main", Environment.TEST, clock)
    client = TestClient(
        create_app(
            Deps(
                tenancy=tenancy,
                keys=keys,
                quota=QuotaCounter(QuotaLimits(500, 5_000, 50_000), clock),
                audit=AuditLog(),
                revocations=RevocationSet(),
                ledger=ledger,
                webhooks=WebhookStore(),
                chains=ChainRegistry(),
                clock=clock,
                signing_secret_for={project.id: project.signing_secret}.get,
            )
        )
    )
    _record, plaintext = keys.issue(
        project.id,
        Environment.TEST,
        (Scope.USERS_ADMIN, Scope.ACCOUNTS_READ, Scope.ACCOUNTS_WRITE),
        clock,
    )
    minted = client.post(
        "/auth/token",
        json={"external_user_id": "unit-user-1"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert minted.status_code == 200, minted.text

    facade = _facade(
        FakeSource({ADDR_A: (100, 101, 102)}),
        clock=clock,
        ledger=ledger,
        settings=Settings(sync_min_interval_s=60, project_id=project.id),
    )
    facade.user("unit-user-1").connect_address(CHAIN, ADDR_A)
    report = facade.sync(budget=5)
    assert report.transactions_ingested == 3

    page = client.get(
        "/crypto/sync?limit=100",
        headers={"Authorization": f"Bearer {minted.json()['token']}"},
    )
    assert page.status_code == 200, page.text
    assert {row["transaction_id"] for row in page.json()["added"]} == {
        transaction.id
        for transaction in _ledger_rows(ledger, facade.user("unit-user-1").tenant_id).values()
    }
    assert len(page.json()["added"]) == 3


# --------------------------------------------------------------------
# #26. A connection is scoped to its chain
# --------------------------------------------------------------------


# pins: the same address on a second chain is a SECOND connection, not a
#       ConflictError naming an id the caller already owns.
def test_the_same_address_connects_on_two_chains():
    source = PerChainSource({(CHAIN, ADDR_A): (100,), (CHAIN_POLYGON, ADDR_A): (300,)})
    user = _facade(source).user("unit-user-1")

    mainnet = user.connect_address(CHAIN, ADDR_A)
    polygon = user.connect_address(CHAIN_POLYGON, ADDR_A)

    assert (mainnet.id, polygon.id) == (CONN_1A, CONN_1A_POLYGON)
    assert mainnet.chain_id == CHAIN
    assert polygon.chain_id == CHAIN_POLYGON
    assert [record.id for record in user.connections()] == [CONN_1A, CONN_1A_POLYGON]


# pins: each chain-scoped connection carries its OWN sync cursor, so one
#       chain's head never silently stands in for the other's.
def test_two_chains_on_one_address_keep_independent_cursors():
    source = PerChainSource(
        {(CHAIN, ADDR_A): (100, 101), (CHAIN_POLYGON, ADDR_A): (300,)}
    )
    ledger, state = MemoryLedger(), MemorySyncState()
    facade = _facade(source, ledger=ledger, state=state, page_size=2)
    user = facade.user("unit-user-1")
    user.connect_address(CHAIN, ADDR_A)
    user.connect_address(CHAIN_POLYGON, ADDR_A)

    report = facade.sync(budget=6)

    rows = {row.connection_id: row for row in report.connections}
    assert sorted(rows) == sorted([CONN_1A, CONN_1A_POLYGON])
    assert rows[CONN_1A].live_cursor == 101
    assert rows[CONN_1A_POLYGON].live_cursor == 300
    assert state.get_state(TENANT_1, CONN_1A).live_cursor == 101
    assert state.get_state(TENANT_1, CONN_1A_POLYGON).live_cursor == 300
    assert report.transactions_ingested == 3
    account_ids = [
        event.transaction.account_id
        for event in ledger.sync(TENANT_1, None, 100).events
    ]
    assert sorted(account_ids) == sorted([CONN_1A, CONN_1A, CONN_1A_POLYGON])


# --------------------------------------------------------------------
# #21: a restarted worker syncs from the state port, not from memory
# --------------------------------------------------------------------


# pins: a FRESH facade bound over an existing state port enumerates the
#       tenants that port holds, so a restarted worker does the stored
#       connection's work instead of returning a success-shaped no_op.
def test_a_fresh_facade_over_stored_state_syncs_instead_of_no_opping():
    source = FakeSource({ADDR_A: (100, 101, 102)})
    ledger, state, clock = MemoryLedger(), MemorySyncState(), FrozenClock(T0)
    first = _facade(source, clock=clock, ledger=ledger, state=state, page_size=2)
    first.user("unit-user-1").connect_address(CHAIN, ADDR_A)
    del first  # the worker restarts; the host rebinds over its own state

    restarted = _facade(source, clock=clock, ledger=ledger, state=state, page_size=2)
    report = restarted.sync(budget=5)

    assert report.no_op is False
    assert [row.connection_id for row in report.connections] == [CONN_1A]
    assert report.transactions_ingested == 3
    assert len(_ledger_rows(ledger, TENANT_1)) == 3


# pins: enumeration comes from the port even for a tenant this process
#       has never seen, no user() call revives the connection.
def test_a_tenant_never_named_in_this_process_is_still_synced():
    source = FakeSource({ADDR_B: (200,)})
    state = MemorySyncState()
    state.add_connection(
        TENANT_2,
        ConnectionRecord(
            id=CONN_2B, chain_id=CHAIN, address=ADDR_B, created_at_ms=T0
        ),
    )
    ledger = MemoryLedger()
    facade = _facade(source, ledger=ledger, state=state, page_size=2)

    report = facade.sync(budget=5)

    assert [row.connection_id for row in report.connections] == [CONN_2B]
    assert report.transactions_ingested == 1
    assert len(_ledger_rows(ledger, TENANT_2)) == 1


# --------------------------------------------------------------------
# #24: an unseeded chain, and one failure that must cost only itself
# --------------------------------------------------------------------


# pins: an address on a chain the registry does not hold is refused AT
#       CONNECT, so no connection can exist that every later sync() dies
#       on; the refusal names the chain and costs zero requests.
def test_an_unseeded_chain_is_refused_at_connect_time():
    source = FakeSource({ADDR_A: (100,)})
    user = _facade(source).user("unit-user-1")

    with pytest.raises(UnknownChainError) as caught:
        user.connect_address(UNSEEDED_CHAIN, ADDR_A)

    assert UNSEEDED_CHAIN in str(caught.value)
    assert source.txlist_calls == []
    assert user.connections() == ()


# pins: a seeded chain still connects. The membership check refuses the
#       unknown, it does not refuse everything.
def test_every_seeded_evm_chain_still_connects():
    source = FakeSource({ADDR_A: ()})
    user = _facade(source).user("unit-user-1")

    for chain in (CHAIN, CHAIN_POLYGON, "eip155:8453"):
        assert user.connect_address(chain, ADDR_A).chain_id == chain


# pins: a connection whose source fails mid-tick costs only itself: its
#       siblings still ingest, and the tick names it as failed rather
#       than reporting clean success.
def test_one_failing_connection_does_not_starve_its_siblings():
    source = FakeSource({ADDR_A: (100, 101, 102), ADDR_B: (200,)}, fails_for=(ADDR_B,))
    ledger = MemoryLedger()
    facade = _facade(source, ledger=ledger, page_size=2)
    user = facade.user("unit-user-1")
    user.connect_address(CHAIN, ADDR_B)  # the doomed one goes FIRST
    user.connect_address(CHAIN, ADDR_A)
    source.armed = True  # the upstream goes down for one address only

    report = facade.sync(budget=6)

    rows = {row.connection_id: row for row in report.connections}
    assert sorted(rows) == sorted([CONN_1A, CONN_1B])
    assert rows[CONN_1B].failed is True
    assert rows[CONN_1B].transactions_ingested == 0
    assert rows[CONN_1A].failed is False
    assert rows[CONN_1A].transactions_ingested == 3
    assert report.failed_connections == (CONN_1B,)
    assert report.no_op is False
    assert report.transactions_ingested == 3
    account_ids = {
        event.transaction.account_id
        for event in ledger.sync(TENANT_1, None, 100).events
    }
    assert account_ids == {CONN_1A}


# pins: a 0.1.0 connection stored on a chain the registry never held
#       fails alone. The connect-time refusal cannot reach rows already
#       in a host's durable state, so isolation has to.
def test_a_stored_connection_on_an_unseeded_chain_fails_alone():
    source = FakeSource({ADDR_A: (100,), ADDR_B: (200,)})
    ledger, state = MemoryLedger(), MemorySyncState()
    state.add_connection(
        TENANT_1,
        ConnectionRecord(
            id="conn_00000000000000ab",
            chain_id=UNSEEDED_CHAIN,
            address=ADDR_B,
            created_at_ms=T0,
        ),
    )
    facade = _facade(source, ledger=ledger, state=state, page_size=2)
    facade.user("unit-user-1").connect_address(CHAIN, ADDR_A)

    report = facade.sync(budget=6)

    rows = {row.connection_id: row for row in report.connections}
    assert rows["conn_00000000000000ab"].failed is True
    assert rows[CONN_1A].failed is False
    assert rows[CONN_1A].transactions_ingested == 1
    assert report.failed_connections == ("conn_00000000000000ab",)
    assert len(_ledger_rows(ledger, TENANT_1)) == 1


# pins: a failing connection spends ONE unit of the shared budget, so N
#       broken connections cannot issue N requests against a budget of 1.
def test_a_failing_connection_still_spends_one_unit_of_the_budget():
    source = FakeSource(
        {ADDR_A: (100,), ADDR_B: (200,)}, fails_for=(ADDR_A, ADDR_B)
    )
    facade = _facade(source, page_size=2)
    user = facade.user("unit-user-1")
    user.connect_address(CHAIN, ADDR_A)
    user.connect_address(CHAIN, ADDR_B)
    source.armed = True

    report = facade.sync(budget=1)

    assert [row.connection_id for row in report.connections] == [CONN_1A]
    assert report.failed_connections == (CONN_1A,)
