# auradefi

Open-source, multi-tenant crypto data aggregator. The tenancy model of
Vezgo, the DeFi position depth of DeBank, the transaction decomposition of
Zerion, and **Plaid's wire format** — so crypto merges with bank and
exchange data in one schema downstream.

**Library first, service second.** A Python host imports `auradefi`
directly and pays no serialisation or network cost; the HTTP API is a thin
shell over the importable core.

> Status: alpha. All ten SPEC phases are implemented and the 0.1.0 release
> gate was green offline on a fresh clone with no API keys.
>
> **Do not use 0.1.0.** An independent adversarial review found nineteen
> verified defects in it — five security, four silent data loss — none of
> which failed a test. [`docs/RELEASE_0.1.1.md`](docs/RELEASE_0.1.1.md) is
> the full accounting and 0.1.1 is the fix release, **in progress**.
> [`STATUS.md`](STATUS.md) carries the live test count and which of those
> fixes have landed; the capability table below says what works today, and
> [`docs/SPEC.md`](docs/SPEC.md) is the full design contract.

## Install

```bash
pip install auradefi                # core — httpx is the only dependency
pip install 'auradefi[sql]'         # + the SQLModel ledger backend
pip install 'auradefi[api]'         # + the FastAPI HTTP surface
```

From a clone (no pip on your system python? `scripts/bootstrap.sh` handles it):

```bash
git clone https://github.com/auracarehq/auradefi
cd auradefi && bash scripts/bootstrap.sh
.venv/bin/pytest                              # the whole suite, offline, no keys
.venv/bin/python docs/examples/quickstart.py  # every phase, end to end
```

## Using it

**As a library** — the host owns storage, transport, prices and the tick
(SPEC §8):

```python
from auradefi import Auradefi

auradefi = Auradefi(
    ledger=SqlModelLedger(session_factory=my_session_factory),  # your database
    source=MySource(),        # your transport: .balances() + .fetch_txlist()
    prices=MyPrices(),        # your price feed: .usd_prices()
)
user = auradefi.user("opaque-host-user-id")   # get-or-create, id is derived
user.connect_address("eip155:1", "0x…")       # validated now, not on a later tick
report = auradefi.sync(budget=5)              # budgeted, resumable, self-throttling
holdings, metrics = auradefi.holdings(), auradefi.scalar_metrics()
```

**As a service** — `create_app` takes ports you already built:

```python
from auradefi.api.app import create_app
from auradefi.api.deps import Deps

app = create_app(Deps(tenancy=…, keys=…, ledger=…, webhooks=…, clock=…))
# POST /auth/token   ·  POST /connections  ·  GET /crypto/sync  (Plaid's envelope)
# GET  /coverage     ·  POST /webhooks/endpoints  ·  POST /webhooks/…/replay
```

Both surfaces are walked — executably, offline — in
[`docs/books/`](docs/books).

## What works today

