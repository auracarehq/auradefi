# auradefi 0.2.0: closing the declared layout

Self-contained handoff. Everything needed to take auradefi from "0.1.2 is
correct, installable and honest about its gaps" to "the layout SPEC §3.2
declares is the layout that ships".

---

## 1. Situation

**0.1.2 is released and correct.** 3,426 tests green offline on a fresh clone
with no API keys, `bash scripts/release_check.sh` PASSED, twelve notebooks and
eleven examples clean, the suite green in a `--network none` container.

What remains open is one thing wearing fifteen numbers. GitHub issues **#1 to
#15** are the gap between the module layout `docs/internal/SPEC.md` §3.2
declares and the tree that shipped. None of them is a defect: every one is a
capability the spec advertises and the package does not have, each already
inventoried in `tests/style/test_spec_layout_matches_tree.py` and named in
README's *What is not there*.

That inventory is the reason this release can be planned mechanically. The gate
diffs the declared layout against the tree in **both** directions and fails when
either list goes stale, so `DECLARED_BUT_ABSENT` is an exact, test-enforced work
list: 42 entries, of which 38 are in scope here and four are the position
adapters §12 defers. Three of the fifteen issues are capability gaps that are
not modules at all (#13 persistence, #14 Postgres, #15 live reconciliation).

Two of the fifteen were decisions before they were code, and both have since
been made. #11 asked whether `jobs/` should exist or whether §3.2 should stop
advertising it: the tree answered, four of its five modules already ship under
other names, so §3.2 dropped it and the issue is closed (§11). #15 asks for a
credentialed drift job the default suite must never run, and that gets built.
Two more were reshaped by the same reading: #4 lost its `helius.py` half (a
paid vendor behind a second key, for a decode the keyless endpoint already
reaches) and #10 lost its route (the PATCH it specifies would re-derive every
id the tenant owns; §8 states what replaces it).

The order below is a dependency order, not a priority order. Phase 11 exists
first because six later modules cannot be written without it: there is no
`eth_call` transport anywhere in `src/auradefi/`, so every on-chain read in the
package today goes through the Etherscan V2 aggregator or a hand-written
fixture.

---

## 2. Definition of done for 0.2.0

All of the following, none assumed:

1. Every issue #1 to #15 closed or answered, each closure that ships code
   carrying a test that fails against the tree before its phase and passes
   after. #11 is already closed as not planned.
2. `DECLARED_BUT_ABSENT` in `tests/style/test_spec_layout_matches_tree.py` is
   **empty** except for the four position adapters §12 puts out of scope, and
   the entries #11 and #4 removed from §3.2 altogether, and
   `SHIPPED_BUT_UNDECLARED` accounts for every module this release adds that
   §3.2 does not name.
3. `.venv/bin/pytest` green. The count will exceed 3,426; new tests only.
4. `bash scripts/release_check.sh` → PASSED.
5. `docker build --target test` + `docker run --rm --network none` → green.
6. All notebooks execute clean in the network-less container.
7. `pyproject.toml` version **and** `src/auradefi/__init__.py` `__version__`
   both read `0.2.0`. `release_check.sh` fails on a half-bump.
8. `CHANGELOG.md` has a `[0.2.0]` section listing every closed issue by number.
9. README's *What is not there* and `docs/internal/STATUS.md` describe the tree
   as it then is. Rule #10 cuts both ways: overstating what ships is a
   correctness problem, and so is understating it.

This is seven phases of work. Run one at a time and read the report before
starting the next, as `docs/internal/loop.md` says. A phase that lands is a
commit on `main`; the version bump and the CHANGELOG section are the last
phase's work, not the first's.

---

## 3. Standing constraints every phase inherits

These are facts about the gates that will judge the work, established by
reading them rather than by assuming. An agent that does not know these will
write code that cannot land.

**The layout inventory is the work list, and it fights you while you work.**
`test_spec_layout_matches_tree.py::test_every_declared_absence_is_inventoried`
asserts set equality. The moment a phase's first module exists, that test goes
red and stays red until `DECLARED_BUT_ABSENT` loses the entry. The orchestrator
therefore stubs a phase's modules and edits the inventory in **one preparatory
commit before the phase's baseline is recorded**, so both the stub's mirror-rule
failure and the inventory edit are inherited state rather than something an
agent broke (loop.md invariant 8). No agent edits that file mid-phase.

