# conduit: an open-source, multi-tenant crypto data aggregator

> Working name; check PyPI before committing. Python, no frontend.
> This document becomes the repo's `docs/internal/SPEC.md`.
> **Shipping name: `auradefi`** (decided 2026-08-02, `conduit` is taken on PyPI; see `docs/internal/DECISIONS.md`).

---

## 1. Context

**The market cleared out in the last twelve weeks.**

| Product | Status |
|---|---|
| **Zapper** | Shut down **3 Aug 2026** after 7 years. 2M+ MAU, $13B+ volume. CEO Seb Audet: retail expects portfolio tracking free; multichain indexing costs are relentless. |
| **Dune SIM** | Sunset **1 Aug 2026**. Dune: *"it pulled focus from where we believe Dune wins."* |
| **OneBalance** | Toolkit API deprecated **18 May 2026**. `be.onebalance.io` now 404s. |
| **Zapper Studio** | Archived Jan 2024. Replaced by no-code "Position Interpreters". |
| **LlamaFolio** | Last commit **May 2024**. 59 stars, 6 watchers. Effectively dead. |

Three of Zerion's seven migration targets died this quarter. Zerion is running a land-grab (seven migration guides, an installable agent-skill, `npx skills add zeriontech/zerion-api-migration`) against a displaced customer base that is in motion **right now**.

**The gap, stated precisely.** From the survey of DeBank, Zerion, CoinStats, Moralis, Alchemy, Vezgo, Allium and GoldRush:

> Nothing represents "a person with five wallets, two exchanges and a hardware ledger". Only Vezgo's `loginName` → user token → `accountId` chain does that, and Vezgo has almost no DeFi depth. **That combination, Vezgo's identity model with DeBank's position depth, does not exist as a product.**

Every onchain-native API (DeBank, Zerion, Moralis, Alchemy, GoldRush, Allium, the late Dune SIM) takes a **raw address per call** and stores nothing. That single choice forecloses cost basis, CEX aggregation, and any notion of "this user". Meanwhile the one vendor with a real tenancy model gates its own address endpoint behind Enterprise and caps free history at 30 days.

**What we build:** the tenancy model of Vezgo, the position depth of DeBank/Allium, the transaction decomposition of Zerion, the adapter economics of LlamaFolio, and **Plaid's wire format**, so crypto merges with Plaid bank and exchange data in one schema downstream.

### 1.1 Licence position

| Source | Licence | What we may do |
|---|---|---|
| **rotki** | AGPLv3 | **Read for architecture only. Copy nothing.** Clean-room. AGPL §13 triggers on network use: fatal for a hosted product. |
| **LlamaFolio** | GPL-3.0 | Read for design. Don't copy. GPL is inherited on distribution. |
| **Zapper Studio** | **BUSL-1.1 → MIT** | Change Date was **2024-04-20** and has passed; by the licence's own terms every published version is now MIT. Note `package.json` says MIT and GitHub reports `NOASSERTION`: **the `LICENSE` file governs**. Attribution required if lifted. |

**Ship Apache-2.0.** Maximises adapter contributions, adds a patent grant. Zapper's own post-mortem is the argument: BUSL with `Additional Use Grant: none` bought them nothing and cost them the ecosystem that would have shared the maintenance burden. Contributors who cannot *run* the thing do not maintain it.

---

## 2. Non-negotiable design decisions

Each is a one-line rule with a named casualty.

| # | Rule | Why |
|---|---|---|
| 1 | **Money is a tagged decimal string.** `{"amount": "-741.027368947745798389", "currency": "USD"}` | Allium's best idea. Sim, GoldRush, OneBalance and **Plaid itself** put fiat in JSON doubles. Plaid's `quantity`/`amount`/`price` are `format: double`: ~15–17 significant digits against 18-decimal tokens. Silently corrupts exactly the largest balances. |
| 2 | **Never emit a JSON integer for a raw amount.** | Allium ships `raw_balance` (integer) beside `raw_balance_str`. Any wei-scale value is past `Number.MAX_SAFE_INTEGER` before it reaches a client. |
| 3 | **Asset IDs are deterministic CAIP-19 and permanently stable.** | Zerion's docs say verbatim: *"There is a non-zero probability that IDs may change in the future."* Disqualifying for anyone persisting portfolio history, and free to beat. |
| 4 | **Every movement is a `part`. No exceptions for EVM.** | Vezgo's deepest flaw: on EVM/Solana, `parts[]` holds only the native asset; ERC-20 movements hide in `misc.tokenTransfers[]`, which is **not in their OpenAPI spec**, uses a different vocabulary, and carries **numeric, negative-when-sent** amounts. Their normalised model stops applying on exactly the chains that matter. |
| 5 | **Golden fixture tests pinned to a block height, per adapter.** | LlamaFolio: **zero** `.test.ts` files in 3,422. Zapper Studio: 3 test files across 1,010 fetchers, none checking a number, typecheck disabled in CI. Both issue trackers are wall-to-wall silent wrongness. The clearest cause of death. |
| 6 | **Multi-tenancy is designed in, never retrofitted.** | rotki's maintainers' own position: one container per user. A process-singleton orchestrator plus password-derived SQLCipher is a rewrite, not a patch. |
| 7 | **Version the decoder; expose reprocess.** | Vezgo has no re-decode path: improving your decoder doesn't improve stored rows. Their answer is "delete the connection (all ids change) or email us." |
| 8 | **Signed webhooks with durable delivery from day one.** | Vezgo authenticates webhooks by **source-IP allowlist**: unusable behind most PaaS ingress. Zerion retries 3× over ~60s then drops permanently, and requires **manual human whitelisting** of each callback URL. |
| 9 | **Return the liquidity number, not just a spam boolean.** | The threshold is a product decision, not a vendor decision. Sim gives `pool_size` + `low_liquidity`; Allium gives `total_liquidity_usd`. |
| 10 | **Publish a per-capability coverage matrix as data.** | Zerion's guides all say "60+ EVM chains"; their supported-blockchains page lists **39**, with DeFi on ~24 and NFTs on ~22. Honest coverage is cheap and is a direct credibility differentiator. |
| 11 | **Library first, service second.** The HTTP API is a thin shell over an importable core. | Every incumbent is a hosted API you can only reach over the wire. A Python host with its own backend should `import conduit` and pay no serialisation or network cost. This also forces the core to stay free of web-framework assumptions. |
| 12 | **Storage is a port, not a hard dependency.** | An embedding host already has a database, a session, and a migration story. Forcing ours on it makes the library unadoptable. Ship a default implementation; let a host bind its own. |

