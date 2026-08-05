"""auradefi in five lines, then the whole library in one file.

    pip install auradefi
    python examples/quickstart.py

No keys. No network. No configuration. `Auradefi.sandbox()` replays a
recording bundled inside the package, and every layer above the transport is
the production one: the same source, decoder, ledger and pricing a live
instance uses. Sandbox data is a RECORDING, so the numbers here are
constants, which is what makes them safe to assert.

This file is also the smoke test CI, `scripts/release_check.sh` (against a
freshly built wheel in a clean venv) and `docker run --network none` all
execute, so nothing in it may depend on the repository.

Each section maps to one SPEC phase, and to a guide that goes deeper:

    the five lines                      examples/01_holdings_for_an_address.py
    0  money, chains, assets, ledger    docs/books/01_foundation … 04_ledger
    1  balances -> holdings             examples/01, examples/04
    2  tenancy and the token mint       examples/06
    3  transaction decode and reorg     examples/04
    4  DeFi positions                   examples/07
    5  embedding in your backend        examples/02, examples/03
    6  Bitcoin xpub derivation          examples/10
    7  Solana Token-2022                examples/10
    8  webhook signing                  examples/09
    9  cost basis and PnL               examples/08
"""

from __future__ import annotations

import json
from decimal import Decimal

import auradefi

print(f"auradefi {auradefi.__version__}: sandbox quickstart, no keys\n")


def section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 62 - len(title)))


# ===================================================== the whole ask, first
section("a priced portfolio, in five lines")

from auradefi import Auradefi

aura = Auradefi.sandbox()
for holding in aura.holdings()[0].holdings:
    print(f"  {holding.symbol:>5} {str(holding.quantity):>4} @ {holding.price}"
          f" = {holding.value}")

(sandbox_report,) = aura.holdings()
assert str(sandbox_report.total_value) == "5025.000000000000000000 USD"
print(f"  total {sandbox_report.total_value}")
print("  ^ that is the entire program. Everything below is detail.")


# --------------------------------------------------------------- phase 0
section("phase 0: money is exact, and a raw amount is a string")

from auradefi.money.decimal_json import quantity_to_wire
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

huge = Quantity(10**77, 18)
assert str(huge) == "1" + "0" * 59  # exact, never scientific notation
wire = quantity_to_wire(Quantity(4878123456789012345678, 18))
assert isinstance(wire["raw"], str), "rule #2: raw is never a JSON number"
assert wire["numeric"] == "4878.123456789012345678"
print(f"10^77 at 18 decimals -> {str(huge)[:20]}… ({len(str(huge))} digits, exact)")
print(f"wire form: {json.dumps(wire)}")

from auradefi.assets.caip import canonical_caip19, parse_caip19
from auradefi.chains.registry import ChainRegistry

chains = ChainRegistry()
assert [chain.caip2 for chain in chains.chains()][:2] == [
    "bip122:000000000019d6689c085ae165831e93",
    "eip155:1",
]
mixed = "eip155:1/erc20:0xA0b86991c6218b36c1D19D4a2e9Eb0cE3606eB48"
assert canonical_caip19(mixed) == mixed.lower()
assert parse_caip19(mixed).namespace == "erc20"
print(f"{len(chains.chains())} chains seeded; CAIP-19 canonicalised: {canonical_caip19(mixed)}")


# --------------------------------------------------------------- phase 1
section("phase 1: balances + prices -> holdings, exactly")

from auradefi.money.decimal_json import money_to_wire
from auradefi.money.fiat import Money

# The five lines above already did this. What matters is HOW the number is
# built: exact `Decimal` throughout, and an asset nobody prices is named in
# `report.unpriced` rather than valued at zero.
for holding in sandbox_report.holdings:
    print(f"  {holding.symbol:>5} {str(holding.quantity):>4} @ "
          f"{str(holding.price):>9} = {holding.value}")
assert sandbox_report.total_value == Money(Decimal("5025"), "USD")
assert sandbox_report.unpriced == ()
print(f"  total {sandbox_report.total_value} (exact Decimal, never a float)")
print(f"  on the wire: {json.dumps(money_to_wire(sandbox_report.total_value))}")

# The offline guarantee is a guarantee: an unrecorded request fails loudly
# rather than reaching the network.
from auradefi.errors import CassetteMissError
from auradefi.sources import sandbox as recording

try:
    recording.client().get("https://api.etherscan.io/v2/api?chainid=999")
except CassetteMissError:
    print("  an unrecorded request is refused: sandbox cannot reach the network")


# --------------------------------------------------------------- phase 2
section("phase 2: two tenants, and one cannot see the other")

from auradefi.clock import FrozenClock
from auradefi.errors import AuthError
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.keys import ApiKeyStore
from auradefi.tenancy.models import Environment, Scope
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import verify_token

