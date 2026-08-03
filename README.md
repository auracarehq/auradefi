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