---

## 3. Architecture

### 3.1 The object graph

Reconciling Vezgo's model with Plaid's: they line up almost exactly, which is the load-bearing insight of this design:

```
Organisation                    (billing + quota boundary)
 └── Project ── api_key(s)      scoped: accounts:read, sync:trigger, users:admin, …
      └── EndUser               external_user_id (opaque, get-or-create)
           └── Connection       ≡ Plaid ITEM. One credentialed-or-watched source.
                │               (an address, an xpub, an exchange key)
                └── Account     ≡ Plaid ACCOUNT ≡ Vezgo WALLET.
                     │          One (connection × chain) or one exchange sub-account.
                     ├── Holding[]           ≡ Plaid Holding
                     ├── Position[]          our DeFi extension
                     └── Transaction[]       ≡ Plaid InvestmentTransaction
```

**Vezgo's `Account` is Plaid's `Item`; Vezgo's `wallet` is Plaid's `Account`.** Once you see that, the two models are the same model. An xpub connection yields one Account; an EVM address connection yields one Account per chain, exactly as Plaid's Item→accounts 1:n relation intends.

Plaid already sanctions all of this: `institution_id` is **nullable**, documented as null *"for non-institution Items"*, and there are two crypto subtypes under `type: investment`:

- **`crypto exchange`**, *"Standard cryptocurrency exchange account"*
- **`non-custodial wallet`**, *"A cryptocurrency wallet where the user controls the private key"*

Plaid made the modelling decision for us. **Use `type: investment` for every wallet, never `depository`.**

### 3.2 Package layout (CUPID / UNIX)

Domains are packages. Files target 300 lines, 400 hard. No directory holds more than 10 non-`__init__` modules: past that it grows subfolders. Tests mirror the source tree exactly.

```
src/auradefi/
  __init__.py  clock.py  config.py  errors.py        ← foundation only, nothing else flat

  money/          quantity.py  fiat.py  decimal_json.py
  assets/         caip.py  models.py  registry.py  groups.py  spam.py  external_ids.py
  chains/         registry.py  families.py  evm.py  bitcoin.py  solana.py

  sources/        __init__.py (Source protocol)
    evm/          etherscan.py  rpc.py  multicall.py  logs.py
    bitcoin/      esplora.py  xpub.py  utxo.py
    solana/       rpc.py  helius.py  spl.py

  prices/         inquirer.py  historian.py  store.py
    oracles/      defillama.py  coingecko.py  onchain_amm.py  manual.py

  decode/         pipeline.py  rules.py  action_items.py  enrich.py  acts.py
    protocols/    registry.py
      transfer/   erc20.py  native.py  nft.py
      amm/        uniswap_v2.py  uniswap_v3.py  curve.py
      lending/    aave.py  compound.py  morpho.py
      staking/    lido.py  rocketpool.py  solana_stake.py

  positions/      protocol.py  registry.py  discovery.py  resolve.py  drill.py
    adapters/     tokens.py  erc4626.py
      amm/        uniswap_v2.py  uniswap_v3.py  curve.py
      lending/    aave.py  compound.py
      staking/    liquid.py  native.py

  ledger/         port.py  models.py  cursors.py  upsert.py  reorg.py
    backends/     sqlmodel.py  memory.py
  accounting/     lots.py  fifo.py  lifo.py  hifo.py  acb.py  pnl.py
  tenancy/        models.py  tokens.py  keys.py  quota.py  audit.py
  project/        plaid.py  native.py  scalar.py        ← pure output projections
  webhooks/       models.py  sign.py  deliver.py  replay.py

  api/            deps.py  errors.py
    routes/       auth.py  connections.py  accounts.py  holdings.py
                  transactions.py  positions.py  sync.py  webhooks.py  admin.py

  jobs/           scheduler.py  discover.py  refresh.py  reprocess.py  backfill.py

tests/
  <mirrors src/ exactly>
  style/          test_size.py  test_structure.py  test_placement.py  test_layering.py
  golden/         <per-adapter fixtures, pinned block heights>
  cassettes/      <recorded HTTP, committed>
```

**Style gates** (`tests/style/`):
- `test_size.py`, 300 soft / **400 hard, no allowlist**
- `test_structure.py`, foundation modules asserted with `==`; every domain is a package; `MAX_DIR_FILES = 10`
- `test_placement.py`, table definitions only in a `models.py`; tests mirror source
- `test_layering.py`, **the important one.** `sources/` may not import `positions/`; `assets/` may not import `prices/`; `project/` may not import anything with I/O; **nothing outside `api/` may import a web framework, and nothing outside `ledger/backends/` may import an ORM.** The dependency graph is acyclic and enforced.

That last gate is what makes rule #11 and #12 true rather than aspirational.

### 3.3 Layer contract

```
sources/     raw chain bytes → typed records.        Knows: HTTP, RPC, ABIs.
                                                     Knows nothing about positions or fiat.
assets/      identity. CAIP-19 in, Asset out.        No I/O beyond its own registry.
prices/      Asset + timestamp → Money.              May call sources/ for AMM quotes.
decode/      raw tx + logs → Transaction(parts[]).   Consumes sources/, assets/, prices/.
positions/   address + chain → Position[].           Consumes sources/, assets/, prices/.
ledger/      persistence port + cursors + reorg.     Consumes everything above.
accounting/  lot ledger → PnL.                       Pure over ledger reads.
project/     internal model → wire format.           PURE. No I/O, no DB, no framework.
api/         HTTP. Thin.                             Consumes ledger/, tenancy/, project/.
```