clock = FrozenClock(1_767_225_600_000)
tenancy = TenancyStore()
org = tenancy.create_organisation("Acme", clock)
project_a = tenancy.create_project(org.id, "tenant-a", Environment.LIVE, clock)
project_b = tenancy.create_project(org.id, "tenant-b", Environment.LIVE, clock)

key, plaintext = ApiKeyStore().issue(
    project_a.id, Environment.LIVE, (Scope.USERS_ADMIN,), clock
)
assert plaintext.startswith("adk_live_") and len(plaintext) == 57

token = tenancy.mint_user_token(
    project_a.id, "host-user-1", ["accounts:read"], 600_000,
    "203.0.113.7", key.id, clock, AuditLog(),
)
claims = verify_token(token, signing_secret=project_a.signing_secret, clock=clock)
assert claims.project_id == project_a.id
try:
    verify_token(token, signing_secret=project_b.signing_secret, clock=clock)
except AuthError as exc:
    print(f"  A's token under B's secret: {type(exc).__name__}: {exc}")
print(f"  minted {plaintext[:13]}… -> user token for {claims.external_user_id}, "
      f"scopes {claims.scopes}, ttl {(claims.exp - claims.iat) // 1000}s")


# --------------------------------------------------------------- phase 3
section("phase 3: decode -> parts/fees, bridge -> ledger, reorg")

from auradefi.decode.pipeline import decode_account
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.ledger.bridge import to_ledger_transaction
from auradefi.ledger.models import SyncEventKind
from auradefi.ledger.reorg import plan_reorg
from auradefi.sources.evm.txlist import NormalTxRecord

ME = "0x" + "11" * 20


def row(tx_hash: str, block: int, seconds: int) -> NormalTxRecord:
    return NormalTxRecord(
        tx_hash=tx_hash, block_number=block, time_stamp=seconds,
        from_address="0x" + "99" * 20, to_address=ME, value_wei=10**18,
        gas_used=21_000, gas_price_wei=10**10, is_error=False,
    )


HASH_A, HASH_B = "0x" + "aa" * 32, "0x" + "bb" * 32
rich = decode_account("eip155:1", "acct_1", ME, [row(HASH_A, 100, 1_700_000_000),
                                                 row(HASH_B, 101, 1_700_000_100)], [])
first = rich[0]
assert [part.direction.value for part in first.parts] == ["in"]
assert first.fees[0].borne_by.value == "counterparty"  # the sender paid the gas
assert to_ledger_transaction(first).entries[0].quantity == Quantity(10**18, 18)
print(f"  {first.id}: type={first.type.value} parts={len(first.parts)} "
      f"fees={len(first.fees)} (fee borne_by={first.fees[0].borne_by.value})")

ledger = MemoryLedger()
bridged = [to_ledger_transaction(txn) for txn in rich]
ledger.upsert("tenant-a", bridged)
page = ledger.sync("tenant-a", None)
assert page.next_cursor == "00000000000000000002" and page.has_more is False

reorged = to_ledger_transaction(decode_account(
    "eip155:1", "acct_1", ME, [row(HASH_B, 105, 1_700_000_500)], []
)[0])
events = ledger.apply_reorg("tenant-a", plan_reorg(
    [ledger.get("tenant-a", txn.id) for txn in bridged], [reorged], from_block=101
))
assert [event.kind for event in events] == [SyncEventKind.ADDED]
delta = ledger.sync("tenant-a", page.next_cursor)
print(f"  reorg at block 101 -> " + ", ".join(
    f"{event.kind.value} {event.transaction.id[:12]}… (block {event.transaction.block_number})"
    for event in delta.events
) + f"; cursor {page.next_cursor} -> {delta.next_cursor}")


# --------------------------------------------------------------- phase 4
section("phase 4: positions drill down, and the projection invariant")

from auradefi.positions.drill import drill, project_to_synthetic_holdings
from auradefi.positions.models import (
    MetaType,
    Position,
    PositionKind,
    PositionType,
    ProtocolModule,
    Underlying,
    group_id_for,
    position_id,
)

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
ETH_ID = "eip155:1/slip44:60"
USDC_ID = f"eip155:1/erc20:{USDC}"
AAVE_POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
AWETH = "0x4d5f47fa6a74757f35c14fd3a6ef8e3c9bc514e8"
group = group_id_for("aave-v3", "eip155:1", AAVE_POOL)

