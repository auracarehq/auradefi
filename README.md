# auradefi

Open-source, multi-tenant crypto data aggregator. It takes the tenancy model
from Vezgo, the DeFi position depth from DeBank and the transaction
decomposition from Zerion, then emits Plaid's wire format, so crypto lands in
the same downstream schema as bank and exchange data.

It is a library first and a service second. A Python host imports `auradefi`
directly and pays no serialisation or network cost. The HTTP API is a thin
shell over the same importable core.

> Status: alpha, and 0.1.1 is the release to use. All ten SPEC phases are
> implemented. The suite is 3,247 tests green offline on a fresh clone with no
> API keys, all twelve notebooks execute clean, and every example under
> [`examples/`](examples) runs against the published wheel.
>
> Do not use 0.1.0. A separate adversarial review of it found nineteen
> verified defects, of which five were security and four were silent data
> loss. None of them failed a test.
> [`docs/internal/RELEASE_0.1.1.md`](docs/internal/RELEASE_0.1.1.md) is the
> full accounting. 0.1.1 fixes all nineteen and deliberately breaks one id
> derivation, so read *Upgrading* in [`CHANGELOG.md`](CHANGELOG.md) before you
> move library-ingested data across.
>
> Alpha means the gaps in *What is not there* are real; read that section
> before you budget work against this.
> [`STATUS.md`](docs/internal/STATUS.md) carries the live gate state, and
> [`docs/internal/SPEC.md`](docs/internal/SPEC.md) is the design contract.

