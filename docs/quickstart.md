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

That is a complete program. No API key, no database, no network, no
configuration file.

## What just happened

`Auradefi.sandbox()` is the **Sandbox environment**: a recording of one
address' real Etherscan and DefiLlama traffic, bundled inside the package
and replayed locally. Everything above the transport is the production code
path — the same source, the same decoder, the same ledger, the same pricing
arithmetic a live instance uses.

Sandbox exists so you can write working code before anyone has approved an
API key, and so an example in these docs can never drift from the library.

| | |
|---|---|
| Address | `0x1111111111111111111111111111111111111111` on `eip155:1` |
| Holdings | 2 ETH at 2500 USD, 25 USDC at 1 USD — **5025 USD** |
| History | seven transactions in blocks 100–107 |
| Time | frozen at the instant the traffic was recorded |

Sandbox answers are **constants**, because they are a recording. Ask for
something it does not hold — a different address, a second chain, a wider
page — and you get `CassetteMissError` listing what it does hold. That is
the offline guarantee working, not a bug.

## Sync some history

```python
report = aura.sync(budget=10)
print(report.pages_fetched, report.transactions_ingested)   # 5 7
```

`budget` caps how many source pages **one call** may spend. Cursors make the
next call resume where this one stopped, so a tick is bounded and never
loses its place. Call it again inside `sync_min_interval_s` and it is a
no-op that touches no transport:

```python
aura.sync(budget=10).no_op       # True
```

That is the whole scheduling contract: you call `sync()` on your own
schedule, from your own worker. There is no background thread in this
package and nothing runs unless you ask it to.

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

The key is **optional** — without one, Etherscan's keyless tier applies.
[Authentication & keys](authentication.html) lists every service this
package talks to, which of them need a credential (almost none do), and what
each covers.

Storage still defaults to memory at this point, which means `from_env()`
alone loses everything when the process exits. Passing your own database is
one keyword:

```python
aura = Auradefi.from_env(ledger=SqlModelLedger(session_factory=…))
```

[Bring your own](bring-your-own.html) has that in full, along with the other
four ports.

## Where to go next

- **[Guides](examples/index.html)** — one file per task: holdings, your own
  source, your own database, the HTTP API, tenancy, positions, cost basis,
  webhooks, Bitcoin and Solana.
- **[Authentication & keys](authentication.html)** — what you need before
  pointing this at mainnet, and what happens when a key is wrong.
- **[Bring your own](bring-your-own.html)** — every port, its exact methods,
  and a minimal implementation of each.
- **[API reference](reference/index.html)** — signatures, parameters, return
  fields and exceptions.

## A word on what this is not

auradefi is **alpha**, and the [README](index.html) keeps an explicit list of
what is absent — no multicall, one price oracle covering six EVM chains, no
Bitcoin or Solana prices at all, no scheduler, no on-chain reader for the
DeFi position adapters. Read that list before you budget work against this.
Sandbox makes the library easy to try; it does not make the gaps go away.