**Tests mirror source, exactly, both ways.** `test_placement.py` requires
`src/auradefi/a/b.py` to have `tests/a/test_b.py` and forbids the orphan in
either direction. Only `tests/style/**`, `tests/golden/**`, `tests/cassettes/**`
and `tests/contract/**` are exempt. A new module without its mirror is a red
gate, so every work order names both.

**Ten modules per directory, no exceptions.** `test_structure.py` caps
non-`__init__` modules at `MAX_DIR_FILES = 10` and says "grow a subfolder".
`sources/evm/` holds seven today and phase 11 adds four, so phase 11 grows
`sources/evm/codec/`. Count before you write.

**300 lines soft, 400 hard, no allowlist.** `test_size.py` applies to `src/`
only. Several phases here split a module for this reason and each split is
pre-authorised below by name, because loop.md's fix-release note is right that
discovering a cap at the cap is more expensive than planning for it.

**Package `__init__.py` files are docstring-only.** No re-exports, anywhere,
including new subpackages. This is enforced and it is deliberate.

**The layer contract is enforced over the real import graph.**
`test_layering.py` holds the edges that matter here:

- `sources` may import `money`, `chains`, `assets`, `testing`. It may **not**
  import `positions`, which is why phase 11's concrete reader binds to
  `ContractReader` structurally and never imports the protocol that names it.
- `prices` may import `sources`. `prices/oracles/onchain_amm.py` is legal.
- `decode` may import `sources` and `prices`. `decode/enrich.py` is legal.
- `positions` is **not** in `IO_DOMAINS`, so no HTTP client may ever appear
  there. `IO_DOMAINS` is `sources`, `prices`, `testing`, `api`, `webhooks`.
  `jobs` was in both that set and `ALLOWED_IMPORTS` until #11 closed; removing
  a domain nothing implements is part of that closure, because a declared
  layer for a package that will never exist reads as a plan.
- **An ORM may only be imported under `ledger/backends/`.** Issue #13's own
  suggested fix, "a SQLModel backend per store", is illegal as written. Phase 16
  widens that exemption to a declared list of per-domain `backends/`
  subpackages. That is a deliberate gate change, made once, in a preparatory
  commit, and recorded in DECISIONS.md.
- **A web framework may only be imported under `api/`.** This is the whole
  point of #12: while the Plaid projection lives in `api/wire.py`, an embedder
  cannot reach Plaid-shaped output without taking the FastAPI dependency.

**No new third-party dependencies. stdlib first, always.** This is the most
expensive rule in the release and phase 11 pays for it in full: `hashlib`
provides `sha3_256`, which is **not** keccak256 (different padding), and an
Ethereum function selector is the first four bytes of the keccak256 of the
signature. Phase 11 therefore hand-rolls keccak-f[1600]. It is roughly seventy
lines and it has published vectors, so it is verifiable; it is not optional and
it is not a place to be clever.

**The suite runs offline.** `tests/conftest.py` monkeypatches
`socket.socket.connect` to raise, autouse, for every test. Every I/O path in
this release is exercised through a committed cassette. Phase 16's Postgres run
and phase 18's reconciliation job are the two things that need a real socket,
and both are opt-in and out of the default suite by construction.

**Incomplete data is declared, never defaulted to zero.** Rule #8. It applies
to every new number this release produces: an unpriceable asset, a reverting
`eth_call`, an oracle that does not list a token, a decode that cannot attribute
a protocol. `None` with a `data_quality` note, never `0`.

---

## 4. Phase 11: an EVM node path

**Closes #3, #2, #1.**

Today every EVM read goes through the Etherscan V2 aggregator. There is no
direct-node path, no batching, no log-scanning surface, and no concrete
`ContractReader`, so `positions/` resolves entirely against hand-written
fixtures and a host must supply its own reader before any adapter can touch a
live chain. `STATUS.md` *Known caveats* #2 calls this the largest gap between
"works" in the README capability table and "works against mainnet".

### Modules

```
sources/evm/codec/keccak.py    keccak-f[1600]; keccak256(bytes) -> bytes
sources/evm/codec/abi.py       static-type ABI encode/decode + selector
sources/evm/rpc.py             JSON-RPC client: single and batched
sources/evm/multicall.py       Multicall3 aggregate3, per-call failure isolated
sources/evm/logs.py            eth_getLogs over chunked block ranges
sources/evm/reader.py          the concrete ContractReader
```

