"""How do I run this inside my own Python backend, with my own database?

    pip install auradefi && python 02_embed_in_your_backend.py

This is the shape most hosts want: `from auradefi import Auradefi`, no HTTP
hop, no separate service, no serialisation cost. Everything the library
would otherwise decide for you is a port you hand it — storage, sync state,
transport, prices, and the clock — so the answers are reproducible and the
library never opens a connection you did not give it.

What this file demonstrates, in order:

1. binding the ports (zero I/O happens in the constructor);
2. validation at CONNECT time, not on a background tick hours later;
3. a *budgeted* sync you call on your own schedule, and its throttle;
4. a restart: a new process, the same durable state, work resumes;
5. one dead chain failing on its own row instead of failing the tick;
6. reading holdings and the 26 scalar metrics back out.

`MemoryLedger` and `MemorySyncState` keep this file standalone; swap in
`SqlModelLedger` and your own `SyncStatePort` and nothing above changes —
see `04_persist_to_your_database.py`.
"""

from __future__ import annotations

from decimal import Decimal

from auradefi import Auradefi
from auradefi.clock import FrozenClock
from auradefi.config import Settings
from auradefi.embed.state import MemorySyncState
from auradefi.errors import SourceError, UnknownChainError, ValidationError
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.sources.evm.etherscan import BalanceRecord

ETH = "eip155:1/slip44:60"
WALLET = "0x1111111111111111111111111111111111111111"
COUNTERPARTY = "0x9999999999999999999999999999999999999999"


class HostSource:
    """Your transport. One object, two seams — the library asks for both.

    `balances(chain_id, address)` answers holdings questions;
    `fetch_txlist(...)` answers history questions and is paged by the
    engine. Put your RPC, your vendor SDK or your queue behind these two
    methods; see `03_write_a_source_adapter.py` for a real HTTP one.
    """

    def __init__(self, dead_chains: frozenset[str] = frozenset()) -> None:
        self.dead_chains = dead_chains
        self.requests: list[tuple[str, int]] = []

    def balances(self, chain_id: str, address: str) -> list[BalanceRecord]:
        return [BalanceRecord(caip19=ETH, symbol="ETH",
                              quantity=Quantity(3 * 10**18, 18), contract_address=None)]

    def fetch_txlist(self, chain_id, address, *, start_block, end_block,
                     page, offset, sort) -> list[dict]:
        self.requests.append((chain_id, page))
        if chain_id in self.dead_chains:
            # Raise `SourceError` (or any `AuradefiError`) for an upstream
            # failure and the library contains it to this one connection.
            # Anything else — a KeyError in your adapter — propagates
            # untouched, because that is your bug and not an outage.
            raise SourceError(f"{chain_id} RPC did not answer")
        if offset == 1:
            return [self._row(0)]      # the connect-time liveness probe
        if page > 1 or start_block > 0:
            return []                  # four transactions, then a short page
        return [self._row(index) for index in range(4)]

    @staticmethod
    def _row(index: int) -> dict:
        """One raw Etherscan-shaped row. The decoder seam owns the format."""
        return {
            "hash": "0x" + f"{index + 1:02x}" * 32,
            "blockNumber": str(18_000_000 + index),
            "timeStamp": str(1_753_000_000 + index * 3_600),
            "from": COUNTERPARTY, "to": WALLET,
            "value": str(10**18), "gasUsed": "21000",
            "gasPrice": "10000000000", "isError": "0",
        }


class HostPrices:
    """Your price feed: CAIP-19 ids in, `Money` out. Absent is allowed."""

    def usd_prices(self, caip19s):
        return {ETH: Money(Decimal("3000"), "USD")}


# ------------------------------------------------------- 1. bind the ports
ledger = MemoryLedger()          # your database
state = MemorySyncState()        # your cursor store — it OUTLIVES the facade
source = HostSource()
clock = FrozenClock(1_754_000_000_000)

auradefi = Auradefi(
    ledger=ledger,
    source=source,
    prices=HostPrices(),
    clock=clock,                                   # None -> SystemClock
    settings=Settings(sync_min_interval_s=60),     # your throttle floor
    sync_state=state,
    sync_page_size=100,
)
assert source.requests == [], "binding ports performs NO I/O"
print("bound: ledger, sync_state, source, prices, clock — zero requests so far")

# A port that does not satisfy the seams is refused HERE, not on tick one.
try:
    Auradefi(ledger, object(), HostPrices(), clock)
except ValidationError as exc:
    print(f"  bad source refused at bind time: {exc}")

# --------------------------------------------- 2. validate at connect time
user = auradefi.user("your-opaque-user-id")     # get-or-create; id is derived
mainnet = user.connect_address("eip155:1", WALLET)
polygon = user.connect_address("eip155:137", WALLET)

