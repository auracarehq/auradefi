"""Contract tests for auradefi.embed.facade (SPEC §8).

The embedding surface driven through fakes: one ``FakeSource`` satisfying
BOTH seams (``balances`` for holdings, ``fetch_txlist`` for history), a
``FakePrices`` oracle, ``MemoryLedger`` + ``MemorySyncState``, and a
``FrozenClock`` at T0 = 1_754_000_000_000 with ``sync_min_interval_s=60``.

The connect-time contract is asserted by COUNTING requests, not by
timing: a bad chain, a bad address and a duplicate must each cost ZERO
requests, and a valid connect must cost EXACTLY one.

Ids are golden literals derived independently with ``python3 -c`` from
the pinned formulas in docs/DECISIONS.md — never regenerated from the
code under test. The facade is constructed INSIDE test bodies so a stub
fails with NotImplementedError instead of erroring during collection.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import pytest

from auradefi.clock import FrozenClock
from auradefi.config import Settings
from auradefi.embed.facade import Auradefi
from auradefi.embed.state import MemorySyncState
from auradefi.errors import (
    CaipParseError,
    ConflictError,
    SourceError,
    ValidationError,
)
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.ledger.models import Direction, Entry
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.sources.evm.etherscan import BalanceRecord

T0 = 1_754_000_000_000
INTERVAL_MS = 60_000
CHAIN = "eip155:1"
ADDR_A = "0x1111111111111111111111111111111111111111"
ADDR_B = "0x2222222222222222222222222222222222222222"
SENDER = "0x9999999999999999999999999999999999999999"
ETH = "eip155:1/slip44:60"

# Derived via python3 from the pinned formulas; NEVER from the code here.
#   usr_ = "usr_" + sha256("embed|<external_user_id>")[:16]
#   conn_ = "conn_" + sha256("embed|<tenant>|address|<normalized>")[:16]
#   txn_ = "txn_" + sha256("<chain>|<tx_hash>|<account_id>")[:16]
TENANT_1 = "usr_92f3779edb633e0b"  # unit-user-1
TENANT_2 = "usr_cc1ec9058380eaac"  # unit-user-2
CONN_1A = "conn_25c01723fcf5e88a"  # unit-user-1 x 0x1111…
CONN_1B = "conn_bd449baa130e8a82"  # unit-user-1 x 0x2222…
CONN_2A = "conn_6c7ef3e07cb54341"  # unit-user-2 x 0x1111…
CONN_2B = "conn_12409fc9ced83187"  # unit-user-2 x 0x2222…
TXN_100 = "txn_34fbe670fd9511e8"  # eip155:1 | 0x…064 | CONN_1A
TXN_101 = "txn_5751bbfbe33a4e2b"
TXN_102 = "txn_5f28133ac0d6c635"


def _row(block: int, to_address: str = ADDR_A) -> dict:
    """One Etherscan-shaped txlist row — every field a string (rule #2)."""
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
        window = [
            row
            for row in self._histories.get(address.lower(), [])
            if start_block <= int(row["blockNumber"]) <= end_block
        ]
        window.sort(key=lambda row: int(row["blockNumber"]), reverse=sort == "desc")
        return window[(page - 1) * offset : page * offset]


class BalancesOnly:
    """A source missing the history seam — a very likely host mistake."""

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
) -> Auradefi:
    """Build the facade; call this INSIDE a test body."""
    return Auradefi(
        MemoryLedger(),
        source,
        prices if prices is not None else FakePrices(),
        clock if clock is not None else FrozenClock(T0),
        Settings(sync_min_interval_s=60),
        sync_state=MemorySyncState(),
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


def test_a_malformed_row_surfaces_as_a_source_error():
    # A JSON number in a raw-amount field — never trusted (rule #2).
    source = FakeSource({ADDR_A: (100,)}, corrupt={"value": 1})
    facade = Auradefi(
        MemoryLedger(),
        source,
        FakePrices(),
        FrozenClock(T0),
        Settings(sync_min_interval_s=60),
        sync_page_size=2,
    )
    facade.user("unit-user-1").connect_address(CHAIN, ADDR_A)

    with pytest.raises(SourceError):
        facade.sync(budget=5)