`sources/evm/` holds `etherscan.py`, `source.py`, `txfetch.py`, `txlist.py`
today. Adding `rpc.py`, `multicall.py`, `logs.py` and `reader.py` takes it to
eight, under the cap; `keccak.py` and `abi.py` go to `codec/` because they are
pure and have nothing to do with HTTP, and because nine was too close to ten.

`sources/evm/codec/keccak.py`, `codec/abi.py` and `reader.py` are not in §3.2
and go to `SHIPPED_BUT_UNDECLARED` with the reason above.

### Interface detail

**The call surface is bounded and known.** Every `ContractReader` call site in
the package was enumerated before this was written, and the whole set is:

| Function | Argument types | Return |
|---|---|---|
| `balanceOf` | `address` | `uint256` |
| `decimals` | none | `uint8` |
| `totalSupply` | none | `uint256` |
| `token0`, `token1` | none | `address` |
| `getReserves` | none | `(uint112,uint112,uint32)` |
| `allPairsLength` | none | `uint256` |
| `allPairs` | `uint256` | `address` |
| `slot0` | none | `(uint160,int24,uint16,uint16,uint16,uint8,bool)` |
| `positions` | `uint256` | 12-tuple, `uint96`/`address`/`uint24`/`int24`/`uint128`/`uint256` |
| `getPool` | `address,address,uint24` | `address` |
| `tokenOfOwnerByIndex` | `address,uint256` | `uint256` |
| `getUserAccountData` | `address` | `(uint256,)*6` |
| receipt `rate_fn` | none | `uint256` |

**Not one dynamic type.** No `string`, no `bytes`, no arrays, no nested tuples.
Every value is a single 32-byte word or a fixed sequence of them. `abi.py`
therefore implements exactly `uint<N>`, `int<N>` in two's complement, `address`
and `bool`, and **raises on anything else** rather than guessing. That refusal
is the interesting test: a codec that silently mis-encodes a type it does not
support is the shape of defect this project cuts releases over.

`reader.py` carries the signature registry as data, keyed by function name to
`(arg_types, return_types)`, sourced from the table above. An unknown `fn` is
an error, not a best effort. A reader whose registry does not know a function
the adapters call is a startup failure, and a test asserts the registry covers
every call site.

`rpc.py` speaks JSON-RPC 2.0 over `httpx` behind the existing cassette
discipline: `eth_call`, `eth_getBalance`, `eth_blockNumber`, `eth_getLogs`, and
a batch form that posts an array and matches responses by `id` rather than by
position. Matching by position is the defect to write a test against: a
compliant node may return a batch in any order.

`multicall.py` targets Multicall3's `aggregate3((address,bool,bytes)[])`, whose
whole reason for existing here is `allowFailure`. One reverting call must not
void the batch; it must come back as a declared failure for that call alone.
Multicall3's own calldata **is** dynamic (a dynamic array of tuples containing
`bytes`), which is the one place `abi.py` needs dynamic encoding, so it is
implemented there as an explicit, named special case with its own vectors,
not as general dynamic support.

`logs.py` scans `eth_getLogs` over a block range, chunked, with topic filters,
and returns typed log records. It exists because the decode protocol handlers in
phases 13 and 14 need it.

`reader.py` binds `ContractReader` structurally. It never imports
`auradefi.positions`. `runtime_checkable` `isinstance` against the protocol is
the test that proves the binding without creating the import edge the layering
gate forbids.

### Done when

`tests/golden/test_phase11_reader.py`: every shipped position adapter
(`tokens`, `amm/uniswap_v2`, `amm/uniswap_v3`, `lending/aave`,
`staking/liquid`) resolves against a **cassette-backed reader** at block
20,450,000 and produces Positions equal to the ones the existing hand-written
fixtures produce. The block pin stays 20,450,000 so the existing
`tests/golden/test_positions_*.py` vectors keep their meaning, and the equality
is what makes this a golden vector rather than a new claim: the recorded node
and the hand-written fixture must agree, or one of them is wrong.

Plus: a reverting call in a batch of five yields four results and one declared
failure; a batch response returned in reversed `id` order is matched correctly;
`keccak256(b"")` is
`c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470`.

---

## 5. Phase 12: prices that can look backwards

**Closes #6, #7.**

`prices/__init__.py` says it outright: "current-USD lookups; the historian
arrives later." There is no historical price service and no price cache, so
accounting marks are the caller's problem, which quietly limits how useful
arbitrary-date PnL is out of the box. And `defillama.py` is a single point of
failure for every fiat number the package produces, with no fallback and no
manual override.

### Modules