`project/` being a **pure, I/O-free projection** is deliberate: the entire output contract becomes unit-testable from fixtures, and a consumer wanting the richer native model can bypass it.

---

## 4. The core data model

### 4.1 Money and quantity

Two types, both immutable, both string-serialised. Rules #1 and #2 made concrete.

```python
@dataclass(frozen=True, slots=True)
class Quantity:
    raw: int          # base units. Python int: arbitrary precision, no ceiling.
    decimals: int

    def as_decimal(self) -> Decimal: ...
    def __str__(self) -> str: ...    # exact decimal string, never scientific notation

@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str     # ISO 4217, or a CAIP-19 for crypto denomination
```

Wire form, borrowing Zerion's four-field shape (raw for arithmetic, float for display, exact string for correctness, decimals inline so no second lookup):

```json
"quantity": { "raw": "1234567890123456789", "decimals": 18,
              "numeric": "1.234567890123456789", "float": 1.2345678901234568 }
"value":    { "amount": "4321.55", "currency": "USD" }
```

`float` is **display-only and documented as lossy.** `raw` is a *string* in JSON, an `int` in Python.

> Zerion's `float` legitimately differs from `raw / 10^decimals` for Solana Token-2022 ScaledUiAmount tokens. Handle it in `chains/solana.py`; don't assume the identity holds.

### 4.2 Asset identity: CAIP-19, deterministic and stable

```
eip155:1/erc20:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48    USDC on Ethereum
eip155:8453/slip44:60                                        native ETH on Base
bip122:000000000019d6689c085ae165831e93/slip44:0             BTC
solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/token:EPjFWdd5…      USDC on Solana
```

No `0xeeee…`/`0x0`/`"native"` sentinel zoo: every surveyed vendor invented a different one. CAIP-2 chain ids mean no `eth-mainnet` (GoldRush) vs `ethereum` (Allium) vs `1` (Dune SIM) translation table.

