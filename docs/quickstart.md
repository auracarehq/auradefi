# Quickstart

Five lines, no credentials.

```bash
pip install auradefi
```

```python
from auradefi import Auradefi

aura = Auradefi.sandbox()
for holding in aura.holdings()[0].holdings:
    print(holding.symbol, holding.quantity, holding.value)
```

```
ETH  2   5000.000000000000000000 USD
USDC 25  25.000000 USD
```

That is a complete program. It needs no API key, no database, no network and
no configuration file.

## What just happened

`Auradefi.sandbox()` opens the Sandbox environment: a recording of one
address' real Etherscan and DefiLlama traffic, bundled inside the package and
replayed locally. Everything above the transport is the production code path,
using the same source, decoder, ledger and pricing arithmetic as a live
instance.

Sandbox exists so you can write working code before anyone has approved an API
key. It also keeps the examples in these docs from drifting away from the
library.

| | |
|---|---|
| Address | `0x1111111111111111111111111111111111111111` on `eip155:1` |
| Holdings | 2 ETH at 2500 USD and 25 USDC at 1 USD, totalling 5025 USD |
| History | seven transactions in blocks 100 to 107 |
| Time | frozen at the instant the traffic was recorded |

Because Sandbox is a recording, its answers are constants. Ask for something
it does not hold, such as a different address, a second chain or a wider page,
and you get `CassetteMissError` listing what it does hold. That is the offline
guarantee working as intended.

## Record your own Sandbox

One address is enough to learn the shape and not enough to test against your
own data. `Recorder` is the other half: point it at the live service once, and
every run after that is offline.

```python
from auradefi.testing.cassettes import Recorder, load
from auradefi.sources.evm.source import EtherscanSource

with Recorder("mywallet.json") as recorder:                  # records
    EtherscanSource(recorder.client(), api_key=KEY).balances("eip155:1", ADDRESS)

source = EtherscanSource(load("mywallet.json").client())     # replays, no key
```

The saved file is keyless on purpose. Query parameters that carry credentials
are stripped as it writes, which is also what lets the replay run without a
key at all. Response bodies are saved whole, so read a recording before you
commit it: a service that echoes your credential back to you defeats the
stripping.

Committed alongside your tests, that file gives you the offline guarantee this
package holds itself to, over your addresses instead of ours.

## Sync some history

```python
report = aura.sync(budget=10)
print(report.pages_fetched, report.transactions_ingested)   # 5 7
```

`budget` caps how many source pages one call may spend. Cursors let the next
call resume where this one stopped, so a tick is bounded and keeps its place.
Call it again inside `sync_min_interval_s` and it is a no-op that touches no
transport:

```python
aura.sync(budget=10).no_op       # True
```

That is the whole scheduling contract. You call `sync()` on your own schedule,
from your own worker. The package starts no background thread, and nothing
runs unless you ask for it.

## Go live

One line changes:

```python
aura = Auradefi.from_env()
user = aura.user("your-opaque-user-id")
user.connect_address("eip155:1", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
aura.sync(budget=5)
```

```bash
export AURADEFI_ETHERSCAN_API_KEY=…     # optional; see Authentication
```

The key is optional. Without one, Etherscan's keyless tier applies.
[Authentication & keys](authentication.html) lists every service this package
talks to, which of them need a credential (almost none do), and what each
one covers.

Storage still defaults to memory at this point, so `from_env()` on its own
loses everything when the process exits. Passing your own database is one
keyword:

```python
aura = Auradefi.from_env(ledger=SqlModelLedger(session_factory=…))
```

[Bring your own](bring-your-own.html) has that in full, along with the other
four ports.

## Or serve it over HTTP

The library is the product and the HTTP API is one adapter over it, so the
same ports you just bound also make an app:

```python
from auradefi.api.app import create_app
from auradefi.api.deps import Deps

app = create_app(Deps(ledger=…, tenancy=…, keys=…, clock=…, …))
```

It holds no state, opens no connections and creates no stores. Responses use
Plaid's wire format, so a client that already reads Plaid reads this. The
journey a caller makes:

```
POST /auth/token          server key -> short-lived user token
POST /connections         user token -> conn_…
GET  /crypto/sync         user token -> added/modified/removed + cursor
GET  /coverage            public     -> the capability matrix, as data
```

`Deps` has more fields than the four above, and every one of them is a port
you already own. [Guide 05](examples/05_serve_the_http_api.html) is a single
file that wires all of them and drives the result, and the
[HTTP API](http.html) page lists every route with its fields.

## Where to go next

- [Guides](examples/index.html): one file per task, covering holdings, your
  own source, your own database, the HTTP API, tenancy, positions, cost
  basis, webhooks, Bitcoin and Solana.
- [Authentication & keys](authentication.html): what you need before pointing
  this at mainnet, and what happens when a key is wrong.
- [Limits and cost](limits.html): what one call costs in requests, what the
  services allow, and what you get when you cross a line.
- [Bring your own](bring-your-own.html): every port, its exact methods, and a
  minimal implementation of each.
- [Glossary](glossary.html): CAIP-2, parts, acts, tenants, cursors, and every
  other term these pages assume.
- [API reference](reference/index.html): signatures, parameters, return
  fields and exceptions.
- [Build with an LLM](llms.html): a prompt to paste into a model before
  asking it for auradefi code, plus `llms.txt` and the whole documentation
  as one file.

## What this is not

auradefi is alpha, and the [README](index.html) keeps an explicit list of what
is absent: no multicall, one price oracle covering six EVM chains, no Bitcoin
or Solana prices at all, no scheduler, and no on-chain reader for the DeFi
position adapters. Read that list before you budget work against this.
Sandbox makes the library easy to try, and the gaps are still there
afterwards.