```
prices/store.py                the price cache port + an in-memory backend
prices/historian.py            Asset + instant -> Money, through the store
prices/oracles/coingecko.py    the second aggregator
prices/oracles/manual.py       a caller-supplied override, highest precedence
prices/oracles/onchain_amm.py  spot from a pool, for what no aggregator lists
```

`prices/` goes to three modules, `prices/oracles/` to four. Both under the cap.

### Interface detail

The oracle seam already exists in `inquirer.py`; read it and extend it rather
than inventing a second one. Every new oracle satisfies the same structural
protocol `defillama.py` does.

**Precedence is declared, ordered and tested**: `manual` beats everything, then
`defillama`, then `coingecko`, then `onchain_amm`. A fallback is only taken when
the higher-precedence oracle **does not have** the asset, never when it returns
a suspicious number. Silently preferring a different source because a price
looked wrong is how an aggregator produces numbers nobody can reproduce.

`store.py` is a port plus a memory backend, following `ledger/port.py` in shape:
a structural `Protocol`, so a host can hand in Redis or Postgres without this
package growing a dependency. It is keyed by `(asset_id, instant)` at a declared
resolution, and the resolution is part of the contract, not an implementation
detail: a mark asked for at 12:00:31 and a mark asked for at 12:00:59 must
either be the same cache entry or provably different ones. State which, in
DECISIONS.md, and test the boundary.

`historian.py` takes an asset and a millisecond-epoch instant and returns
`Money`, consulting the store first. A cache hit makes **no** request, and the
test proves it by counting transport calls, the way
`tests/contract/test_embedding.py` proves its no-op.

`coingecko.py` is **keyless by default and degrades to stated-absent** without a
key, the way the Etherscan key already works. No key configured means that
oracle is not in the chain, said out loud, never silently skipped. It is also
the weakest of the three: CoinGecko and DefiLlama list substantially the same
assets, so a failover between them covers "one is down" and does nothing for
"neither lists this token". The two that change what the package can do are the
other two, and `onchain_amm.py` is the one that needs no vendor at all.

`onchain_amm.py` reads a pool through phase 11's `reader.py`, which is why this
phase follows that one. It prices in the pool's quote asset and converts, and
every step of that conversion is exact `Decimal` or `Fraction`, never float.

An asset no oracle can price comes back `None` with a `data_quality` note.
Rule #8. There is no zero.

### Done when

`tests/golden/test_phase12_historian.py`: a past-instant mark for a known asset
resolves from a recorded feed; the second identical lookup makes zero requests;
each oracle's absence hands off to the next in the declared order; a manual
override wins over a live aggregator; an asset no oracle lists comes back
declared-unpriced and the caller can tell that apart from "worth nothing".

---

## 6. Phase 13: the decoder learns transfers

**Closes half of #8.**

`decode/` ships `models.py` and `pipeline.py`. The README states the
consequence: `acts[]` is always one act and `protocol` is always `None`, so a
Uniswap swap and a plain transfer are indistinguishable downstream. §3.2's
`ActionItem` deferred-instruction design, the mechanism that resolves
protocol-event-before-Transfer ordering without a second pass, has no
implementation at all.

This is the largest single issue in the release and it is split across two
phases. Phase 13 builds the machinery and the three transfer decoders, which
are the ones every chain needs. Phase 14 builds protocol attribution on top.

### Modules

```
decode/rules.py                     the rule engine: match a log, emit acts
decode/acts.py                      act construction and act-level invariants
decode/action_items.py              the deferred-instruction mechanism
decode/protocols/registry.py        name -> handler, by topic0 and address
decode/protocols/transfer/erc20.py  Transfer(address,address,uint256)
decode/protocols/transfer/native.py value-carrying calls and internal transfers
decode/protocols/transfer/nft.py    ERC-721 and ERC-1155
```

### Interface detail

Read `decode/pipeline.py` and `decode/models.py` first. The pipeline exists and
this phase feeds it; it does not get rewritten. `pipeline.py:47,109` note that
every `value` and `price` on a decoded transaction is `None`, which phase 14
changes, not this one.

`action_items.py` implements SPEC §3.2's deferred instruction (SPEC.md:356).
The problem it solves: a protocol event can appear in a log **before** the
`Transfer` it explains, so a single forward pass cannot attribute the transfer
when it sees it. An action item is a deferred instruction a handler emits
saying "when a transfer matching this shape arrives, treat it as mine". One
pass, no second scan. The invariant to test hardest: an action item that is
never claimed must not silently vanish, and must not silently claim a transfer
it does not match.

