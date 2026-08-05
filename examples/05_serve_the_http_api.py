"""How do I expose this over HTTP, the way Plaid clients already expect?

    pip install 'auradefi[api]'
    python examples/05_serve_the_http_api.py

`create_app(Deps(...))` returns a FastAPI app. It is a shell: it holds no
state, opens no connections and invents no stores: you inject the same
ports the library uses (`04_persist_to_your_database.py`) and the routes
project them onto Plaid's wire format.

This file drives the app with FastAPI's `TestClient` so it runs without a
server, then shows the one-liner that serves it for real. The journey is
the one a client actually makes:

    POST /auth/token          server key  -> short-lived user token
    POST /connections         user token  -> conn_… (409 names the existing one)
    GET  /crypto/sync         user token  -> added/modified/removed + cursor
    POST /batch/holdings      server key  -> partial success, per-item errors
    GET  /coverage            public      -> generated capability matrix

Ingestion is NOT an HTTP concern: rows arrive in the ledger from your own
worker calling the library (`02_embed_in_your_backend.py`). This file seeds
them directly so the sync feed has something to page.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from auradefi.api.app import create_app
from auradefi.api.deps import Deps
from auradefi.chains.registry import ChainRegistry
from auradefi.clock import FrozenClock
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.ledger.models import Direction, Entry, LedgerTransaction, transaction_id
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.portfolio.models import Holding, HoldingsReport
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.keys import ApiKeyStore
from auradefi.tenancy.models import Environment, Scope, end_user_id
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import RevocationSet
from auradefi.webhooks.deliver import WebhookStore

CHAIN, ETH = "eip155:1", "eip155:1/slip44:60"
ADDRESS = "0xAAAAaaaaAAAAaaaaAAAAaaaaAAAAaaaaAAAAaaaa"
NOW = 1_754_000_000_000


class StubHoldings:
    """Whatever answers `holdings(chain_id, address)`. Bind yours, or
    leave `Deps.holdings=None` and the batch route is not mounted at all.
    An unbound capability has no endpoint rather than a broken one."""

    def holdings(self, chain_id: str, address: str) -> HoldingsReport:
        return HoldingsReport.assemble(address, chain_id, [
            Holding(caip19=ETH, symbol="ETH", quantity=Quantity(2 * 10**18, 18),
                    price=Money(Decimal("3000"), "USD"),
                    value=Money(Decimal("6000"), "USD")),
        ], NOW)


# --------------------------------------------------------- 1. build the app
clock = FrozenClock(NOW)
tenancy = TenancyStore()
organisation = tenancy.create_organisation("acme", clock)
project = tenancy.create_project(organisation.id, "main", Environment.LIVE, clock)
ledger = MemoryLedger()

deps = Deps(
    tenancy=tenancy,
    keys=ApiKeyStore(),
    quota=QuotaCounter(QuotaLimits(1_000, 10_000, 100_000), clock),
    audit=AuditLog(),
    revocations=RevocationSet(),
    ledger=ledger,
    webhooks=WebhookStore(),
    chains=ChainRegistry(),
    clock=clock,
    signing_secret_for={project.id: project.signing_secret}.get,
    holdings=StubHoldings(),
    # `/coverage` is generated from THIS, never from prose. Declare only
    # what you actually wired up.
    capabilities={CHAIN: frozenset({"balances", "transactions", "prices"})},
)
client = TestClient(create_app(deps))
print(f"project {project.id}: routes:")
schema = client.get("/openapi.json").json()
for path in sorted(schema["paths"]):
    for method in sorted(schema["paths"][path]):
        print(f"  {method.upper():<5} {path}")

# ------------------------------------------- 2. server key -> user token
# The server key never leaves your backend. It mints a short-lived token
# scoped to ONE end user, which is what a browser or mobile app may hold.
key, secret = deps.keys.issue(
    project.id, Environment.LIVE,
    (Scope.USERS_ADMIN, Scope.ACCOUNTS_READ, Scope.ACCOUNTS_WRITE), clock,
)
server_auth = {"Authorization": f"Bearer {secret}"}
assert secret.startswith("adk_live_")

minted = client.post("/auth/token", json={"external_user_id": "host-user-7"},
                     headers=server_auth)
assert minted.status_code == 200 and list(minted.json()) == ["token"]
user_auth = {"Authorization": f"Bearer {minted.json()['token']}"}

quota_headers = {name.lower(): value for name, value in minted.headers.items()
                 if name.lower().startswith("x-ratelimit")}
assert len(quota_headers) == 9          # 3 windows x limit/remaining/reset
print(f"\nminted a user token from {secret[:13]}…")
print(f"  quota headers: {len(quota_headers)} "
      f"(second/minute/day x limit/remaining/reset)")
print(f"  audit: {deps.audit.entries(project.id)[0].event} "
      f"by {deps.audit.entries(project.id)[0].key_id} "
      f"from {deps.audit.entries(project.id)[0].ip}")

# ---------------------------------------------------- 3. connect a wallet
created = client.post("/connections", json={"kind": "address", "descriptor": ADDRESS},
                      headers=user_auth)
assert created.status_code == 201
connection_id = created.json()["id"]

# The same wallet again, in different case, is a 409 that NAMES the
# connection you already have, so a retrying client can carry on.
conflict = client.post("/connections",
                       json={"kind": "address", "descriptor": ADDRESS.lower()},
                       headers=user_auth)
assert conflict.status_code == 409
assert conflict.json()["error"]["existing_connection_id"] == connection_id
print(f"\nPOST /connections -> 201 {connection_id}")
print(f"  again -> {conflict.status_code} {conflict.json()['error']['message']}")

# ------------------------------------------------------ 4. the sync feed
# Seeded here; in production your worker wrote these rows.
tenant = end_user_id(project.id, "host-user-7")
ledger.upsert(tenant, [
    LedgerTransaction(
        id=transaction_id(CHAIN, "0x" + f"{index:02x}" * 32, "acct_eth"),
        chain_id=CHAIN, tx_hash="0x" + f"{index:02x}" * 32, account_id="acct_eth",
        block_number=18_000_000 + index,
        initiated_at=NOW - 10_000 + index, confirmed_at=NOW - 9_000 + index,
        entries=(Entry(asset_id=ETH, quantity=Quantity(index * 10**17, 18),
                       direction=Direction.IN),),
    ) for index in (1, 2, 3)
])

seen, cursor, pages = [], None, 0
while True:
    query = "/crypto/sync?limit=2" + (f"&cursor={cursor}" if cursor else "")
    page = client.get(query, headers=user_auth).json()
    pages += 1
    assert set(page) == {"added", "modified", "removed", "next_cursor", "has_more"}
    seen += [row["transaction_id"] for row in page["added"]]
    cursor = page["next_cursor"]
    if not page["has_more"]:
        break

assert (pages, len(seen)) == (2, 3)
quantity = page["added"][0]["entries"][0]["quantity"]
assert quantity == {"raw": "300000000000000000", "decimals": 18,
                    "numeric": "0.3", "float": 0.3}
print(f"\nGET /crypto/sync: {pages} pages, {len(seen)} transactions, cursor {cursor}")
print(f"  quantity on the wire: {quantity}")
print("  `raw` is a STRING even here: a JS client cannot round it by accident")

# A bad cursor is a 422 and costs the caller no quota: a client with a
# hard-coded bad parameter cannot drain the project's daily window.
bad = client.get("/crypto/sync?cursor=not-a-cursor", headers=user_auth)
assert bad.status_code == 422
print(f"  bad cursor -> {bad.status_code} {bad.json()['error']['type']}")

# ------------------------------------------------------ 5. batch holdings
batch = client.post("/batch/holdings", json={"items": [
    {"chain": CHAIN, "address": ADDRESS},
    {"chain": "eip155:99999", "address": ADDRESS},        # unknown chain
]}, headers=server_auth)
assert batch.status_code == 200                          # partial success
items = batch.json()["items"]
assert [item["status"] for item in items] == ["ok", "error"]
assert items[0]["result"]["total_value"] == {"amount": "6000", "currency": "USD"}
assert items[1]["error"]["type"] == "UnknownChainError"
print(f"\nPOST /batch/holdings -> {batch.status_code}, items in request order: "
      f"{[item['status'] for item in items]}")
print(f"  item 0: {items[0]['result']['total_value']}")
print(f"  item 1: {items[1]['error']['type']}: one bad item never fails the request,"
      "\n          and index i of the response always answers index i of the request")

# ---------------------------------------------------------- 6. coverage
coverage = client.get("/coverage").json()               # public, no auth
row = next(entry for entry in coverage["chains"] if entry["chain_id"] == CHAIN)
assert row["capabilities"] == {"balances": True, "transactions": True,
                               "positions": False, "prices": True, "xpub": False}
print(f"\nGET /coverage: {len(coverage['chains'])} chains, generated from Deps:")
print(f"  {CHAIN}: " + ", ".join(sorted(name for name, on in row["capabilities"].items() if on)))
print("  (positions=False because no reader is bound: the matrix cannot flatter us)")

# ------------------------------------------------------------- 7. serve it
# In a file called `main.py`:
#
#     from auradefi.api.app import create_app
#     from auradefi.api.deps import Deps
#     app = create_app(Deps(...))      # your stores, your clock
#
#     uvicorn main:app --host 0.0.0.0 --port 8000
#
# Behind a proxy that appends one X-Forwarded-For hop, pass
# `trusted_proxy_hops=1`; it defaults to 0, so no caller can choose the IP
# its own audit row records.
print("\nOK: Plaid's envelope over your ports, and nothing else.")