# SPEC §6.3 verbatim: supply 10 ETH, borrow 5,000 USDC. ONE risk unit.
positions = [
    Position(
        id=position_id("aave-v3", "eip155:1", AWETH), adapter_id="aave-v3",
        chain_id="eip155:1", contract_address=AWETH, kind=PositionKind.APP_TOKEN,
        position_type=PositionType.DEPOSIT, protocol_module=ProtocolModule.LENDING,
        group_id=group,
        underlyings=(Underlying(ETH_ID, Quantity(10 * 10**18, 18), MetaType.SUPPLIED),),
    ),
    Position(
        id=position_id("aave-v3", "eip155:1", AAVE_POOL), adapter_id="aave-v3",
        chain_id="eip155:1", contract_address=AAVE_POOL,
        kind=PositionKind.CONTRACT_POSITION, position_type=PositionType.LOAN,
        protocol_module=ProtocolModule.LENDING, group_id=group,
        underlyings=(Underlying(USDC_ID, Quantity(5000 * 10**6, 6), MetaType.BORROWED),),
    ),
]
prices = {ETH_ID: Money(Decimal("3584.17"), "USD"), USDC_ID: Money(Decimal("0.999839"), "USD")}
drilled = drill(positions, prices)
synthetic = project_to_synthetic_holdings(drilled)

assert drilled.net_worth.amount == Decimal("30842.505")
assert {holding.quantity for holding in synthetic} == {Decimal("10"), Decimal("-5000")}
naive_sum = sum((holding.institution_value.amount for holding in synthetic), Decimal("0"))
assert naive_sum == drilled.net_worth.amount  # THE invariant (SPEC §6.3)
SYMBOLS = {ETH_ID: "ETH", USDC_ID: "USDC"}
print(f"  gross {drilled.gross_assets} − debt {drilled.total_debt} = net {drilled.net_worth}")
print("  synthetic holdings: " + ", ".join(
    f"{holding.quantity.normalize():f} {SYMBOLS[holding.asset_id]}" for holding in synthetic))
print(f"  a Plaid-only client summing institution_value gets {naive_sum}: exactly the net worth")


# --------------------------------------------------------------- phase 5
section("phase 5: embedding: your ports, your tick, your database")

# `sandbox()` and `from_env()` differ by one line and nothing else:
#
#     aura = Auradefi.from_env()                    # your Etherscan key
#     aura = Auradefi.from_env(ledger=MyLedger())   # + your database
#
# `sync(budget=N)` caps the source pages ONE call may spend; cursors make
# the next call resume; calling it again inside
# `settings.sync_min_interval_s` is a no-op that touches no transport.
synced = aura.sync(budget=10)
assert (synced.pages_fetched, synced.transactions_ingested) == (5, 7)
assert aura.sync(budget=10).no_op is True
assert synced.failed_connections == ()
print(f"  sync: {synced.pages_fetched} pages, {synced.transactions_ingested} "
      f"transactions across {len(synced.connections)} connection(s)")
print(f"  immediate re-sync: no_op=True, zero requests")
print("  one connection's failure lands in report.failed_connections, never")
print("  in a lost tick: branch on it every time (examples/02)")

metrics = {metric.name: metric.value for metric in aura.scalar_metrics()}
assert len(metrics) == 26
print(f"  26 scalar metrics: portfolio_value_usd={metrics['portfolio_value_usd']}, "
      f"transaction_count={metrics['transaction_count']}")


# --------------------------------------------------------------- phase 6/7
section("phase 6+7: Bitcoin derives locally; Solana can break raw/10^d")

from auradefi.sources.bitcoin.xpub import derive_addresses
from auradefi.sources.solana.spl import (
    aggregate_by_mint,
    build_balances,
    parse_token_accounts,
)

XPUB = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoC"
    "u1Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8"
)
addresses = derive_addresses(XPUB, "p2wpkh", 0, 0, 3)
assert addresses[0] == "bc1qp5wfcq48h6d63wyy9qz0awtpfqwwv4sma86mhz"
print("  BIP32 derived in-process: the extended key never goes near HTTP:")
for index, address in enumerate(addresses):
    print(f"    m/0/{index}  {address}")

T22_MINT = "ScaLedUiAmountMint22222222222222222222222222"
OWNER = "9wFFyRfZBsuAha4YcuxcXLKwMxJR43S7fPfQLXMFxbAF"
# A Token-2022 account whose mint carries a ScaledUiAmount multiplier of 2:
# the node's displayed amount is NOT raw / 10**decimals.
accounts = parse_token_accounts([{
    "pubkey": "T22AcctC3",
    "account": {"data": {"program": "spl-token-2022", "parsed": {"type": "account", "info": {
        "mint": T22_MINT, "owner": OWNER, "state": "initialized",
        "extensions": [{"extension": "scaledUiAmountConfig",
                        "state": {"multiplier": "2"}}],
        "tokenAmount": {"amount": "1000000000", "decimals": 9,
                        "uiAmount": 2.0, "uiAmountString": "2"},
    }}}},
}])
native, scaled = build_balances(3_500_000_000, aggregate_by_mint(accounts))
assert str(native.quantity) == "3.5"
assert str(scaled.quantity) == "1" and scaled.ui_amount_string == "2" and scaled.scaled_ui
print(f"  Token-2022 ScaledUiAmount: raw/10^decimals = {scaled.quantity}, "
      f"node says {scaled.ui_amount_string}, scaled_ui={scaled.scaled_ui}: both carried")


