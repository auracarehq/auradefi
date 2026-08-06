# examples

Start here:

```python
pip install auradefi

from auradefi import Auradefi
aura = Auradefi.sandbox()          # no keys, no network, no configuration
for holding in aura.holdings()[0].holdings:
    print(holding.symbol, holding.quantity, holding.value)
```

That is a complete program. Sandbox replays a recording bundled inside the
package, so you get working code before you hold any credential, and every
layer above the transport is the production one. Once you have an Etherscan
key, `Auradefi.from_env()` is the only line that changes.

Sandbox data is a recording, so its numbers are constants: 5025 USD of
holdings and seven transactions. Asking for anything it does not hold raises
`CassetteMissError`, which names what it does hold.

Eleven task-shaped guides follow. Each is a single file that runs offline without
keys, asserts its own output, and prints a readable trace. All of them are
self-contained, so you can copy a file out, `pip install auradefi`, and run it.

```bash
python examples/01_holdings_for_an_address.py
bash scripts/run_examples.sh               # all of them, from a clone
```

Each file's docstring opens with the question it answers and closes with the
change that points it at real infrastructure.

| Guide | What it covers | Needs |
|---|---|---|
| [auradefi in five lines, then the whole library in one file.](quickstart.py) | Every capability in one file, end to end. | core |
| [How do I get a priced portfolio for one address?](01_holdings_for_an_address.py) | Exact `Decimal` totals, unpriced assets named instead of zeroed, the wire form, and the one line that points it at mainnet. | core |
| [How do I run this inside my own backend, with my own database?](02_embed_in_your_backend.py) | Defaults first, then replacing one port at a time: budgeted sync on your tick, restart resume, one failure contained to one connection, and your own database. | core |
| [How do I point this at MY chain data: my RPC, my vendor, my archive?](03_write_a_source_adapter.py) | For when the shipped `EtherscanSource` is not what you want: the two-method seam, the window the engine owns, and how to signal an upstream failure. | core |
| [How do I store this in MY database, and stream changes to my clients?](04_persist_to_your_database.py) | Host-owned DDL, idempotent upsert, a resumable cursor feed, and a reorg emitted as `removed` then re-`added`. | `[sql]` |
| [How do I expose this over HTTP, the way Plaid clients already expect?](05_serve_the_http_api.py) | Plaid's exact shape: token mint, connections, `/crypto/sync` paging, batch partial success, generated `/coverage`. | `[api]` |
| [How do I serve many customers from one deployment without leaking?](06_isolate_two_tenants.py) | Derived tenant ids, project-signed tokens, scoped keys, and per-project quota, attacked four ways. | core |
| [How do I get DeFi positions, an LP, a loan, and not lie about them?](07_read_defi_positions.py) | DeFi positions that still add up: raw quantities, one risk group, the projection invariant, re-pricing at zero chain reads. | core |
| [How do I answer "what did they make, and what tax lots are open"?](08_report_cost_basis_and_pnl.py) | FIFO/LIFO/HIFO/ACB, any instant you ask about, Plaid `tax_lots[]`, and a visible rounding flag. | core |
| [How do I get told when something changes, and trust what arrives?](09_deliver_signed_webhooks.py) | HMAC signing with the shipped verifier, a pinned retry schedule into a dead letter queue, and replay. | core |
| [How do I handle a Bitcoin xpub and Solana's token zoo?](10_scan_bitcoin_and_solana.py) | Non-EVM chains: a Bitcoin xpub that never leaves the process, gap-limit scanning, and a Token-2022 mint that breaks `raw / 10**decimals`. | core |
| [How do I make each error happen on purpose, so I can test my handler?](11_provoke_every_error.py) | Sixteen error types triggered deterministically in three lines each, grouped by whose problem they are, with the `docs_url` each one carries over HTTP. | core |

Install the extras with `pip install 'auradefi[sql]'` or
`pip install 'auradefi[api]'`. `scripts/run_examples.sh` skips an example whose
extra is absent and says so, instead of failing.

## How these relate to the rest of the docs

- **examples/** answers "how do I do X", one file per task. You are here.
- **[`docs/books/`](../docs/books)** holds twelve executable notebooks, one
  per capability. They go considerably deeper and are run headlessly in CI.

CI executes every example here through `scripts/run_examples.sh`, so an
example that stops working fails the build.
