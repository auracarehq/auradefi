# auradefi

Open-source, multi-tenant crypto data aggregator. The tenancy model of
Vezgo, the DeFi position depth of DeBank, the transaction decomposition of
Zerion, and **Plaid's wire format** — so crypto merges with bank and
exchange data in one schema downstream.

**Library first, service second.** A Python host imports `auradefi`
directly and pays no serialisation or network cost; the HTTP API is a thin
shell over the importable core.

> Status: alpha, under active construction. See [`STATUS.md`](STATUS.md)
> for exactly what works today and [`docs/SPEC.md`](docs/SPEC.md) for the
> full design contract.

## Install

```bash
pip install auradefi          # or: uv pip install auradefi
```

From a clone (no pip on your system python? `scripts/bootstrap.sh` handles it):

```bash
git clone https://github.com/auracarehq/auradefi
cd auradefi && bash scripts/bootstrap.sh
```

## What works today

Coverage published as data, not prose optimism (SPEC rule #10). Everything
marked **works** has an executable walkthrough under
[`docs/books/`](docs/books) that runs offline and asserts its own outputs.

| Capability | Phase | Status |
|---|---|---|
| `Quantity`/`Money`: exact at 10^77, four-field wire form, `raw` always a JSON string, strict wire grammar | 0 | **works** — [`02_money`](docs/books/02_money.ipynb) |
| CAIP-2/CAIP-19 parse + canonicalize, deterministic `ast_…` ids, both-ways asset registry | 0 | **works** — 5 seed chains (Ethereum, Polygon, Base, Bitcoin, Solana); [`03_assets_chains`](docs/books/03_assets_chains.ipynb) |
| Asset groups (decimals-equality law, `single` fallback) + additive spam scoring (score + numbers, caller threshold) | 0 | **works** — [`03_assets_chains`](docs/books/03_assets_chains.ipynb) |
| Ledger port + in-memory backend: idempotent upsert, cursor sync with `has_more` paging, reorg = `removed` + re-`added`, tenant isolation | 0 | **works** — memory backend only, SQL backend is Phase 5; [`04_ledger`](docs/books/04_ledger.ipynb) |
| Cassette replay harness (`CassetteMissError` offline guarantee) | 0 | **works** — [`01_foundation`](docs/books/01_foundation.ipynb) |
| Style gates: size, structure, placement, layering (`tests/style`) | 0 | **works** — no allowlist |
| EVM balances → holdings (Etherscan V2 + DefiLlama prices) | 1 | **in flight** — under construction, not usable yet |
| Tenancy, transaction decode, positions, embedding surface, Bitcoin/xpub, Solana, HTTP API, accounting | 2–9 | not started |

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
- `docs/books/` — executable notebook guides (run in CI so they cannot rot)
- [`docs/AGENT_PROMPTS.md`](docs/AGENT_PROMPTS.md) — the agent loop that builds this repo, with copy-paste prompts
- [`docs/RELEASING.md`](docs/RELEASING.md) — pip + Docker release procedure

## Licence

Apache-2.0 — see [`LICENSE`](LICENSE).