The registry keys handlers on `topic0` and, where a protocol needs it, on
contract address. Two handlers claiming the same log is a declared conflict, not
a race: resolution order is stated and tested.

`acts.py` owns the invariant that makes `acts[]` worth having: the acts of a
transaction must account for its parts. A decode that attributes half a swap and
drops the rest is exactly the success-shaped failure loop.md's report-honesty
lens exists to find, so the act set carries whether it is complete, and an
incomplete attribution says so.

### Done when

`tests/golden/test_phase13_decode_transfer.py`: a recorded ERC-20 transfer, a
native send and both an ERC-721 and an ERC-1155 transfer each decode to a
`Transaction` whose `acts[]` names the act rather than defaulting to one opaque
act; an action item emitted before its transfer is claimed in a single pass; an
unclaimed action item is reported, not dropped; and a log two handlers both
claim resolves by the declared order.

---

## 7. Phase 14: protocol attribution and enrichment

**Closes the rest of #8.**

### Modules

```
decode/protocols/amm/uniswap_v2.py      Swap, Mint, Burn
decode/protocols/amm/uniswap_v3.py      Swap, IncreaseLiquidity, DecreaseLiquidity
decode/protocols/amm/curve.py           TokenExchange and its variants
decode/protocols/lending/aave.py        Supply, Withdraw, Borrow, Repay
decode/protocols/lending/compound.py    Mint, Redeem, Borrow, RepayBorrow
decode/protocols/lending/morpho.py      Supply, Withdraw, Borrow, Repay
decode/protocols/staking/lido.py        Submitted, and the stETH rebase
decode/protocols/staking/rocketpool.py  Deposit and rETH mint
decode/protocols/staking/solana_stake.py  stake delegate and deactivate
decode/enrich.py                        value and price on a decoded tx
```

`decode/protocols/amm/`, `lending/` and `staking/` each hold three modules.
`decode/` itself reaches six. All under the cap.

### Interface detail

Each handler is data plus one match function, following the shape
`positions/adapters/` established: the fork-helper pattern there exists because
a protocol integration should be a table, not a class hierarchy. A new handler
that needs to override behaviour is a signal the registry seam is wrong, and
that is a finding to report rather than to work around.

`protocol` on a decoded transaction is the **DefiLlama slug**, the same join key
`positions/` uses. This matters more than it looks: it is what lets a decoded
swap and a resolved position agree on what protocol they belong to, and two
different spellings of "uniswap-v3" would be the identity defect this project
has already fixed once.

`enrich.py` attaches `value` and `price` using phase 12's historian at the
transaction's own timestamp, not at now. A transaction from 2021 priced at
today's spot is a wrong number that no test currently catches. Where the
historian declares an asset unpriced, `value` stays `None` and the transaction
says why. Rule #8 again, and this is the place a hurried implementation
defaults to zero.

`solana_stake.py` decodes Solana stake instructions, which needs phase 17's
Solana work for the transaction fetch. Its handler is written here and its
recorded vectors land here; if the fetch is not ready, the order says so and
the handler is tested against a committed instruction fixture.

### Done when

`tests/golden/test_phase14_decode_protocols.py`: a recorded Uniswap V2 swap, a
Uniswap V3 swap, an Aave supply and borrow, a Compound redeem and a Lido submit
each carry the right `protocol` slug and a multi-act `acts[]` describing what
happened; `value` and `price` are attached at the transaction's own timestamp;
an asset the historian cannot price at that instant leaves `value` `None` with a
stated reason; and the slug on a decoded swap equals the `adapter_id` the
matching position adapter uses.

---

## 8. Phase 15: the per-resource surface, and projections that are pure

**Closes #9, #10, #12.**

The aggregate Plaid-shaped `/crypto/sync` path exists, but the per-resource
reads a client uses to drill into one account, its holdings, its transactions
or its positions do not. `PATCH /users/me/external_id` was never built, so
`external_user_id` is fixed at connection time with no migration path: the exact
footgun SPEC §7 names at SPEC.md:538. And the Plaid envelope lives in
`api/wire.py`, so Plaid-shaped output cannot be reached without FastAPI.

### Modules