# --------------------------------------------------------------- phase 8
section("phase 8: webhooks are signed, and verification is shipped")

from auradefi.webhooks.sign import sign, verify_signature

secret = "ab" * 32
body = '{"type":"connection.created","data":{"connection_id":"conn_demo"}}'
at_ms = 1_754_000_000_000
signature = sign(secret, at_ms, body)
verify_signature(secret, at_ms, body, signature, at_ms)
try:
    verify_signature(secret, at_ms, body + " ", signature, at_ms)
except AuthError as exc:
    print(f"  tampered body rejected: {exc}")
print(f"  X-Auradefi-Signature: {signature[:34]}…")


# --------------------------------------------------------------- phase 9
section("phase 9: four costing methods, four legal answers")

from auradefi.accounting.lots import AcquisitionEvent, DisposalEvent
from auradefi.accounting.pnl import pnl_at

DAY = 86_400_000
T0 = 1_700_000_000_000
trades = (
    AcquisitionEvent(T0 + 0 * DAY, ETH_ID, Quantity(1, 0), Money(Decimal("10"), "USD"), "txn_b1"),
    AcquisitionEvent(T0 + 1 * DAY, ETH_ID, Quantity(1, 0), Money(Decimal("30"), "USD"), "txn_b2"),
    AcquisitionEvent(T0 + 2 * DAY, ETH_ID, Quantity(1, 0), Money(Decimal("26"), "USD"), "txn_b3"),
    DisposalEvent(T0 + 3 * DAY, ETH_ID, Quantity(1, 0), Money(Decimal("40"), "USD"), "txn_s1"),
)
marks = {ETH_ID: Money(Decimal("50"), "USD")}
realised = {method: pnl_at(trades, method, T0 + 3 * DAY, marks).realized
            for method in ("fifo", "lifo", "hifo", "acb")}
assert [str(value) for value in realised.values()] == ["30 USD", "14 USD", "10 USD", "18 USD"]
print("  bought at 10, 30, 26; sold one unit for 40:")
for method, value in realised.items():
    print(f"    {method:<5} realised {value}")
# Arbitrary date: one millisecond earlier, the sale has not happened yet.
before = pnl_at(trades, "fifo", T0 + 3 * DAY - 1, marks)
assert before.realized == Money(Decimal("0"), "USD") and len(before.open_lots) == 3
print(f"  1 ms before the sale: realised {before.realized}, {len(before.open_lots)} open lots: "
      "any instant is answerable, nothing is pre-computed")


# --------------------------------------------------------- optional extras
section("optional extras (installed only with [sql] / [api])")

try:
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session

    from auradefi.ledger.backends.models import metadata
    from auradefi.ledger.backends.sqlmodel import SqlModelLedger

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    metadata.create_all(engine)  # the HOST's DDL. The library emits none
    sql_ledger = SqlModelLedger(session_factory=lambda: Session(engine))
    sql_ledger.upsert("tenant-a", bridged)
    assert len(sql_ledger.sync("tenant-a", None).events) == 2
    print("  [sql]  SqlModelLedger round-tripped 2 rows through the host's sqlite")
except ImportError:
    print("  [sql]  not installed: skipped (pip install 'auradefi[sql]')")

try:
    from fastapi.testclient import TestClient

    from auradefi.api.app import create_app
    from auradefi.api.deps import Deps
    from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
    from auradefi.tenancy.tokens import RevocationSet
    from auradefi.webhooks.deliver import WebhookStore

    api_deps = Deps(
        tenancy=tenancy, keys=ApiKeyStore(),
        quota=QuotaCounter(QuotaLimits(1_000, 10_000, 100_000), clock),
        audit=AuditLog(), revocations=RevocationSet(), ledger=MemoryLedger(),
        webhooks=WebhookStore(), chains=chains, clock=clock,
        signing_secret_for={project_a.id: project_a.signing_secret}.get,
        capabilities={"eip155:1": frozenset({"balances", "transactions", "prices"})},
    )
    api = TestClient(create_app(api_deps))
    coverage = api.get("/coverage").json()
    ethereum = next(row for row in coverage["chains"] if row["chain_id"] == "eip155:1")
    assert ethereum["capabilities"]["balances"] is True
    assert ethereum["capabilities"]["positions"] is False  # generated, never prose
    print(f"  [api]  GET /coverage: {len(coverage['chains'])} chains, "
          f"eip155:1 -> {sorted(k for k, v in ethereum['capabilities'].items() if v)}")
except ImportError:
    print("  [api]  not installed: skipped (pip install 'auradefi[api]')")

print("\nquickstart OK: nothing above touched the network")