**[Documentation site →](https://auracarehq.github.io/auradefi/)** carries the
examples, the twelve executable notebooks and the full reference, rendered
with every example's real output.

## Install

```bash
pip install auradefi                # core; httpx is the only dependency
pip install 'auradefi[sql]'         # + the SQLModel ledger backend
pip install 'auradefi[api]'         # + the FastAPI HTTP surface
```

From a clone (if your system python has no pip, `scripts/bootstrap.sh`
handles it):

```bash
git clone https://github.com/auracarehq/auradefi
cd auradefi && bash scripts/bootstrap.sh
.venv/bin/pytest                           # the whole suite, offline, no keys
.venv/bin/python examples/quickstart.py    # every phase, end to end
bash scripts/run_examples.sh               # all eleven examples
```

## Examples

[`examples/`](examples) holds one file per question. Each one is
self-contained, reads nothing from this repository, and runs offline without
API keys. Each asserts its own output, and CI executes all of them, so a
stale example fails the build.

| Example | What it answers |
|---|---|
| [`quickstart.py`](examples/quickstart.py) | the whole library in one file; start here |
| [`01_holdings_for_an_address.py`](examples/01_holdings_for_an_address.py) | a priced portfolio, exactly, with unpriced assets named |
| [`02_embed_in_your_backend.py`](examples/02_embed_in_your_backend.py) | your ports, your tick, your database, and restart resume |
| [`03_write_a_source_adapter.py`](examples/03_write_a_source_adapter.py) | point it at your own chain data (two methods) |
| [`04_persist_to_your_database.py`](examples/04_persist_to_your_database.py) | host-owned DDL, a resumable cursor feed, a reorg |
| [`05_serve_the_http_api.py`](examples/05_serve_the_http_api.py) | Plaid's envelope over HTTP, and batch partial success |
| [`06_isolate_two_tenants.py`](examples/06_isolate_two_tenants.py) | one deployment, many customers, attacked four ways |
| [`07_read_defi_positions.py`](examples/07_read_defi_positions.py) | an LP and a loan that still add up to net worth |
| [`08_report_cost_basis_and_pnl.py`](examples/08_report_cost_basis_and_pnl.py) | FIFO/LIFO/HIFO/ACB, any instant, Plaid `tax_lots[]` |
| [`09_deliver_signed_webhooks.py`](examples/09_deliver_signed_webhooks.py) | signing, the pinned retry schedule, replay |
| [`10_scan_bitcoin_and_solana.py`](examples/10_scan_bitcoin_and_solana.py) | an xpub that never leaves the process, and Token-2022 |

[`examples/README.md`](examples/README.md) annotates the index, and the
[documentation site](https://auracarehq.github.io/auradefi/examples/) renders
every example with its real output.

## Using it

As a library, where the host owns storage, transport, prices and the tick
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

As a service, where `create_app` takes ports you already built:

```python
from auradefi.api.app import create_app
from auradefi.api.deps import Deps

app = create_app(Deps(tenancy=…, keys=…, ledger=…, webhooks=…, clock=…))
# POST /auth/token, POST /connections, GET /crypto/sync  (Plaid's envelope)
# GET /coverage, POST /webhooks/endpoints, POST /webhooks/…/replay
```

[`docs/books/`](docs/books) walks both surfaces executably and offline.

## What works today

Coverage is published as data (rule #10). Every row below has an executable
notebook under [`docs/books/`](docs/books) that runs offline and asserts its
own outputs, plus a gate test under `tests/`.

| Capability | Phase | Limits, and what proves it |
|---|---|---|
| `Quantity`/`Money`: exact at 10^77, four-field wire form, `raw` always a JSON string, strict wire grammar | 0 | [`02_money`](docs/books/02_money.ipynb) |
| CAIP-2/CAIP-19 parse + canonicalize, deterministic `ast_…` ids, both-ways asset registry | 0 | 5 seed chains (Ethereum, Polygon, Base, Bitcoin, Solana); [`03_assets_chains`](docs/books/03_assets_chains.ipynb) |
| Asset groups (decimals-equality law, `single` fallback) + additive spam scoring (score + numbers, caller threshold) | 0 | [`03_assets_chains`](docs/books/03_assets_chains.ipynb) |
| Ledger port: idempotent upsert, cursor sync with `has_more` paging, reorg as `removed` + re-`added`, resurrection, tenant isolation | 0 | memory and SQLModel backends; [`04_ledger`](docs/books/04_ledger.ipynb) |
| Cassette replay harness (`CassetteMissError` offline guarantee) | 0 | [`01_foundation`](docs/books/01_foundation.ipynb) |
| Style gates: size, structure, placement, layering (`tests/style`) | 0 | no allowlist |
| EVM balances to holdings, exact-`Decimal` USD totals, unpriced assets named | 1 | Etherscan V2 source + DefiLlama prices; [`05_holdings`](docs/books/05_holdings.ipynb) |
| Tenancy: org/project/end-user, scoped `adk_` keys, `authEndpoint` JWT mint, three-window quota, audit log | 2 | the isolation gate actively tries to leak; [`06_tenancy`](docs/books/06_tenancy.ipynb) |
| Rich transactions: `parts[]`/`acts[]`, fees as siblings carrying `borne_by`, derived `type`, ledger bridge, reorg + resurrection | 3 | EVM only, one act per transaction; [`07_transactions`](docs/books/07_transactions.ipynb) |
| DeFi positions: adapter protocol, drill-down, group totals + health factor, signed synthetic-Holdings projection | 4 | Uniswap v2/v3, Aave v3, Lido/Rocket Pool; fixture-driven, see below; [`08_positions`](docs/books/08_positions.ipynb) |
| Embedding: `from auradefi import Auradefi`, host-owned session, budgeted two-phase sync, 26-metric scalar projection | 5 | chain-scoped connection ids, restart resume enumerated from the state port, one connection's failure contained to its own row (0.1.1 #18/#21/#24/#26); [`09_embedding`](docs/books/09_embedding.ipynb), [`02_embed_in_your_backend.py`](examples/02_embed_in_your_backend.py) |
| Bitcoin: pure-Python BIP32 xpub derivation, gap-20 scan, confirmed-only UTXO balances | 6 | p2wpkh + Esplora only; the extended key never reaches HTTP; [`10_bitcoin_solana`](docs/books/10_bitcoin_solana.ipynb) |
| Solana: SPL + Token-2022 balances, ScaledUiAmount carried both ways, signature history | 7 | balances only, no decode; [`10_bitcoin_solana`](docs/books/10_bitcoin_solana.ipynb) |
| HTTP API: Plaid `/crypto/sync` envelope, connections, `/coverage` generated as data, nine quota headers, batch holdings | 8 | [`12_http_api`](docs/books/12_http_api.ipynb) |
| Webhooks: HMAC-SHA256 signed, durable over a pinned retry schedule, dead letter + replay | 8 | [`12_http_api`](docs/books/12_http_api.ipynb) |
| Accounting: lot ledger, FIFO/LIFO/HIFO/ACB, realised + unrealised PnL, arbitrary-date PnL, Plaid `tax_lots` | 9 | 50,000-event gate; [`11_accounting`](docs/books/11_accounting.ipynb) |

### What is not there

Rule #10 applies to the absences too.

- There are no live network adapters beyond what the cassettes cover. Every
  I/O path is exercised against committed recordings. Pointing a source at the
  real Etherscan, Esplora or Solana RPC needs your own keys and endpoints,
  and CI has not reconciled the output against an incumbent.
- Positions are fixture-driven. The `ContractReader` seam ships and every
  adapter is pinned to block-20,450,000 golden vectors, but no concrete
  on-chain reader ships. The package has no `eth_call` transport and no
  multicall batcher, so a host must supply its own reader to run the adapters
  against a live chain.
- There is no multicall anywhere, so token balances cost one request each.
- One price oracle (DefiLlama), current prices only. There is no fallback
  feed and no historical price service: `prices/historian.py` and
  `prices/store.py` are declared in the spec's layout and absent, as are the
  `coingecko`, `manual` and `onchain_amm` oracles, so accounting marks are
  the caller's.
- No `jobs/` package. The spec declares `scheduler.py`, `discover.py`,
  `refresh.py`, `reprocess.py` and `backfill.py`; none of them ship. There is
  no scheduler, no background worker and no reprocess path. The host owns
  every tick, and a backfill is a `sync()` budget you spend yourself.
- Five of the nine declared `api/routes/` modules are absent:
  `accounts.py`, `holdings.py`, `positions.py`, `transactions.py` and
  `webhooks.py`. What ships is `auth`, `connections`, `sync` and `admin`, so
  holdings and positions have no HTTP surface of their own, and the webhook
  admin routes live in `admin.py`.
- `project/` ships only `scalar.py`. `project/plaid.py` and
  `project/native.py` are declared and absent. The Plaid envelope is
  projected in `api/wire.py` instead, so that projection is not reusable
  outside the HTTP shell the way the layer contract intends.
- Two ledger backends: in-memory and SQLModel/sqlite. Postgres should work
  through the same port; only sqlite is exercised. The tenancy, keys, quota,
  audit and webhook stores are in-memory only.
- Cosmos is absent, as is every EVM chain the registry does not seed. So are
  exchange connections, NFTs and protocol-specific decoders (`acts[]` is
  always one act, and `protocol` is always `None`).
- Solana transaction decode is not implemented. Balances and signature
  history only.
- No async surface, no background worker and no scheduler. The host owns the
  tick.
- There is no migration for the 0.1.1 embed id break. Library-ingested embed
  connection ids, and every `transaction_id` hashed over them, re-derive in
  0.1.1, so 0.1.0 rows written through `Auradefi` stop matching. A host
  either re-derives them itself or accepts the old rows as orphaned history
  (`CHANGELOG.md`, *Upgrading*). Data written through the HTTP API is
  unaffected.
- `SyncStatePort` is a five-method Protocol in 0.1.1 (`tenants()` was added).
  A host store written against the 0.1.0 four-method shape is refused at bind
  time, so it cannot silently sync nothing.

## The rules the code lives by

- Money is a tagged decimal string, and a raw amount is never a JSON integer.
- Asset ids are deterministic CAIP-19 and permanently stable.
- Every movement is a `part[]`; fees are siblings of movements.
- Multi-tenancy is designed in. Two tenants can never see each other's data.
- `pytest` passes on a fresh clone with no API keys, because the cassettes
  are committed.
- Files cap at 400 lines with no allowlist, and `tests/style/` enforces the
  layer contract.

## Docker

```bash
docker compose run --rm test    # full offline suite in a network-less container
docker compose run --rm demo    # quickstart against the installed wheel
```

## Docs

**[auracarehq.github.io/auradefi](https://auracarehq.github.io/auradefi/)** is
built from this repository, with every example executed at build time and
every signature generated from the code.

Start here:

- [Quickstart](https://auracarehq.github.io/auradefi/quickstart.html): five
  lines, no credentials
- [Authentication & keys](https://auracarehq.github.io/auradefi/authentication.html):
  what you need before mainnet (at most one key, and it is optional)
- [Bring your own](https://auracarehq.github.io/auradefi/bring-your-own.html):
  your API, your database, your prices, with every port and its methods
- [Guides](https://auracarehq.github.io/auradefi/examples/index.html):
  [`examples/`](examples), eleven single files that run offline
- [API reference](https://auracarehq.github.io/auradefi/reference/index.html):
  signatures, parameters, return fields, exceptions
- [HTTP API](https://auracarehq.github.io/auradefi/http.html): Plaid's wire
  format, plus `openapi.json`
- [Errors](https://auracarehq.github.io/auradefi/errors.html): every
  exception and its HTTP status

Also in the repository:

- [`docs/books/`](docs/books) holds twelve executable notebooks, run
  headlessly in CI.
- [`CHANGELOG.md`](CHANGELOG.md) records what changed per release, including
  the 0.1.1 upgrade note.
- [`docs/internal/`](docs/internal) covers how this was designed and built
  rather than how to use it: the [design contract](docs/internal/SPEC.md),
  the [pinned algorithms](docs/internal/DECISIONS.md),
  [build status](docs/internal/STATUS.md), the
  [0.1.0 defect accounting](docs/internal/RELEASE_0.1.1.md), the
  [release procedure](docs/internal/RELEASING.md) and the
  [agent loop](docs/internal/AGENT_PROMPTS.md) that wrote most of it.

## Licence

Apache-2.0. See [`LICENSE`](LICENSE).