```
project/plaid.py               the Plaid envelope, pure
project/native.py              the richer native projection, pure
api/routes/accounts.py         GET /accounts, GET /accounts/{id}
api/routes/holdings.py         GET /accounts/{id}/holdings
api/routes/transactions.py     GET /accounts/{id}/transactions
api/routes/positions.py        GET /accounts/{id}/positions
api/routes/webhooks.py         the webhook routes, out of admin.py
api/routes/users.py            PATCH /users/me/external_id
```

`api/routes/` reaches nine modules: `auth`, `connections`, `sync`, `admin`,
plus these five. Under the cap, with one slot left, so a tenth route module in
a later release grows a subfolder.

`api/routes/users.py` is not in §3.2 and goes to `SHIPPED_BUT_UNDECLARED`:
§3.2's route list predates the `/users` table SPEC §7 adds at line 537, and the
`GET /users` and `GET /users/me` halves currently live in `admin.py`. Moving
them to `users.py` alongside the new PATCH keeps one resource in one module,
and it is what pulls `admin.py` back under budget once the webhook routes leave
it too.

### Interface detail

**#12 is a move, not a rewrite.** `api/wire.py` holds the working envelope
today: `added`/`modified`/`removed`, `next_cursor`, `has_more`, the synthetic
negative-quantity Holdings. That logic is correct and pinned by
`tests/contract/test_projection_invariant.py`. It moves to `project/plaid.py`
verbatim where it can, and `api/wire.py` stays as the HTTP adaptor. The
projection invariant test must pass **unchanged**: if it needs editing, the move
changed behaviour and that is a finding.

The gate that makes the move worth doing: `project/plaid.py` must be importable
with **no web framework available at all**. Test it in a subprocess with an
import hook that raises on `fastapi` and `starlette`. A passing import proves
what `test_layering.py` can only assert about source text.

`external_id` migration is the part with teeth. `external_user_id` participates
in id derivation, so changing it is not a column update. State in DECISIONS.md
and in CHANGELOG.md exactly what a PATCH does to already-persisted rows: which
ids re-derive, which do not, and what a host must do. If ids re-derive, the
route either migrates the rows in the same transaction or refuses. It does not
leave a tenant half-renamed. SPEC §7 calls immutable-forever a footgun; a
migration that silently orphans data is a worse one.

Per-resource reads are tenant-scoped through the existing `api/deps.py`
dependencies, and the cross-tenant isolation suite gains a case per new route.
Every new route that takes an id is an enumeration surface, and
`tests/contract/test_tenant_isolation.py` is the file that already knows how to
try to leak.

### Done when

`tests/contract/test_phase15_resource_routes.py`: one tenant drills from
`/connections/{id}` into accounts, holdings, transactions and positions, and
every payload is byte-identical to the same `project/` call made directly;
`project/plaid.py` imports clean in a subprocess where importing `fastapi`
raises; `PATCH /users/me/external_id` renames a tenant and every id that
derives from `external_user_id` is either migrated in the same transaction or
the PATCH is refused with a stated reason; and a second tenant gets 404, not
403, on every new route (existence is information).

---

## 9. Phase 16: persistence for everything that is not the ledger

**Closes #13, #14.**

`ledger/` has two backends. Every other store is memory-only: `tenancy/store.py`,
`keys.py`, `quota.py`, `audit.py`, the webhook endpoint, delivery and dead-letter
stores, and `embed/state.py`, whose docstring says a SQL-backed implementation
is deliberately deferred. A restart loses every project, scoped key, quota
window, audit entry, webhook registration and undelivered event. The audit log
in particular is a compliance surface that does not survive a process bounce.

### The gate change this needs, stated first

Issue #13's suggested fix is illegal: `test_layering.py::test_no_orm_outside_ledger_backends`
exempts `ledger/backends/` and nothing else. Putting the stores under
`ledger/backends/` instead is worse, because `webhooks` already imports `ledger`
and the reverse edge would cycle a graph the same gate asserts is acyclic.

**The resolution:** the ORM exemption becomes a declared list of per-domain
persistence packages, `ledger/backends/`, `tenancy/backends/`,
`webhooks/backends/` and `embed/backends/`. Each imports only its own domain,
so no new domain edge appears and the graph stays acyclic. `test_placement.py`
still requires table definitions to live in a `models.py`, so each new package
has one, exactly as `ledger/backends/models.py` does.

That edit to `tests/style/test_layering.py` is made by the orchestrator in the
preparatory commit, not by an agent mid-phase: `tests/style/` is
`pattern-sweeper`'s exclusive territory and a second writer there breaks
loop.md invariant 1. It is recorded in DECISIONS.md as a pinned decision with
its reason.