# The same address on two chains is two connections, and the ids say so.
assert mainnet.id != polygon.id and mainnet.id.startswith("conn_")
print(f"\nconnected {mainnet.id} (eip155:1)")
print(f"          {polygon.id} (eip155:137)")

for chain, address, expected in (("eip155:99999", WALLET, UnknownChainError),
                                 ("eip155:1", "0xnope", ValidationError)):
    try:
        user.connect_address(chain, address)
    except expected as exc:
        print(f"  refused now, not later: {type(exc).__name__} — {exc}")

# --------------------------------------------------- 3. sync, on your tick
# `budget` is the maximum number of source pages this call may spend. It is
# how a host bounds a tick; the cursor makes the next call resume.
report = auradefi.sync(budget=4)
assert report.no_op is False
assert report.transactions_ingested == 8          # 4 transactions x 2 chains
print(f"\nsync(budget=4): {report.pages_fetched} pages "
      f"({report.live_pages} live + {report.backfill_pages} backfill), "
      f"{report.transactions_ingested} transactions")
for row in report.connections:
    print(f"  {row.connection_id}  live_cursor={row.live_cursor} "
          f"backfill_complete={row.backfill_complete} failed={row.failed}")

requests_so_far = len(source.requests)
throttled = auradefi.sync(budget=4)               # again, immediately
assert throttled.no_op is True
assert len(source.requests) == requests_so_far, "a no-op must not touch the transport"
print(f"  immediate re-sync: no_op={throttled.no_op}, "
      f"{len(source.requests) - requests_so_far} extra requests")

# ------------------------------------------------------- 4. restart resume
# A new process binds fresh objects and the SAME state port. Connections
# come from that port, so a restarted worker resumes stored work instead of
# reporting a cheerful no-op over an empty in-process list.
clock.advance(120_000)
restarted = Auradefi(ledger, source, HostPrices(), clock,
                     Settings(sync_min_interval_s=60), sync_state=state,
                     sync_page_size=100)
assert state.tenants() == (user.tenant_id,)
after_restart = restarted.sync(budget=4)
assert [row.connection_id for row in after_restart.connections] == [mainnet.id, polygon.id]
assert after_restart.transactions_ingested == 0, "already-seen rows do not double-write"
print(f"\nafter restart: enumerated {len(after_restart.connections)} stored connection(s) "
      f"from the state port; {after_restart.transactions_ingested} new transactions "
      "(the upsert is idempotent)")

# ------------------------------------------------ 5. one failure, one row
# Polygon's RPC goes dark. The tick must not be lost, and the failure must
# not be reported as success.
clock.advance(120_000)
flaky = Auradefi(ledger, HostSource(dead_chains=frozenset({"eip155:137"})),
                 HostPrices(), clock, Settings(sync_min_interval_s=60),
                 sync_state=state, sync_page_size=100)
degraded = flaky.sync(budget=4)
# `failed_connections` is derived from the per-connection rows, so a partial
# failure can never hide behind an aggregate that reads like a clean tick.
assert degraded.failed_connections == (polygon.id,)
assert all(row.failed is False for row in degraded.connections
           if row.connection_id == mainnet.id)
print(f"\npolygon RPC dark: failed_connections={degraded.failed_connections}, "
      f"mainnet still synced — branch on this every tick")

# ---------------------------------------------------- 6. read it back out
reports = auradefi.holdings()                     # one report per connection
assert [report.total_value.amount for report in reports] == [Decimal("9000")] * 2
print(f"\nholdings: {len(reports)} connection(s) — "
      + ", ".join(f"{report.chain_id} {report.total_value}" for report in reports))

# 26 metrics PER CONNECTION, concatenated in connection order: a portfolio
# value, a transaction count, and 24 hourly buckets. Each connection's
# transactions are the non-removed ledger rows carrying ITS account id, so
# two chains never get merged into one number.
metrics = auradefi.scalar_metrics()
assert len(metrics) == 26 * len(reports)
mainnet_metrics = {metric.name: metric.value for metric in metrics[:26]}
assert mainnet_metrics["transaction_count"] == 4.0
busiest = max((metric for metric in metrics[:26] if metric.name.startswith("tx_count_hour")),
              key=lambda metric: metric.value)
print(f"  {len(metrics)} metrics ({len(metrics) // len(reports)} per connection): "
      f"portfolio_value_usd={mainnet_metrics['portfolio_value_usd']}, "
      f"transaction_count={mainnet_metrics['transaction_count']}, "
      f"busiest hour {busiest.name[-2:]}:00 UTC with {busiest.value}")

print("\nOK — one process, your ports, your tick, your database.")
