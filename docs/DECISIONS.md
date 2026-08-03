# Decisions

Settled questions, so nobody relitigates them. SPEC §14 tracks the ones
still open.

| # | Decision | Why | When |
|---|---|---|---|
| 1 | **Package name: `auradefi`** (dist and import) | Owner's choice 2026-08-02. `conduit` is taken on PyPI (power-engineering package, 2019); `chainconduit`/`cryptoconduit`/`conduit-defi` were free but rejected. Matches the GitHub repo `auracarehq/auradefi`. | 2026-08-02 |
| 2 | Licence Apache-2.0 | SPEC §1.1 — patent grant, maximises adapter contributions; BUSL killed Zapper's ecosystem. | 2026-08-02 |
| 3 | Build backend hatchling | src layout without MANIFEST ceremony; ships `py.typed` cleanly. | 2026-08-02 |
| 4 | pytest `--import-mode=importlib`, **no `tests/__init__.py`** | The mirror rule produces duplicate test basenames (`tests/chains/test_registry.py` vs `tests/assets/test_registry.py`) which collide fatally under prepend mode. | 2026-08-02 |
| 5 | Domain `__init__.py` are docstring-only; no re-exports | One domain's broken import can never poison another's test run; concurrent agents never edit a shared `__init__`. Enforced by `tests/style/test_structure.py`. | 2026-08-02 |
| 6 | Offline guarantee is an autouse socket-block fixture | SPEC §13 "no API keys" enforced as a hard failure, not a timeout. | 2026-08-02 |
| 7 | Cassette harness ships as public `auradefi.testing.cassettes` | Hosts embedding the library get the same offline-testing story we use ourselves. | 2026-08-02 |
| 8 | Exceptions live in `auradefi/errors.py` only | Convention drift between concurrent agents is a red test (`tests/test_errors.py`), not a debate. | 2026-08-02 |
| 9 | httpx is the only HTTP client, runtime dependency | One client to cassette-mock; layering gate confines it to I/O domains. | 2026-08-02 |
| 10 | Version 0.1.0, Development Status :: 3 - Alpha; artifacts built but **never uploaded autonomously** | Publishing needs the owner's PyPI credentials and judgement. `docs/RELEASING.md` has the exact commands. | 2026-08-02 |

## Pinned algorithms (public stability guarantees)

These are wire-format contracts (SPEC rule #3). Changing any of them is a
breaking change to persisted data, guarded by golden vectors in tests.

- **Quantity wire form**: `{"raw": "<decimal int string>", "decimals": <int>, "numeric": "<exact decimal string>", "float": <lossy float>}`. `raw` is a JSON **string**. `numeric` never uses scientific notation.
- **Asset id**: `"ast_" + sha256("\n".join(sorted(canonical_caip19s))).hexdigest()[:16]`. Canonical CAIP-19 lowercases EVM addresses; Solana references keep base58 case.
- **Transaction id**: `"txn_" + sha256(f"{chain_id}|{tx_hash}|{account_id}").hexdigest()[:16]` (SPEC §4.4: hash over chain, tx hash, account).
- **Cursor token**: opaque to callers; internally a backend-monotonic last-modified sequence serialised as `f"{seq:020d}"` so lexicographic order equals numeric order.