### Modules

```
tenancy/backends/models.py     tables for project, key, quota window, audit row
tenancy/backends/sqlmodel.py   the four tenancy stores, SQL-backed
webhooks/backends/models.py    tables for endpoint, delivery, dead letter
webhooks/backends/sqlmodel.py  the three webhook stores, SQL-backed
embed/backends/models.py       the sync-state table
embed/backends/sqlmodel.py     SyncStatePort, SQL-backed
```

Every one of these is additive: the ports are already structural `Protocol`s.
If a port turns out not to be substitutable, that is a seam finding about the
port, and it is more valuable than the backend.

### Interface detail

Every existing store contract test becomes parametrized over both backends.
That is the point of the phase and it is where the bugs are: a memory store
that iterates a dict and a SQL store that iterates a query do not order
identically, and any test that passed on insertion order was passing by
accident.

Money columns are `NUMERIC`, never float, and the round trip is tested by exact
`Decimal` equality. `tests/style/test_schema_ddl_is_current.py` and
`scripts/emit_schema.py` already own the emitted DDL, so the new tables join
that pipeline.

**Postgres, #14.** `ledger/backends/sqlmodel.py` should work on Postgres through
the same port but only sqlite is exercised anywhere. Three specific risks: the
money columns' `NUMERIC` versus sqlite's loose typing, the cursor's
last-modified ordering under concurrent writers, and `ON CONFLICT` upsert
semantics in `ledger/upsert.py`. The ledger contract suite runs against a
Postgres service container in CI, opt-in by marker so the default offline suite
never reaches for a socket. The autouse socket block in `tests/conftest.py`
means the Postgres tests must explicitly opt out of it, and doing that by
marker rather than by weakening the fixture is the difference between one
guarded exception and no guard at all.

### Done when

`tests/contract/test_phase16_durable_stores.py`: every store's contract suite
passes against both backends by parametrization; a new session over the same
sqlite file preserves projects, scoped keys, quota windows, audit rows, webhook
endpoints and undelivered deliveries; an audit row survives a process bounce
with its IP and timestamp intact; ordering is explicit in every query that a
test asserts an order on. Plus a CI job running the ledger contract suite green
against Postgres, with the money round trip asserted by exact `Decimal`
equality and the upsert path asserted on both engines.

---

## 10. Phase 17: Cosmos, and Solana that decodes

**Closes #5, #4.**

`chains/` seeds EVM, Bitcoin and Solana. Cosmos is absent, which is the main
thing standing between the current chain set and "multi-chain". `GET /coverage`
reports it honestly rather than silently under-reporting net worth, so this is a
capability gap and not a correctness bug. And `sources/solana/rpc.py:5` records
its own deferral: Solana is balances and signature history only, with no
transaction decode.

### Modules

```
chains/cosmos.py               the Cosmos family and its seeded chains
```

`chains/` reaches five modules. `sources/solana/` gains none: #4's
`helius.py` half was dropped and the decode goes through the keyless
endpoint `rpc.py` already posts to.

### Interface detail

Cosmos ids follow CAIP-2 (`cosmos:cosmoshub-4`) and CAIP-19 for denoms,
including IBC denoms, whose identity is a hash of the trace path. An IBC denom
that resolves to two different asset ids depending on which chain observed it is
the identity defect this project has already fixed once; the derivation is
pinned in DECISIONS.md before any code is written, and a golden vector
cross-pins it.

`bech32` address handling is stdlib-only, like `sources/bitcoin/encoding.py`.
That module already exists and its reference-vector discipline is the model.

Solana transaction decode is reached with `getTransaction` and
`{"encoding": "jsonParsed"}`, on the same keyless `mainnet-beta` URL
`sources/solana/rpc.py:139` already posts to for token accounts. No second API
key, and `docs/authentication.md`'s one-row table stays true. Helius would have
bought convenience and pre-decoded protocol labels, not access, and a protocol
attribution taken from a vendor is one no committed vector can reproduce.

Solana transaction decode reuses phase 13's rules and registry. An SPL transfer
decodes to the same act shape an ERC-20 transfer does, which is the test worth
writing: two chains, one act vocabulary, or the vocabulary is wrong.

### Done when

`tests/golden/test_phase17_cosmos_solana.py`: a Cosmos address on a seeded chain
yields balances with correct CAIP-19 ids including one IBC denom, and the denom's
id is identical whichever chain observed it; `GET /coverage` reports Cosmos as
covered; a recorded Solana transaction decodes to typed acts with an SPL
transfer producing the same act shape an ERC-20 transfer does; and with no
Helius key configured, every Solana path behaves as it does today.