**Chain-agnostic asset with per-chain implementations** (Zerion's best structural idea):

```python
class Asset:            # "USDC", chain-agnostic
    id: str             # deterministic: hash over the sorted implementation set
    symbol: str; name: str; icon: str | None
    implementations: list[Implementation]
    external_ids: dict[str, str]      # coingecko, cmc: Vezgo ships none, everyone rebuilds it
    asset_class: AssetClass
    flags: AssetFlags

class Implementation:
    caip19: str
    chain_id: str
    decimals: int       # ← ON THE IMPLEMENTATION, not the asset. USDC is 6 on both
                        #    Ethereum and Polygon, but bridged assets genuinely differ.
```

Consequence to hold onto: **you cannot format an amount without knowing which chain it is on.** That is correct, and it is why `decimals` travels inside `Quantity`.

Addressable **both ways**, on every filter: by canonical id or by `chain:address`. Zerion does this and it is why their filters are usable.

`AssetClass`: one enum spanning families, after Allium's polymorphic token `type`:
`native | token | stablecoin | lp_token | receipt_token | debt_token | vault_share | nft | wrapped | derivative | perp`

**Asset groups** (OneBalance's `ob:usdc`): one "USDC" row across seven chains, with per-chain breakdown underneath, and an explicit `single` fallback bucket so nothing falls out of the model. Aggregation is only sound when implementations share `decimals`: enforce that. Zerion punts this to the client; doing it server-side is a real differentiator.

**Spam** (rotki's layered model, plus rule #9): heuristic score → distributed marks → per-user ignore list → **per-user whitelist for false positives**. Ship `liquidity_usd` and `holder_count` as numbers alongside `is_spam` so the consumer picks the threshold. rotki's scar: a transient source failure once wiped previously-detected tokens. Make detection additive, never destructive.

### 4.3 Positions

Zapper Studio's ontology is the crown jewel and it survives its author:

**AppToken**. The position *is* a fungible token (aToken, cToken, stETH, UNI-V2 LP, ERC-4626 share). It has a price, a supply, and a `price_per_share`. Because it is priceable, **it composes**: a Curve LP inside a Convex wrapper inside a vault resolves transitively without every integrator re-deriving it.

**ContractPosition**: a leaf. Not tokenised, so no price, no supply, no decimals. It can only be *valued*, per address, by summing its constituent token balances. Zapper's rule of thumb: if you cannot add it to MetaMask, it is a contract position.

The asymmetry drives pricing, caching and the balance pipeline. Note the Aave case: supply positions are AppTokens, Compound *borrow* positions are ContractPositions: one protocol splits across both.

**MetaType** on each underlying token: one small enum that removes the need for per-protocol schemas, and yields debt sign for free:

```python
class MetaType(StrEnum):
    WALLET = "wallet"; SUPPLIED = "supplied"; BORROWED = "borrowed"
    CLAIMABLE = "claimable"; VESTING = "vesting"; LOCKED = "locked"; NFT = "nft"
```

`BORROWED` flips value negative. A CDP is `SUPPLIED` ETH + `BORROWED` DAI. A farm is `SUPPLIED` LP + `CLAIMABLE` SUSHI. A lock is `LOCKED` CRV. One mechanism, no special cases.

**Two orthogonal classification axes** (Zerion), genuinely better than one flat enum:

- `position_type`, what state the asset is in: `wallet | deposit | loan | locked | staked | reward | investment`
- `protocol_module`, where in the protocol: `lending | liquidity_pool | yield | farming | staked | leveraged_farming | vesting | rewards | locked | nft_staked | deposit | investment`

Aave supply = `lending` + `deposit`. Aave borrow = `lending` + `loan`.

**Fix Zerion's two defects:**
1. **Signed values.** Zerion returns debt as a *positive* `value` on a `loan` row and never says whether the total nets it. Return signed values plus an explicit triple: `gross_assets`, `total_debt`, `net_worth`.
2. **Group totals.** Zerion returns LP legs as separate rows sharing `group_id` with **no per-group total**. The consumer must group and sum. Ship `PositionGroup` with `total_value`, `health_factor`, `ltv`, `liquidation_price` on the group, after Allium and DeBank's `health_rate`. LlamaFolio had the right instinct: a group is a **risk unit**, not a display unit.

Also carry, because Zerion admits it lacks all of these: `apy` (explicitly typed, APR vs APY, gross vs net, source, staleness, not Zapper's untyped `apy: number`), and for concentrated liquidity `tick_lower`, `tick_upper`, `in_range`, `unclaimed_fees`.

**Vault shares are valued by redemption, not by price feed**: call `previewRedeem`/`convertToAssets`. Quote what the user would actually get out.

### 4.4 Transactions

Vezgo's `parts[]` model is better than Plaid's single signed `amount`, and it is the base:

> **A transaction is a bag of signed movements. The `type` is a derived label over the shape of the bag.**

```python
class Transaction:
    id: str                     # deterministic: hash(chain_id, tx_hash, account_id)
    chain_id: str
    tx_hash: str
    status: TxStatus            # pending | confirmed | failed | reverted | replaced | dropped
    block_number: int | None
    initiated_at: int           # ms epoch, everywhere, always
    confirmed_at: int | None
    type: TxType                # DERIVED from parts. Never stored as ground truth.
    subtype: TxSubtype
    parts: list[Part]           # EVERY movement. No exceptions. (rule #4)
    fees: list[Fee]             # siblings of parts, never movements
    acts: list[Act]             # sub-operation decomposition
    protocol: ProtocolRef | None
    decoder_version: int        # (rule #7)
    data_quality: DataQuality

class Part:
    act_id: str | None          # FK into acts[]
    direction: Direction        # in | out | self
    asset_id: str               # CAIP-19
    quantity: Quantity
    value: Money | None
    price: Money | None         # historical, at execution
    from_address: str; to_address: str
    meta_type: MetaType | None
    other_parties: list[str]    # extra UTXO inputs/outputs
```

Three things this gets right that the incumbents don't:

**Fees are siblings, never movements.** They can never corrupt the trade legs. `fee.act_id` attributes a fee to a leg. Gas is denominated in a *different asset* than the trade (ETH for a USDC transfer), sometimes paid to a paymaster in a third asset, sometimes charged on a *failed* transaction that moved nothing. Plaid's single nullable `fees: double` cannot express any of that. Mark network fees on *inbound* transfers `borne_by: "counterparty"` so naïve summing doesn't over-count; Vezgo gets this wrong.

**`acts[]` with `act_id` back-references** (Zerion's best idea, nearly unique to them). A real transaction often does several things: a multicall that swaps *and* claims *and* pays a UI fee. One top-level `type` cannot express it. A two-level tree flattened into parallel arrays handles ERC-4337 bundles, Solana Jito tips, and multicalls cleanly. It also **solves Plaid's swap problem natively**. Plaid's `InvestmentTransaction` has exactly one `security_id`, so a swap must become two unlinkable rows; acts give the linkage for free.

**Staking as a trade into a synthetic asset** (Vezgo, genuinely clever): `ETH` → `ETH.staked`. No new primitives, and staked balances fall out of the normal balance path.

`TxSubtype` covers what Vezgo refuses to normalise: they surface a DEX swap as `misc.isSwap: true`, *a boolean in an unstructured object*: `swap | lp_add | lp_remove | borrow | repay | liquidation | bridge_in | bridge_out | claim | approve | revoke | stake | unstake | nft_mint | nft_purchase | nft_sale | airdrop | reward | fee | …`

**`data_quality` is a first-class field**, expanding Vezgo's `misc.incomplete[]` (which almost nobody ships and which is a genuinely good idea):

```python
class DataQuality:
    incomplete: list[str]       # ["fiat_value", "counterparty"]
    confidence: float
    decoder_version: int
    sources: list[str]
```

`status` must be **real**, not permanently null. Vezgo ships six always-null fields in its public contract, including `transaction.status`, so you cannot tell pending from confirmed except by inferring from `confirmed_at`.

### 4.5 The decoder

rotki's architecture is the reference, and it is a **rule registry over receipt logs**, not a generic ABI decoder, not a trace differ.

Pipeline: `raw tx → gas + internal txs → per-log dispatch → enrich → post-decode → swap reconstruction → Transaction`.

A protocol decoder contributes dispatch tables, assembled once at startup:

| Hook | Purpose |
|---|---|
| `counterparties()` | declares protocol identities this module owns: **required** |
| `addresses_to_decoders()` | contract address ⇒ handler. Primary dispatch. |
| `decoding_by_input_data()` | 4-byte selector → topic → handler, for factory-deployed contracts |
| `enricher_rules()` | run over *already decoded* plain transfers to re-label them |
| `post_decoding_rules()` | after the whole tx is decoded; priority-ordered; assembles multi-leg ops |

Two mechanisms worth lifting outright:

**`ActionItem` deferred instructions.** A decoder that sees a protocol event *before* the corresponding ERC-20 Transfer log queues an instruction: *"when you next see a transfer of asset X for amount Y, retype it to Z with counterparty C."* Items flow forward through the log loop and are consumed on match, with an amount tolerance for protocols with rounding drift. Solves ordering **without a second pass**.

**Enrichers.** How one plain ERC-20 Transfer becomes "Deposit into Aave" *after the fact*.

**Registration is explicit, not filename magic.** Zapper's `@PositionTemplate()` decorator read the **call stack** to derive identity from the file path, and swallowed failures in a bare `catch { console.error(e) }`: a mis-named file silently produced a fetcher with `appId === undefined`. Declare registration in code.

**Zapper's own conclusion deserves a hearing.** After archiving Studio they pivoted to *event interpretation*: one community template describes **~10,000 transactions** (their figure), versus one adapter per protocol per chain. That is a far better leverage ratio, and it is the clearest signal anyone has about the adapter model's economics. Our `decode/` layer is event-interpretation-shaped and `positions/` is adapter-shaped: **watch which one earns its keep**, and let the ratio decide.

---

## 5. Position discovery: the scaling problem

This is where the money goes, and it is the reason both predecessors died.

### 5.1 The two-phase split (LlamaFolio's central idea)

| | `discover()` | `resolve()` |
|---|---|---|
| Runs | background cron, per adapter × chain | per user balance refresh |
| Knows the address? | **No** | Yes |
| Output | static contract descriptors, persisted | amounts attached to those descriptors |

The expensive part, enumerate 1,000+ Uniswap pairs, read reserves, resolve underlyings, price them, **does not depend on the address at all**. Zapper reached the same conclusion independently and cached position discovery on a 45-second refresh-ahead interval, explicitly to avoid a thundering herd.

### 5.2 Pre-filtering by interaction: the cost centre, stated honestly

**Only adapters whose contracts the user has actually touched are run.** An interaction is: the account sent a transaction to the contract, **or** received the token via a `Transfer` event.

LlamaFolio's query joins its contract table against a materialized view over token transfers keyed by holder. That view lives in **`evm-indexer`, a separate Rust service that is not in the repo**. Zapper did the same thing behind a closed endpoint and it is why their docs insist a position's address must be *the contract the user's transaction touched*.

> **Without an interaction index, a typical address runs all N adapters instead of a handful. This is the hard dependency that killed the open-source-ness of both predecessors.**

Three options, ascending in cost and independence:

1. **Explorer `txlist` + `tokentx` per address**. Cheap, no infrastructure, works today. Derive the touched-contract set from the address's own history. Bounded by explorer page limits.
2. **A `Transfer`-log index we run**. Postgres, one table, `(chain, contract, holder)`. Backfilled from logs. The real answer, and the largest single infrastructure line item.
3. **Skip discovery, run every adapter**. Viable only while adapter count is small. Honest starting point; must be measured, not assumed.

**Ship option 1, instrument it, and let the numbers decide when option 2 is due.** Publish the finding. Nobody else has.

### 5.3 Batching and the read/write split

**Multicall behind a transparent proxy + request coalescing.** Zapper's single best ergonomic idea: adapter code writes naive `await contract.balance_of(address)` inside a `gather()`, and 250 reads coalesce into one `Multicall.aggregate`. Non-strict aggregation returns typed per-call errors so one bad contract cannot kill a batch. LlamaFolio's variant preserves positional alignment and passes `None` through, so `results[i]` always matches `inputs[i]` and nothing ever throws.

**Separate chain I/O from pricing.** `raw_balances()` → chain reads only; `drill(raw, prices)` → pure, no I/O. Persist raw balances and **re-drill against fresh prices without touching an RPC**. A price tick must not cost a re-scan.

**Read/write split with an explicit staleness contract:**

```
GET  /accounts/{id}/holdings    → cached. { status: "fresh"|"stale", updated_at, next_update_at }
POST /accounts/{id}/refresh     → enqueue recompute, return job id
GET  /jobs/{id}                 → poll
```

Zapper's public API worked exactly this way and was candid about the cost: *"Cached values for apps are never purged, so could be months or years old"*, and *"Most POST … finish computing within 10 seconds."* **Full recomputation is not a request-latency operation.** Say so in the docs.

### 5.4 The adapter contract

The bar is LlamaFolio's claim that most adapters take **under an hour**. What makes that true is not the interface. It is the fork helpers. `lib/uniswap_v2.py`, `lib/masterchef.py`, `lib/erc4626.py`, `lib/aave_v2.py`, `lib/compound_v2.py`, `lib/curve.py`. Zapper's *entire* production Uniswap V2 integration was 15 lines and **zero methods**: a subclass setting `factory_address`, `subgraph_url`, and a label. Aim there.

```python
class PositionAdapter(Protocol):
    id: str                                  # DefiLlama protocol slug: the join key
    chains: frozenset[str]

    def discover(self, ctx: DiscoveryContext) -> ContractSet: ...
    def resolve(self, ctx: ResolveContext, contracts: ContractSet) -> list[Position]: ...
```

`ContractSet` arrives at `resolve()` **partially populated or empty**. That is the whole point of pre-filtering. A resolver that raises is caught, logged, and drops only its own slice.

**Two lessons from Zapper's abstraction graveyard:** `Erc4626VaultTemplate` was built and **never adopted by a single app**: most vault integrations predated the standard. Build the abstraction when the third caller shows up, not the first. And don't commit generated bindings: roughly half of Studio's 2,742 files were machine-written code under human review.

---

## 6. Output projections

`project/` is a set of pure functions from the internal model to a wire format. Three targets, all I/O-free and fixture-testable:

| Projection | For |
|---|---|
| `native.py` | The full model: positions with groups, acts, data quality. Richest. |
| `plaid.py` | Plaid-compatible, so crypto merges with bank/exchange data downstream. |
| `scalar.py` | `(metric, timestamp, float)` triples for hosts with a scalar metrics pipeline (§8). |

### 6.1 The sign convention: get this wrong and nothing errors

> **"Positive values when money moves out of the account; negative values when money moves in."**. Plaid docs, verbatim

Inverted relative to almost every accounting system. Consistent across `/transactions` and `/investments`. A consumer merging our rows with Plaid's under the wrong convention **silently double-counts net worth**. One contract test exists solely for this.

### 6.2 Mapping

| Ours | Plaid |
|---|---|
| Connection (address / xpub / exchange key) | `Item`, `institution_id: null` or a synthetic `ins_crypto_*` |
| Account | `Account{type: "investment", subtype: "non-custodial wallet" \| "crypto exchange"}` |
| Asset | `Security{type: "cryptocurrency", subtype: "cryptocurrency"}` |
| Holding | `Holding{quantity, institution_price, institution_value, cost_basis, tax_lots[]}` |
| Transaction | `InvestmentTransaction{type, subtype}` |
| Part (direction out) | `type: transfer, subtype: send`, Plaid: *"Inflow or outflow of fiat or cryptocurrency to an address or email"* |
| Swap | `type: transfer, subtype: trade`, *"Trade of one cryptocurrency for another"* |
| Position | `crypto_positions[]` (extension) **+** synthetic Holdings |

Plaid prices crypto securities intra-day and says so on `close_price`, *"If the security is a cryptocurrency, this field will be updated multiple times a day"*, unlike equities. Their own sandbox ships a `"Plaid Crypto Exchange Account"` holding DOGE.

### 6.3 Where Plaid breaks, and the minimal fix

**`unofficial_currency_code` is a closed 23-value list frozen circa 2018**. `BTC ETH USDT ADA XRP DOGE …` and no `USDC`, `SOL`, `MATIC`, `AVAX`, `ARB`, `stETH`, `WBTC`. You therefore *cannot* denominate a value field in most crypto assets. The only compliant move is to price everything in `iso_currency_code: "USD"` and carry the crypto denomination on the Security.

> **Consequence: being Plaid-shaped forces us to own a price oracle.** That is the largest hidden cost in this design. Budget for it.

Extensions, each namespaced under a single strippable `crypto` key:

```jsonc
security.crypto  = { asset_id (CAIP-19), chain_id (CAIP-2), contract_address,
                     token_standard, decimals, asset_class, token_id, collection,
                     price_source, liquidity_usd, is_spam_suspected }

holding.crypto   = { quantity_raw: "1234567890123456789", decimals,
                     as_of_block, as_of_datetime }        // ← the precision fix

account.crypto   = { address, chain_id, custody, wallet_type,
                     derivation_path, is_watch_only }

investment_transaction.crypto = {
    tx_hash, chain_id, block_number, log_index,   // (tx_hash, log_index) = idempotency key
    from_address, to_address, status, confirmations,
    quantity_raw, decimals,
    gas: { security_id, quantity_raw, decimals, value },   // gas is its OWN asset
    act_id, leg_group_id, leg_role,               // ← the swap fix
    protocol, income_kind,
    is_internal_transfer }                        // ← without this, every self-transfer
                                                  //    looks like income and every tax
                                                  //    report is wrong
```

Plus a **new top-level `crypto_positions[]`**. Do not bend `Holding` into a DeFi position; you end up with a schema that is neither Plaid-compatible nor DeFi-correct.

**The projection invariant, with a test:** an Aave position supplying 10 ETH and borrowing 5,000 USDC emits two synthetic Holdings. `+10 ETH` and a **negative-quantity** USDC Holding. A Plaid-only client sums `institution_value` and gets the right net worth. Negative `quantity` mildly extends Plaid's semantics; it is the only way to make the naive sum correct, it is consistent with `tax_lots[].position_type: SHORT`, and it must be documented loudly.

Mint synthetic institutions (`ins_crypto_ethereum`, `ins_crypto_base`) so nothing downstream sees a null; keep `institution_id: null` only for genuinely ad-hoc watched addresses, which Plaid's docs already sanction.

### 6.4 `/crypto/sync`: the endpoint Plaid doesn't have

Plaid gives cursor sync to `/transactions` (bank) and **nothing to `/investments`**. Where crypto lives. Build it, in exactly Plaid's envelope:

```json
{ "added": [...], "modified": [...], "removed": [{"transaction_id","account_id"}],
  "next_cursor": "...", "has_more": true }
```

Keep both of Plaid's hard rules: **order every array by ascending last-modified time** (not by transaction date, that is what lets a two-year-old row reappear in `modified`), and require the client to page until `has_more == false` before persisting the cursor.

**This is why the model fits crypto so well: a chain reorg is `removed` + re-`added`.** A last-modified-ordered cursor is precisely the primitive for a ledger that can rewrite its own history. Zerion re-delivers reorged transactions with `deleted: true`; we make it a first-class event rather than a magic boolean.

---

## 7. Tenancy

### 7.1 What to copy from Vezgo, verbatim

**`external_user_id` (their `loginName`) is the entire tenancy model.** There is no user-creation endpoint. A user exists as a side effect of minting a token: get-or-create, idempotent, the same string always resolves to the same user and account set. *The host's* system stays the directory of record; ours is a keyed store. This eliminates a whole class of drift bugs, and it is why Vezgo has no list-users endpoint. There does not need to be one.

**The `authEndpoint` contract: POST → `{token}`. That is the whole interface.** `external_user_id` is chosen server-side from the host's session, so **a hostile client cannot request a token for a different user. It literally cannot express which user it wants.** This is the single best idea in Vezgo's design. Copy it exactly, including the `authorizer` callback escape hatch for native clients.

**Short-lived JWT, expiry readable client-side.** No refresh token, no revocation store, no `/refresh`. Clients decode `exp` and re-mint on a configurable `minimum_lifetime`: small for API calls, larger before opening a connect flow.

Also copy: `?v=` versioned long-poll with `304` (optimistic-concurrency counter, 30s block, no WebSocket needed); `409` conflicts carrying `existing_connection_id` so a UI can navigate to the conflict; the OAuth2-shaped redirect (`code` duplicating the account id) so off-the-shelf AppAuth works on mobile; and a **first-class Demo provider** where every error branch is reachable without a real account.

### 7.2 What to fix: Vezgo's model is genuinely unsafe

> **The client secret is an unscoped god key.** `client_id` + `secret` + any `external_user_id` = that user's entire portfolio. No consent step, no per-user grant, no audit trail, no scoping, no revocation, no documented rotation. A leaked secret compromises every user simultaneously and **silently**.

| Fix | Detail |
|---|---|
| **Scoped keys** | `Organisation → Project → ApiKey` with scopes: `accounts:read`, `accounts:write`, `sync:trigger`, `users:admin`. A read-only analytics service must not be able to mint tokens or delete accounts. Vezgo has one flat level. |
| **Key rotation** | Separate keys per environment, independent rotation, overlap window. |
| **Revocation** | `POST /auth/revoke` + `jti` in the JWT. A 10-minute TTL is not a substitute for revocation when the app credential is compromised. |
| **Audit log** | Every token mint: `external_user_id`, key id, IP, timestamp. Vezgo has nothing. |
| **Opaque ids enforced** | Vezgo *warns* against PII in prose while their OpenAPI `loginName` example is literally `user@example.dev`. Make it an invariant: reject email-shaped input, or hash at the edge. The real reason is one their docs never state. **It is a bearer-equivalent secret, and an email is guessable.** |
| **`GET /users/me`** | Vezgo has only `DELETE`. Add read, plus a **project-scoped** `GET /users`. "No list endpoint" is defensible for a user token; it is not defensible for the operator. Their own clean-up guide is an admission of the gap. |
| **`PATCH /users/me/external_id`** | Immutable-forever with no migration path is a footgun. |

### 7.3 Multi-tenant primitives the whole market lacks

Zerion's `wallet-sets` is capped at **one EVM address and one Solana address**, not a portfolio-of-many-wallets primitive. Their rate limits are **org-scoped** with no per-tenant attribution. Their webhook callback URLs need **manual whitelisting by support**.

| Primitive | Prior art |
|---|---|
| **Batch-wallet queries** | Allium `POST` with 100 `{chain, address}` pairs. Strictly better than a one-address GET for a multi-tenant caller. |
| **Partial success in batches** | Allium's `items[]` is a union of `Result \| Error`, each tagged `{chain, address}`. One bad address does not fail the request. Plus a `warnings[]` array. Both beat all-or-nothing. |
| **Per-tenant quota headers** | Zerion's three-window shape is right (`Second`/`Day`/`Month`, each limit/remaining/reset), but scope it **per tenant**, not per org. |
| **Self-serve webhooks with durable delivery** | HMAC-SHA256 over `timestamp + body`, `delivery_id`, exponential backoff over 24h (Dune SIM's 5-retry model, not Zerion's 3-over-60s), dead-letter view, **replay endpoint**. |
| **Rich event set** | Vezgo has two terminal events. Ship: `connection.created/disconnected/deleted`, `holdings.updated`, `transactions.available`, `sync.started/failed`, `reorg.detected`. |
| **Billing by work done** | Dune SIM charged N CU where N = chains touched, 4N for DeFi. GoldRush charges **per item**. Both are more honest than per-call, and they rebut "all chains in one call". That call is 20× the work. |

---

## 8. Embedding

Rules #11 and #12, made concrete. A host application with its own Python backend must be able to adopt this without adopting our infrastructure.

**Import, don't call.** The public surface is a plain Python API. No HTTP hop, no serialisation, no separate service to deploy.

```python
from auradefi import Conduit
from auradefi.ledger.backends.sqlmodel import SqlModelLedger

conduit = Conduit(ledger=SqlModelLedger(session_factory=host_session_factory))

user = conduit.user("opaque-host-user-id")          # get-or-create
conn = user.connect_address(chain="eip155:1", address="0x…")
report = user.sync()                                 # budgeted, resumable, self-throttling
```

**Storage is a port.** `ledger/port.py` defines the protocol; `ledger/backends/sqlmodel.py` is the default; `memory.py` backs the test suite. A host binds its own session factory and keeps its own migration story. We never open a connection the host didn't hand us, and nothing outside `ledger/backends/` imports an ORM: enforced by `test_layering.py`.

**The host owns scheduling; we own throttling.** Many hosts run a single fixed-interval tick across every integration, with no per-integration cadence. So `sync()` must be **self-throttling**: calling it more often than the underlying data changes is a cheap no-op, driven by a module-level minimum interval and the stored cursor. Never assume a scheduler exists, and never require one.

**Sync is budgeted and resumable, in two phases.** A wallet with a decade of history must not spend hours pulling old blocks while a dashboard sits empty. One shared budget per call, spent first on a **live window** from the cursor forward, then on a **backwards backfill** from a second cursor. History fills in from recent to old *behind* the live window. Both cursors persist; the live cursor only advances when its window drains, or the pages the budget cut off are stranded.

**Validate at connect time, not at sync time.** `connect_address()` performs a cheap reachability and liveness check and raises immediately on a bad address or unreachable source. A connector that accepts anything and fails silently on a background tick hours later is the worst possible failure mode for an embedding host.

**Three output shapes, so a host takes only what it can use** (§6):
- `native`, the full model
- `plaid`, merges with bank and exchange data
- `scalar`, `(metric, timestamp, float)` triples for hosts whose metrics pipeline is scalar-only. Emit at minimum `portfolio_value_usd` and `transaction_count`, plus **activity cadence** (transactions per hour-of-day), because timing is a signal in its own right and costs nothing to derive.

**Nothing in the core imports a web framework.** The HTTP API in `api/` is one adapter among several, not the product.

---

## 9. Accounting: the clearest open space

Every vendor either lacks PnL or ships it as a black box. **Three vendors offer PnL; none document the method**, no wash-sale handling, no transfer-between-own-wallets detection.

Zerion is the exception and it is FIFO-only. Their own spec leaks the implementation:

> *"PnL is pre-computed at standard marks (`now`, `1 day ago`, `1 week ago`, `1 month ago`, `1 year ago`, `beginning of the year`). Other values are supported only if fewer than 3,000 transactions sit between your timestamp and the nearest mark: otherwise the request errors out."*

Plus: 503 on first request for a cold wallet, no wallets over 1M transactions, per-token breakdown only for tokens you name (max 100), no NFT PnL, no per-chain split.

**Arbitrary-date PnL is effectively unavailable on active wallets.** That is the most exploitable weakness in the market for anyone willing to do incremental lot-tracking properly.

Ship `accounting/` with **pluggable methods**, FIFO, LIFO, HIFO, average cost basis, over a real lot ledger, and map to Plaid's `tax_lots[]` (`institution_lot_id`, `original_purchase_datetime`, `quantity`, `purchase_price`, `cost_basis` inclusive of fees, `current_value`, `position_type: LONG|SHORT`). Plaid's lot structure is *better* than most crypto APIs give you. Use it.

`is_internal_transfer` is not optional: without it every self-transfer reads as income and every tax report is wrong.

---

## 10. Coverage

Chains ship as **source adapters**, one family at a time. Publish the per-capability matrix as an endpoint (`GET /coverage`) so clients grey out what we cannot do rather than silently under-reporting net worth. Dune SIM's `/defi/supported-protocols` is the model.

| Family | Source | Notes |
|---|---|---|
| **EVM** | Etherscan V2 (one key, 50+ chain ids) + RPC multicall | Free tier 3/sec, 100k/day. **July 2026 cuts**: max records per request 10,000 → **1,000**, some high-traffic chains excluded from free. Paginate correctly from day one. |
| **Bitcoin** | Blockstream Esplora | Free since 2021, ~50 req/s, native **xpub**, 25 tx/page via `last_seen_txid`. Derive locally with BIP32, **never send an extended key off-box** (rotki does this right). Gap-limit-aware scanning. |
| **Solana** | Helius / public RPC | `getSignaturesForAddress` + SPL token accounts. Genuinely messier than EVM; the long pole. Token-2022 ScaledUiAmount breaks the `raw/10^d` identity. |
| **Cosmos** | LCD/RPC direct | **Nobody serves this.** Allium has ten Cosmos chains warehouse-only; GoldRush, Sim, Zerion, DeBank, Moralis and Alchemy have none. A genuine differentiator, and genuinely a lot of work. Not phase 1. |

**Bitcoin xpub is the single most-requested thing an EVM-native tracker cannot do.** Only CoinStats, Vezgo and GoldRush derive HD wallets. It is the difference between "paste 40 addresses" and "paste one xpub", and Esplora gives it to us free.

---

## 11. Phases

| Phase | Deliverable | Done when |
|---|---|---|
| **0** | Foundation: `money`, `assets` (CAIP-19), `chains`, ledger port + memory backend, style gates, cassette harness | `pytest` green on a fresh clone with **no API keys** |
| **1** | EVM balances → holdings. Etherscan V2 + DefiLlama prices. Single-tenant, library-only. | A known-rich address returns a USD total within a few % of an incumbent |
| **2** | Tenancy: org/project/key, `external_user_id`, JWT mint, `authEndpoint`, connections, audit log | Two tenants cannot see each other's data, with a test that tries |
| **3** | Transactions: `decode/` pipeline, ERC-20 + native, `parts[]`/`acts[]`, cursor sync | A reorg fixture produces `removed` + re-`added` |
| **4** | Positions: adapter protocol, `tokens.py`, Uniswap V2/V3, Aave, liquid staking. Golden fixtures per adapter. | Projection invariant holds: synthetic Holdings sum to the same net worth |
| **5** | Embedding surface: SQLModel backend, budgeted two-phase sync, `scalar` projection | A host can import, bind a session, and sync on its own tick |
| **6** | Bitcoin + xpub | One xpub returns the full derived-address balance set |
| **7** | Solana | |
| **8** | HTTP API, webhooks (signed, durable, replayable), quota headers, batch endpoints | |
| **9** | `accounting/`: lot ledger, FIFO/LIFO/HIFO/ACB, PnL | Arbitrary-date PnL on a 50k-transaction wallet. The thing Zerion cannot do |

Phase 1 is the honest first milestone. Phases 0–4 are the bulk of the work.

---

## 12. Risks

1. **Nobody survived on retail.** Zapper had 2M MAU and died. The warning is in their own post-mortem: free expectations, unbounded indexing cost. Open source sidesteps the revenue problem but not the **cost** problem, which is why §5.2 matters more than any other section.
2. **Adapter rot is the dominant ongoing cost.** rotki patches decoders broken by upstream changes almost every release across 130+ protocols. Zapper's issue tracker is 364 issues of breakage. The failure mode is **silently wrong numbers**, not outages.
3. **Contribution velocity decays.** Zapper Studio: 1,680 merged PRs in 2022 → 996 in 2023 → **12 in 2024**. Hacktoberfest bursts do not sustain. Design for a small maintainer set; make the fork helpers do the work.
4. **Owning the price oracle is unavoidable** and is the largest hidden cost (§6.3).
5. **Etherscan's free tier is degrading.** Expect to pay.
6. **Docs lie, including your own.** Allium's Holdings page says "Bitcoin and Solana only" while its live endpoint reports PnL on 16 chains. Zerion's Moralis guide says PnL average buy/sell prices are not returned; their OpenAPI says they are. **Generate the coverage matrix from live capability checks, never from prose.**

---

## 13. Verification

```bash
pytest
```

Must pass **on a fresh clone with no API keys**. Cassettes committed. This is phase 0's acceptance criterion and the single best predictor of whether adapter contributions arrive.

```bash
pytest tests/golden -v
```

Per-adapter golden fixtures pinned to a block height. A number changes → the test fails. Non-negotiable (rule #5).

```bash
pytest tests/style
```

Size caps, structure, placement, layering, including the gates that keep the core free of web frameworks and ORMs.

**Correctness against reality.** The only real test of a portfolio engine is cross-checking a known-rich public address against a live incumbent and reconciling the delta. Do it in CI weekly against a fixed address set, and **publish the reconciliation**. Nobody else does, and it is the most credible artefact an open-source project in this space can show.

**Contract tests, each in its own file because each has burned somebody:**
- Plaid sign convention: outflow is **positive** (§6.1)
- Projection invariant: `sum(synthetic Holdings.institution_value) == net_worth` (§6.3)
- No JSON integer ever appears in a raw-amount field (rule #2)
- Two tenants cannot read each other's data
- A reorg fixture yields `removed` + re-`added` with a monotonic cursor
- `sync()` called twice in quick succession is a no-op the second time (§8)
- The core imports no web framework and no ORM

---

## 14. Open decisions

| Decision | Recommendation |
|---|---|
| **Name** | ~~`conduit` is a placeholder, check PyPI~~ **Resolved: `auradefi`** (see `docs/internal/DECISIONS.md`) |
| **Licence** | **Apache-2.0** (§1.1) |
| **Database** | Postgres via the default SQLModel backend; SQLite and in-memory for tests |
| **Interaction index** | Start with explorer-derived (§5.2 option 1); instrument before building the log index |
| **Web framework** | FastAPI + SQLModel, but confined to `api/` and `ledger/backends/` respectively, per §3.3 |
| **Primary output** | Serve **all three** projections. `project/` is pure, so this is nearly free. |
