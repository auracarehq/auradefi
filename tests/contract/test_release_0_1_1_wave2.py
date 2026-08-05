"""Acceptance gate — release 0.1.1, wave 2 (RELEASE_0.1.1 §5, waves A and B).

Done when #18, #19, #21, #22, #24 and #26 are each closed with a regression
test that fails against the unfixed code (§6). Four of the six are SILENT —
they lose transactions or return empty results while reporting success — so
every test below asserts a VALUE a caller can observe: the rows read back,
the id handed out, the flag the report shows. Never the absence of an error.

Written blind from docs/RELEASE_0.1.1.md, docs/SPEC.md §8, docs/DECISIONS.md
and the published surface (README.md, docs/books/, examples/quickstart.py).
Nothing under src/auradefi was read; every expected id is derived from the
formula pinned in DECISIONS.md, never by calling the code under test.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auradefi import Auradefi
from auradefi.api.app import create_app
from auradefi.api.deps import Deps
from auradefi.chains.registry import ChainRegistry
from auradefi.clock import FrozenClock
from auradefi.config import Settings
from auradefi.embed.state import MemorySyncState
from auradefi.errors import ConflictError, SourceError, UnknownChainError, ValidationError
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.ledger.models import (
    Direction, Entry, LedgerTransaction, SyncEventKind, transaction_id)
from auradefi.ledger.reorg import plan_reorg
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

REPO = Path(__file__).resolve().parents[2]
CHAIN_A, CHAIN_B, UNSEEDED = "eip155:1", "eip155:137", "eip155:42161"
ETH = "eip155:1/slip44:60"
COUNTERPARTY = "0x" + "99" * 20
T0, MINUTE_MS = 1_754_000_000_000, 60_000

# DECISIONS.md, "Deterministic tenancy ids", re-derived here so no expectation
# comes from the code under test:
#   end_user_id   = "usr_"  + sha256(f"{project_id}|{external_user_id}")[:16]
#   connection_id = "conn_" + sha256(f"{project_id}|{end_user_id}|{kind}|{descriptor}")[:16]
GOLDEN_DEFAULT_TENANT = "usr_84191de22278db9b"  # project "embed", user "u-1"
GOLDEN_CHAINLESS_CONNECTION = "conn_7c779ab9b4503199"  # 0x22*20 for gate-user-26


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def tenant_of(project_id: str, external_user_id: str) -> str:
    return "usr_" + _sha16(f"{project_id}|{external_user_id}")


def chainless_connection_of(project_id: str, tenant: str, address: str) -> str:
    return "conn_" + _sha16(f"{project_id}|{tenant}|address|{address.lower()}")


def _hash(seed: int) -> str:
    return "0x" + f"{seed:064x}"


def _facade(ledger, source, clock, *, settings=None, state=None, page_size=2):
    """Bind the facade as README.md documents, optionally over a host
    SyncStatePort — whose constructor keyword no published document names, so a
    small set of spellings is probed."""
    settings = settings if settings is not None else Settings(sync_min_interval_s=60)
    if state is None:
        return Auradefi(ledger, source, HostPrices(), clock, settings, sync_page_size=page_size)
    for spelling in ("sync_state", "state", "sync_state_port", "sync_states"):
        try:
            return Auradefi(ledger, source, HostPrices(), clock, settings,
                            sync_page_size=page_size, **{spelling: state})
        except TypeError:
            continue
    pytest.fail("Auradefi must let a host bind its own SyncStatePort (docs/books/09_embedding)")


def _live(ledger, tenant: str) -> dict:
    """Every non-removed transaction the tenant can read back, keyed by tx hash."""
    seen, cursor = {}, None
    while True:
        page = ledger.sync(tenant, cursor=cursor, limit=100)
        for event in page.events:
            seen[event.transaction.id] = event.transaction
        cursor = page.next_cursor
        if not page.has_more:
            break
    return {txn.tx_hash: txn for txn in seen.values() if not txn.removed}


def _reported_failure(report) -> str | None:
    """RELEASE_0.1.1 §5 #24 requires the report to say a connection failed but
    does not name the field, so any public truthy attribute that speaks of
    failure counts as the report being honest."""
    words = ("fail", "error", "partial", "skip", "degrad")
    for holder in (report, *tuple(getattr(report, "connections", ()) or ())):
        for name in dir(holder):
            if name.startswith("_") or not any(word in name for word in words):
                continue
            value = getattr(holder, name, None)
            if not callable(value) and value:
                return f"{type(holder).__name__}.{name}={value!r}"
    return None


class HostSource:
    """The host's transport seam (SPEC §8), bound as quickstart.py binds it.
    Rows are answered the way the upstream answers them: filtered to
    [start_block, end_block], ordered by `sort`, then sliced by (page, offset),
    so any correct windowing strategy can drain them."""

    def __init__(self, rows: dict, failing: tuple = ()) -> None:
        # A key is either an address (rows answered on EVERY chain, the
        # single-chain shape) or a (chain_id, address) pair (rows answered on
        # that chain only). The pair form exists because an upstream is
        # per-chain: without it a fixture cannot express "this address has
        # DIFFERENT history on mainnet and polygon", and a test claiming to
        # pin chain independence would silently serve both chains the same
        # rows — which is not a scenario the product can ever be in.
        self._rows: dict[tuple[str | None, str], tuple] = {}
        for key, pairs in rows.items():
            if isinstance(key, tuple):
                chain_id, address = key
                self._rows[(str(chain_id), address.lower())] = tuple(pairs)
            else:
                self._rows[(None, key.lower())] = tuple(pairs)
        self._failing = {address.lower() for address in failing}
        self.armed = False

    def _pairs_for(self, chain_id: str, address: str) -> tuple:
        key = address.lower()
        if (str(chain_id), key) in self._rows:
            return self._rows[(str(chain_id), key)]
        return self._rows.get((None, key), ())

    def balances(self, chain_id: str, address: str) -> list:
        return [BalanceRecord(caip19=ETH, symbol="ETH",
                              quantity=Quantity(10**18, 18), contract_address=None)]

    def fetch_txlist(self, chain_id, address, *, start_block, end_block, page, offset, sort):
        key = address.lower()
        if self.armed and key in self._failing:
            raise SourceError(f"upstream unavailable for {address}")
        index = max(int(page), 1)
        window = sorted(
            (
                pair
                for pair in self._pairs_for(chain_id, address)
                if start_block <= pair[0] <= end_block
            ),
            reverse=str(sort).lower().endswith("desc"),
        )
        return [self._wire(address, block, tx_hash)
                for block, tx_hash in window[(index - 1) * offset:index * offset]]

    @staticmethod
    def _wire(address: str, block: int, tx_hash: str) -> dict:
        return {"hash": tx_hash, "blockNumber": str(block),
                "timeStamp": str(1_700_000_000 + block * 60), "from": COUNTERPARTY,
                "to": address, "value": "1000000000000000000", "gasUsed": "21000",
                "gasPrice": "10000000000", "isError": "0"}


class HostPrices:
    def usd_prices(self, caip19s) -> dict:
        return {caip19: Money(Decimal("2500"), "USD") for caip19 in caip19s}


def test_19_facade_and_api_address_the_same_ledger_tenant():
    # pins: rows ingested by the embed facade configured for project X are the
    #       rows GET /crypto/sync returns for a user token of project X — one
    #       derivation, so a library write is an HTTP read.
    clock, tenancy = FrozenClock(T0), TenancyStore()
    ledger, keys = MemoryLedger(), ApiKeyStore()
    organisation = tenancy.create_organisation("acme", clock)
    project = tenancy.create_project(organisation.id, "main", Environment.LIVE, clock)
    client = TestClient(create_app(Deps(
        tenancy=tenancy, keys=keys,
        quota=QuotaCounter(QuotaLimits(1_000, 10_000, 100_000), clock),
        audit=AuditLog(), revocations=RevocationSet(), ledger=ledger,
        webhooks=WebhookStore(), chains=ChainRegistry(), clock=clock,
        signing_secret_for={project.id: project.signing_secret}.get,
        capabilities={CHAIN_A: frozenset({"balances", "transactions", "prices"})},
    )))
    _, plaintext = keys.issue(project.id, Environment.LIVE, (Scope.USERS_ADMIN,
                              Scope.ACCOUNTS_READ, Scope.ACCOUNTS_WRITE), clock)
    minted = client.post("/auth/token", json={"external_user_id": "gate-user-19"},
                         headers={"Authorization": f"Bearer {plaintext}"})
    assert minted.status_code == 200, minted.text
    token_headers = {"Authorization": f"Bearer {minted.json()['token']}"}

    address = "0x" + "19" * 20
    rows = ((900, _hash(0x191)), (901, _hash(0x192)), (902, _hash(0x193)))
    facade = _facade(ledger, HostSource({address: rows}), clock, page_size=10,
                     settings=Settings(sync_min_interval_s=60, project_id=project.id))
    user = facade.user("gate-user-19")
    expected_tenant = tenant_of(project.id, "gate-user-19")
    assert user.tenant_id == expected_tenant, (
        f"the facade keys the ledger by {user.tenant_id!r}, project {expected_tenant!r}")
    user.connect_address(CHAIN_A, address)
    facade.sync(budget=8)

    page = client.get("/crypto/sync?limit=100", headers=token_headers)
    assert page.status_code == 200, page.text
    added = {row["transaction_id"] for row in page.json()["added"]}
    ingested = {txn.id for txn in _live(ledger, expected_tenant).values()}
    assert len(ingested) == 3, f"the facade ingested {len(ingested)} rows, not 3"
    assert added == ingested, (
        f"GET /crypto/sync returned {sorted(added)}; the library wrote {sorted(ingested)}")


def test_19_default_project_id_still_derives_the_0_1_0_tenant():
    # pins: with no project_id configured the facade still derives the shipped
    #       0.1.0 tenant id, so 0.1.0 library data stays addressable in 0.1.1.
    assert tenant_of("embed", "u-1") == GOLDEN_DEFAULT_TENANT
    facade = _facade(MemoryLedger(), HostSource({}), FrozenClock(T0))
    assert facade.user("u-1").tenant_id == GOLDEN_DEFAULT_TENANT, (
        "the default derivation moved: 0.1.0 rows are no longer addressable")


def test_26_same_address_on_two_chains_is_two_connections():
    # pins: a connection id is chain-scoped, so one address connects on two
    #       chains, gets two distinct ids, and the two carry independent cursors.
    clock, ledger, address = FrozenClock(T0), MemoryLedger(), "0x" + "22" * 20
    tenant = tenant_of("embed", "gate-user-26")
    assert chainless_connection_of("embed", tenant, address) == GOLDEN_CHAINLESS_CONNECTION
    mainnet_rows = ((500, _hash(0x261)), (501, _hash(0x262)), (502, _hash(0x263)))
    polygon_rows = ((100, _hash(0x264)), (101, _hash(0x265)), (102, _hash(0x266)))
    # Keyed PER CHAIN. Keyed by address alone, both connections would fetch
    # all six rows — the mainnet sync would ingest every hash first, and the
    # polygon sync would then re-present the same hashes and be deduplicated
    # against rows already stamped with the mainnet connection id. The test
    # would fail with six mainnet-owned rows and read as "#26 is unfixed",
    # when what it had actually built was an address with identical history
    # on two chains, which no upstream can produce.
    source = HostSource({(CHAIN_A, address): mainnet_rows,
                         (CHAIN_B, address): polygon_rows})
    facade = _facade(ledger, source, clock, page_size=10)
    user = facade.user("gate-user-26")
    mainnet = user.connect_address(CHAIN_A, address)
    try:
        polygon = user.connect_address(CHAIN_B, address)
    except ConflictError as exc:
        pytest.fail(f"{CHAIN_B} was refused as a duplicate of "
                    f"{getattr(exc, 'existing_id', None)!r}: {exc}")
    assert mainnet.id != polygon.id, f"both chains share the id {mainnet.id!r}"
    assert mainnet.id != GOLDEN_CHAINLESS_CONNECTION, (
        f"{mainnet.id!r} is still the chainless hash of (project, tenant, kind, address)")
    assert {mainnet.id[:5], polygon.id[:5], str(len(mainnet.id))} == {"conn_", "21"}

    report = facade.sync(budget=12)
    landed = _live(ledger, tenant)
    assert set(landed) == {h for _, h in mainnet_rows + polygon_rows}, (
        f"only {sorted(landed)} of the six transactions across both chains landed")
    owners = [txn.account_id for txn in landed.values()]
    assert sorted(owners) == sorted([mainnet.id] * 3 + [polygon.id] * 3), owners
    cursors = sorted(row.live_cursor for row in report.connections)
    assert cursors == [102, 502], f"the two chains did not keep independent cursors: {cursors}"


def test_26_decisions_records_the_chain_scoped_connection_id():
    # pins: the breaking derivation change is written down where the project
    #       pins its algorithms, including that 0.1.0 ids do not carry over.
    lines = (REPO / "docs" / "DECISIONS.md").read_text(encoding="utf-8").lower().splitlines()
    assert any("embed" in line and "chain" in line and "connection" in line for line in lines), (
        "docs/DECISIONS.md does not record that embed connection ids are chain-scoped")
    assert any("0.1.0" in line and "portab" in line for line in lines), (
        "docs/DECISIONS.md does not record that 0.1.0 connection ids are not portable")


def test_21_a_restarted_worker_syncs_from_the_state_port():
    # pins: a fresh Auradefi bound over an existing SyncStatePort enumerates the
    #       tenants that port holds, so sync() does the stored connection's work
    #       instead of returning a success-shaped no_op.
    clock = FrozenClock(T0)
    ledger, state = MemoryLedger(), MemorySyncState()
    address = "0x" + "21" * 20
    rows = ((700, _hash(0x211)), (701, _hash(0x212)), (702, _hash(0x213)))
    source = HostSource({address: rows})
    tenant = tenant_of("embed", "gate-user-21")

    first = _facade(ledger, source, clock, state=state, page_size=10)
    connection = first.user("gate-user-21").connect_address(CHAIN_A, address)
    assert _live(ledger, tenant) == {}, "nothing should be ingested before the first sync"

    del first  # the worker restarts; the host rebinds over its own durable state
    clock.advance(MINUTE_MS)
    restarted = _facade(ledger, source, clock, state=state, page_size=10)
    report = restarted.sync(budget=8)

    assert report.no_op is False, "sync() no_op'd after a restart with stored work waiting"
    landed = _live(ledger, tenant)
    assert set(landed) == {tx_hash for _, tx_hash in rows}, (
        f"the restarted worker ingested {sorted(landed)}, not the three stored transactions")
    assert {txn.account_id for txn in landed.values()} == {connection.id}


def test_24_unseeded_chain_is_refused_at_connect_time():
    # pins: an address on a chain the ChainRegistry does not seed is refused at
    #       connect, so no connection can exist that every later sync() fails on.
    clock, ledger, address = FrozenClock(T0), MemoryLedger(), "0x" + "24" * 20
    rows = ((300, _hash(0x241)), (301, _hash(0x242)))
    facade = _facade(ledger, HostSource({address: rows}), clock, page_size=10)
    user = facade.user("gate-user-24")
    with pytest.raises((UnknownChainError, ValidationError)) as caught:
        user.connect_address(UNSEEDED, address)
    assert UNSEEDED in str(caught.value), f"the refusal omits the chain: {caught.value}"

    user.connect_address(CHAIN_A, address)
    report = facade.sync(budget=8)
    assert report.no_op is False
    assert set(_live(ledger, tenant_of("embed", "gate-user-24"))) == {h for _, h in rows}, (
        "the refused chain left the sync loop unable to serve a seeded one")


def test_24_one_failing_connection_does_not_starve_its_siblings():
    # pins: a connection whose source fails mid-sync costs only itself — its
    #       siblings still ingest, and the report does not claim clean success.
    clock, ledger = FrozenClock(T0), MemoryLedger()
    healthy, broken = "0x" + "2a" * 20, "0x" + "2b" * 20
    healthy_rows = ((400, _hash(0x2A1)), (401, _hash(0x2A2)), (402, _hash(0x2A3)))
    source = HostSource({healthy: healthy_rows, broken: ((400, _hash(0x2B1)),)}, (broken,))
    facade = _facade(ledger, source, clock, page_size=10)
    user = facade.user("gate-user-24b")
    good = user.connect_address(CHAIN_A, healthy)
    user.connect_address(CHAIN_A, broken)
    source.armed = True  # the upstream goes down for one address only
    clock.advance(MINUTE_MS)

    try:
        report = facade.sync(budget=8)
    except SourceError as exc:
        pytest.fail(f"one failing connection aborted the whole sync loop: {exc}")
    landed = _live(ledger, tenant_of("embed", "gate-user-24b"))
    assert set(landed) == {tx_hash for _, tx_hash in healthy_rows}, (
        f"the healthy connection was starved by its sibling: got {sorted(landed)}")
    assert {txn.account_id for txn in landed.values()} == {good.id}
    assert _reported_failure(report) is not None, f"partial failure reported clean: {report}"


def test_18_a_page_boundary_inside_a_block_loses_no_transaction():
    # pins: a backfill page that ends inside a block still fetches the rest of
    #       that block, and backfill_complete is True only once every
    #       transaction is in the ledger.
    clock, ledger, address = FrozenClock(T0), MemoryLedger(), "0x" + "18" * 20
    rows = ((106, _hash(0x181)), (105, _hash(0x182)),
            (104, _hash(0x183)), (104, _hash(0x184)), (104, _hash(0x185)))
    facade = _facade(ledger, HostSource({address: rows}), clock, page_size=2)
    user = facade.user("gate-user-18")
    user.connect_address(CHAIN_A, address)
    tenant, expected = tenant_of("embed", "gate-user-18"), {h for _, h in rows}

    at_completion = None
    for _ in range(10):
        clock.advance(MINUTE_MS)
        report = facade.sync(budget=4)
        if any(row.backfill_complete for row in report.connections):
            at_completion = set(_live(ledger, tenant))
            break
    assert at_completion is not None, "backfill never reported itself complete"
    missing = expected - at_completion
    assert not missing, f"backfill_complete went True, never fetching {sorted(missing)}"
    landed = _live(ledger, tenant)
    assert set(landed) == expected
    assert len(landed) == 5, f"an inclusive boundary duplicated rows: {len(landed)} of 5"


def test_22_a_removed_transaction_returning_unchanged_is_re_added():
    # pins: a transaction orphaned by an earlier reorg that is canonical again
    #       with a byte-identical payload is re-added, not left removed forever.
    ledger, tenant = MemoryLedger(), "usr_00000000000000ff"

    def make(tx_hash: str, block: int) -> LedgerTransaction:
        return LedgerTransaction(
            id=transaction_id(CHAIN_A, tx_hash, "acct_22"), chain_id=CHAIN_A,
            tx_hash=tx_hash, account_id="acct_22", block_number=block,
            initiated_at=1_700_000_000_000, confirmed_at=1_700_000_012_000,
            entries=(Entry(asset_id=ETH, quantity=Quantity(10**18, 18), direction=Direction.IN),))

    txns = [make(_hash(0x220 + index), 100 + index) for index in range(4)]
    ledger.upsert(tenant, txns)
    orphan = txns[3]  # block 103, orphaned by an earlier reorg
    ledger.mark_removed(tenant, [orphan.id])
    assert ledger.get(tenant, orphan.id).removed is True

    stored = [ledger.get(tenant, txn.id) for txn in txns]
    plan = plan_reorg(stored, list(txns), from_block=103)  # unchanged, back on chain
    assert orphan.id not in plan.remove_ids
    assert [txn.id for txn in plan.add] == [orphan.id], (
        f"a removed row returning unchanged planned add={[txn.id for txn in plan.add]}")

    events = ledger.apply_reorg(tenant, plan)
    assert [event.kind for event in events] == [SyncEventKind.ADDED]
    assert ledger.get(tenant, orphan.id).removed is False, "still flagged removed to clients"
    assert orphan.tx_hash in _live(ledger, tenant)


def test_no_pre_existing_regression_test_was_deleted_or_weakened():
    # pins: 0.1.1 adds tests and retires none — the two tests §5 #22 names and
    #       the end_user_id half of the embed/tenancy cross-pin all survive.
    expected = {
        "tests/contract/test_phase3_reorg.py": ("test_gate_resurrection_re_add_of_c",),
        "tests/ledger/test_reorg.py": ("test_bookkeeping_only_difference_is_not_readded",),
        "tests/golden/test_embed_ids.py": ("end_user_id",),
    }
    for relative, needles in expected.items():
        path = REPO / relative
        assert path.is_file(), f"{relative} was deleted"
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle in text, f"{relative} no longer carries {needle!r}"