---

## 11. Phase 18: drift shows up as a failing job

**Closes #15.** (#11 was closed as not planned; see below.)

### What happened to `jobs/`

This phase was drafted to build `jobs/` and answer #11 with "build it". Reading
the tree answered it the other way. Four of the five declared modules already
ship: `embed/sync.py:182` is the budgeted two-phase backfill, `embed/dispatch.py:22`
the shared-budget refresh with per-connection containment, `positions/discovery.py:55`
the address-blind discovery pass, and `decode/models.py:153,185` already persist
the `decoder_version` a reprocess would select on. The fifth, `scheduler.py`,
cannot be built as asked: a scheduler that owns no event loop and starts no
thread, which the embedding posture requires, is a cron-expression evaluator.
That is a utility, not a domain, and shipping it would advertise a background
worker this package does not have.

So `jobs/` left SPEC §3.2, `ALLOWED_IMPORTS` and `IO_DOMAINS`, and #11 is
closed. The one real need it named, reprocess after a decoder improves, is a
query over a field that already exists and belongs beside the decoder. It is
phase 14's, not a package of its own.

### #15, live reconciliation

Every I/O path in the suite replays committed cassettes, which is what makes
`pytest` green offline in a `--network none` container, the SPEC §13 acceptance
criterion. That stays true for the default suite, without exception.

The reconciliation job is opt-in and credentialed, nightly or manual. It hits
the real endpoints and diffs against the cassettes, so provider drift, a changed
Etherscan V2 response shape or a renamed DefiLlama field, shows up as a failing
job rather than a bug report. Cassette re-recording is the fix path, and
`auradefi.testing.cassettes.Recorder` shipped in 0.1.2 exactly for it.

The job must fail loudly on a diff and must never rewrite a cassette by itself.
A job that silently re-records is a job reporting success while the contract
moved, which is the `backfill_complete` defect shape in a CI file.

### Done when

`scripts/reconcile_live.py` and `.github/workflows/reconcile.yml` run against
real endpoints on a schedule, fail on any diff from the committed cassettes,
never write one, and are unreachable from `.venv/bin/pytest`.

---

## 12. Explicitly not in scope for 0.2.0

- **Four position adapters `DECLARED_BUT_ABSENT` names that no issue covers:**
  `positions/adapters/erc4626.py`, `amm/curve.py`, `lending/compound.py`,
  `staking/native.py`. §3.2 declares them and no golden vector covers them.
  They stay in the inventory and stay named in README's gap section. Phase 14
  builds the *decode* handlers for Curve and Compound, which is a different
  thing from a position adapter, and the README must not blur the two.
- **Async.** Nothing in this release adds an async surface. `jobs/` computes
  what is due; it does not run an event loop.
- **New third-party dependencies.** Including for keccak, ABI encoding,
  bech32 or Postgres drivers beyond what `[sql]` already declares.
- **Re-recording existing cassettes.** Phase 18's job reports drift; it does
  not fix it, and a drift that needs fixing is a separate change with a human
  reading the diff.

---

## 13. Per-phase protocol: non-negotiable

For each phase, in order:

1. **Preparatory commit, by the orchestrator.** Stub the phase's modules with a
   docstring and `raise NotImplementedError`; remove exactly those entries from
   `DECLARED_BUT_ABSENT`; add the undeclared ones to `SHIPPED_BUT_UNDECLARED`;
   make any style-gate change this document names. Commit it. Then record the
   baseline. The mirror-rule failures the stubs cause are inherited state, which
   is what loop.md invariant 8 is for.
2. **Run the loop**: `Workflow({ name: 'phase-build', args: { phase: N } })`.
3. **Read the report.** A `blocker` contradiction from the spec audit is a
   question for a human, not a thing to route around.
4. **Suite, then release gate.** `.venv/bin/pytest` green, `bash
   scripts/release_check.sh` PASSED. The suite before the gate, because the gate
   is slow and a red suite makes it pointless.
5. **Commit and push.** One commit per phase, on `main`, naming the issues it
   closes so GitHub closes them.

The version bump to 0.2.0, the `[0.2.0]` CHANGELOG section, the README gap
rewrite and the STATUS.md rewrite happen **once**, in the last phase. A version
bumped early is a version that lies for seven phases.