Coverage published as data, not prose optimism (rule #10). Every row marked
**works** has an executable notebook under [`docs/books/`](docs/books) that
runs offline and asserts its own outputs, plus a gate test under `tests/`.

| Capability | Phase | Status |
|---|---|---|
| `Quantity`/`Money`: exact at 10^77, four-field wire form, `raw` always a JSON string, strict wire grammar | 0 | **works** — [`02_money`](docs/books/02_money.ipynb) |
| CAIP-2/CAIP-19 parse + canonicalize, deterministic `ast_…` ids, both-ways asset registry | 0 | **works** — 5 seed chains (Ethereum, Polygon, Base, Bitcoin, Solana); [`03_assets_chains`](docs/books/03_assets_chains.ipynb) |
| Asset groups (decimals-equality law, `single` fallback) + additive spam scoring (score + numbers, caller threshold) | 0 | **works** — [`03_assets_chains`](docs/books/03_assets_chains.ipynb) |
| Ledger port: idempotent upsert, cursor sync with `has_more` paging, reorg = `removed` + re-`added`, resurrection, tenant isolation | 0 | **works** — memory **and** SQLModel backends; [`04_ledger`](docs/books/04_ledger.ipynb) |
| Cassette replay harness (`CassetteMissError` offline guarantee) | 0 | **works** — [`01_foundation`](docs/books/01_foundation.ipynb) |
| Style gates: size, structure, placement, layering (`tests/style`) | 0 | **works** — no allowlist |
| EVM balances → holdings, exact-`Decimal` USD totals, unpriced assets named not guessed | 1 | **works** — Etherscan V2 source + DefiLlama prices; [`05_holdings`](docs/books/05_holdings.ipynb) |
| Tenancy: org/project/end-user, scoped `adk_` keys, `authEndpoint` JWT mint, three-window quota, audit log | 2 | **works** — isolation gate actively tries to leak; [`06_tenancy`](docs/books/06_tenancy.ipynb) |
| Rich transactions: `parts[]`/`acts[]`, fees as siblings carrying `borne_by`, derived `type`, ledger bridge, reorg + resurrection | 3 | **works** — EVM only, one act per transaction; [`07_transactions`](docs/books/07_transactions.ipynb) |
| DeFi positions: adapter protocol, drill-down, group totals + health factor, signed synthetic-Holdings projection | 4 | **works** — Uniswap v2/v3, Aave v3, Lido/Rocket Pool; **fixture-driven, see below**; [`08_positions`](docs/books/08_positions.ipynb) |
| Embedding: `from auradefi import Auradefi`, host-owned session, budgeted two-phase sync, 26-metric scalar projection | 5 | **RED on this branch** — the 0.1.1 sync fixes (#18/#21/#24) are in `embed/` and their recorded fixture has not been re-recorded to match the new backfill window, so the engine is currently losing rows against it; see [`STATUS.md`](STATUS.md). Chain-scoped connection ids, restart resume and per-connection failure isolation are demonstrated in [`docs/examples/quickstart.py`](docs/examples/quickstart.py); [`09_embedding`](docs/books/09_embedding.ipynb) does **not** execute clean |
| Bitcoin: pure-Python BIP32 xpub derivation, gap-20 scan, confirmed-only UTXO balances | 6 | **works** — p2wpkh + Esplora only; the extended key never reaches HTTP; [`10_bitcoin_solana`](docs/books/10_bitcoin_solana.ipynb) |
| Solana: SPL + Token-2022 balances, ScaledUiAmount carried both ways, signature history | 7 | **works** — balances only, no decode; [`10_bitcoin_solana`](docs/books/10_bitcoin_solana.ipynb) |
| HTTP API: Plaid `/crypto/sync` envelope, connections, `/coverage` generated as data, nine quota headers, batch holdings | 8 | **works** — [`12_http_api`](docs/books/12_http_api.ipynb) |
| Webhooks: HMAC-SHA256 signed, durable over a pinned retry schedule, dead letter + replay | 8 | **works** — [`12_http_api`](docs/books/12_http_api.ipynb) |
| Accounting: lot ledger, FIFO/LIFO/HIFO/ACB, realised + unrealised PnL, **arbitrary-date** PnL, Plaid `tax_lots` | 9 | **works** — 50,000-event gate; [`11_accounting`](docs/books/11_accounting.ipynb) |

### What is **not** there

Stated plainly, because rule #10 cuts both ways.

- **No live network adapters beyond what the cassettes cover.** Every I/O
  path is exercised against committed recordings. Pointing a source at the
  real Etherscan / Esplora / Solana RPC needs your own keys and endpoints,
  and has not been reconciled against an incumbent in CI.
- **Positions are fixture-driven.** The `ContractReader` seam ships and
  every adapter is pinned to block-20,450,000 golden vectors, but **no
  concrete on-chain reader ships** — there is no `eth_call` transport and no
  multicall batcher in the package. A host must supply its own reader to run
  the adapters against a live chain.
- **No multicall anywhere.** Token balances cost one request each.
- **One price oracle** (DefiLlama), current prices only. No fallback feed and
  no historical price service: `prices/historian.py` and `prices/store.py` are
  declared in the spec's layout and **absent**, along with the `coingecko`,
  `manual` and `onchain_amm` oracles, so accounting marks are the caller's.
- **No `jobs/` package.** The spec declares `scheduler.py`, `discover.py`,
  `refresh.py`, `reprocess.py` and `backfill.py`; none of them ship. There is
  no scheduler, no background worker and no reprocess path — the host owns
  every tick, and a backfill is a `sync()` budget you spend yourself.
- **Five of the nine declared `api/routes/` modules are absent**:
  `accounts.py`, `holdings.py`, `positions.py`, `transactions.py` and
  `webhooks.py`. What ships is `auth`, `connections`, `sync` and `admin` — so
  holdings and positions have no HTTP surface of their own, and the webhook
  admin routes live in `admin.py` rather than in a `webhooks.py`.
- **`project/` ships only `scalar.py`.** `project/plaid.py` and
  `project/native.py` are declared and absent; the Plaid envelope is
  projected in `api/wire.py` instead, which means that projection is not
  reusable outside the HTTP shell the way the layer contract intends.
- **Two ledger backends**: in-memory and SQLModel/sqlite. Postgres should
  work through the same port; only sqlite is exercised. Tenancy, keys,
  quota, audit and webhook stores are **in-memory only**.
- **Cosmos is absent**, as is every EVM chain the registry does not seed,
  along with exchange connections, NFTs and protocol-specific decoders
  (`acts[]` is always one act and `protocol` is always `None`).
- **Solana transaction decode is not implemented** — balances and signature
  history only.
- No async surface, no background worker, no scheduler: the host owns the
  tick.
- **No migration for the 0.1.1 embed id break.** Library-ingested embed
  connection ids — and every `transaction_id` hashed over them — re-derive in
  0.1.1, so 0.1.0 rows written through `Auradefi` stop matching. A host either
  re-derives them itself or accepts the old rows as orphaned history
  (`CHANGELOG.md`, *Upgrading*). Data written through the **HTTP API** is
  unaffected.
- **`SyncStatePort` is a five-method Protocol in 0.1.1** (`tenants()` was
  added). A host store written against the 0.1.0 four-method shape is refused
  at bind time rather than silently syncing nothing.

## The rules the code lives by

- Money is a tagged decimal string; a raw amount is **never** a JSON integer.
- Asset ids are deterministic CAIP-19 and permanently stable.
- Every movement is a `part[]`; fees are siblings, never movements.
- Multi-tenancy is designed in; two tenants can never see each other's data.
- `pytest` passes on a fresh clone with **no API keys** — cassettes committed.
- Files cap at 400 lines with no allowlist; the layer contract is enforced
  by tests (`tests/style/`), not by convention.

## Docker

```bash
docker compose run --rm test    # full offline suite in a network-less container
docker compose run --rm demo    # quickstart against the installed wheel
```

## Docs

- [`docs/SPEC.md`](docs/SPEC.md) — the design contract
- [`docs/books/`](docs/books) — twelve executable notebooks, run headlessly in
  CI so they cannot rot
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — every pinned algorithm and id formula
- [`STATUS.md`](STATUS.md) — phase gates and known caveats
- [`docs/AGENT_PROMPTS.md`](docs/AGENT_PROMPTS.md) — the agent loop that builds this repo, with copy-paste prompts
- [`docs/RELEASING.md`](docs/RELEASING.md) — pip + Docker release procedure
- [`docs/RELEASE_0.1.1.md`](docs/RELEASE_0.1.1.md) — every defect found in
  0.1.0, its fix and the regression-test protocol
- [`CHANGELOG.md`](CHANGELOG.md) — what changed per release, including the
  0.1.1 upgrade note

## Licence

Apache-2.0 — see [`LICENSE`](LICENSE).
