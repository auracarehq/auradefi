# examples

Ten task-shaped recipes, each a single file that **runs offline with no API
keys**, asserts its own output, and prints a readable trace. Every one is
self-contained: it reads nothing from this repository, so you can copy one
file out, `pip install auradefi`, and run it.

```bash
pip install auradefi                       # core: httpx is the only dependency
python examples/01_holdings_for_an_address.py

bash scripts/run_examples.sh               # all of them, from a clone
```

Each file's docstring opens with the question it answers and closes with the
change that points it at real infrastructure.

| Example | Answers | Needs |
|---|---|---|
| [`quickstart.py`](quickstart.py) | The whole library in one file — every phase, end to end. Start here. | core |
| [`01_holdings_for_an_address.py`](01_holdings_for_an_address.py) | Priced portfolio for one address: exact `Decimal` totals, unpriced assets named rather than zeroed, and the wire form. | core |
| [`02_embed_in_your_backend.py`](02_embed_in_your_backend.py) | `from auradefi import Auradefi` inside your own service: your ports, budgeted sync on your tick, restart resume, one failure contained to one connection. | core |
| [`03_write_a_source_adapter.py`](03_write_a_source_adapter.py) | Point it at your own chain data. The two-method seam, what the engine asks for and in what order, and how to signal an upstream failure. | core |
| [`04_persist_to_your_database.py`](04_persist_to_your_database.py) | Store it in your database: host-owned DDL, idempotent upsert, a resumable cursor feed, and a reorg emitted as `removed` then re-`added`. | `[sql]` |
| [`05_serve_the_http_api.py`](05_serve_the_http_api.py) | Expose it over HTTP in Plaid's shape: token mint, connections, `/crypto/sync` paging, batch partial success, generated `/coverage`. | `[api]` |
| [`06_isolate_two_tenants.py`](06_isolate_two_tenants.py) | Many customers, one deployment: derived tenant ids, project-signed tokens, scoped keys, per-project quota — attacked four ways. | core |
| [`07_read_defi_positions.py`](07_read_defi_positions.py) | DeFi positions that still add up: raw quantities, one risk group, the projection invariant, re-pricing at zero chain reads. | core |
| [`08_report_cost_basis_and_pnl.py`](08_report_cost_basis_and_pnl.py) | Cost basis and PnL: FIFO/LIFO/HIFO/ACB, any instant you ask about, Plaid `tax_lots[]`, and a visible rounding flag. | core |
| [`09_deliver_signed_webhooks.py`](09_deliver_signed_webhooks.py) | Webhooks you can trust: HMAC signing with the shipped verifier, a pinned retry schedule into a dead letter queue, and replay. | core |
| [`10_scan_bitcoin_and_solana.py`](10_scan_bitcoin_and_solana.py) | Non-EVM chains: a Bitcoin xpub that never leaves the process, gap-limit scanning, and a Token-2022 mint that breaks `raw / 10**decimals`. | core |

Install the extras with `pip install 'auradefi[sql]'` or
`pip install 'auradefi[api]'`; `scripts/run_examples.sh` skips an example
whose extra is absent and says so rather than failing.

## How these relate to the rest of the docs

- **examples/** — "how do I do X", one file per task. You are here.
- **[`docs/books/`](../docs/books)** — twelve executable notebooks, one per
  SPEC phase, that go considerably deeper and are run headlessly in CI so
  they cannot rot.
- **[`docs/SPEC.md`](../docs/SPEC.md)** — the design contract, and
  [`docs/DECISIONS.md`](../docs/DECISIONS.md) every pinned algorithm and id
  formula.

Every example here is executed by CI (`scripts/run_examples.sh`), so an
example that stops working fails the build rather than misleading a reader.
