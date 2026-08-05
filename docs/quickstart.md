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

## Where to go next

- [Guides](examples/index.html): one file per task, covering holdings, your
  own source, your own database, the HTTP API, tenancy, positions, cost
  basis, webhooks, Bitcoin and Solana.
- [Authentication & keys](authentication.html): what you need before pointing
  this at mainnet, and what happens when a key is wrong.
- [Bring your own](bring-your-own.html): every port, its exact methods, and a
  minimal implementation of each.
- [API reference](reference/index.html): signatures, parameters, return
  fields and exceptions.

## What this is not

auradefi is alpha, and the [README](index.html) keeps an explicit list of what
is absent: no multicall, one price oracle covering six EVM chains, no Bitcoin
or Solana prices at all, no scheduler, and no on-chain reader for the DeFi
position adapters. Read that list before you budget work against this.
Sandbox makes the library easy to try, and the gaps are still there
afterwards.
