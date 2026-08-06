# Glossary

Most of the vocabulary here is either a standard nobody has met yet (CAIP-2)
or a word this package uses more narrowly than the industry does (a part, a
connection, a tenant). This page defines every term the other pages assume.

Definitions come from the code, and the linked page is where each one is used
in anger.

## Chains and asset ids

| Term | What it means |
|---|---|
| CAIP-2 | A chain id as a string: `eip155:1` is Ethereum, `eip155:137` Polygon, `bip122:…` Bitcoin, `solana:…` Solana. Every method that takes a chain takes this. `"ethereum"` is refused. |
| CAIP-19 | An asset id as a string, built on a CAIP-2. Native coins use the chain's own form; an ERC-20 is `{caip2}/erc20:{lowercased contract}`. This is what `Part.asset_id` and the price port speak. |
| `ast_…` id | The asset registry's own key for an asset it has seen, derived deterministically and permanently stable. It is a registry handle, and never appears where a CAIP-19 is expected. |
| Asset group | Several asset ids that a caller wants totalled as one thing. Members must agree on decimals, which is the law that stops two tokens of different precision being summed. Ungrouped assets fall back to `single`. |
| Chain registry | The per-instance, mutable set of chains an instance will accept. Five are seeded. `register()` adds one, and `connect_address` refuses any chain the registry does not hold. |

## Amounts

| Term | What it means |
|---|---|
| `Quantity` | An amount of an asset, wrapping `Decimal`, exact to 10^77. Carries its own decimals and its `raw`. |
| `Money` | An amount of a currency, wrapping `Decimal`, with the currency attached. `Money(Decimal("2500"), "USD")`. |
| `raw` | The on-chain integer, always carried as a JSON **string**. A raw amount is never a JSON number, because a large one loses precision in any JSON parser that reads numbers as doubles. |
| Tagged decimal string | How both types cross a wire: the value as a string beside its unit, never as a bare number. |
| Unpriced | Held, and with no price available. The holding comes back with `price=None`, is named in `report.unpriced`, and is never counted as zero. |

## Balances, positions, transactions

| Term | What it means |
|---|---|
| Holding | One asset balance on one account, matching Plaid's Holding. Price and value are set together or both `None`; one of the two alone is a `ValidationError`. |
| Position | A DeFi commitment that is not a plain balance: an LP pair, a loan, a stake. Read through a position adapter, and projected into signed synthetic holdings so net worth still adds up. |
| Part | One movement of one asset inside a transaction. Every movement is a part, and a transaction is the list of its parts. |
| Act | One sub-operation of a transaction, such as the swap inside a transaction that also paid a fee. Parts point back at their act by `act_id`. Today every decoded transaction has exactly one. |
| Fee | A sibling of the parts, never a member of them, and never a ledger entry. `borne_by` records who paid it, which is what lets a total skip the fee on an inbound transfer while still showing it. |
| Data quality | Decode metadata carried on the transaction itself: what was incomplete, a confidence, the decoder version, and which sources contributed. |

## The ledger and the tick

| Term | What it means |
|---|---|
| Ledger | The store transactions live in, reached through the `ledger` port. The default is memory and loses everything at exit. |
| Cursor | A position in the ledger's change feed. Pages come back in last-modified order, so a row that changes later reappears at the end of the feed. |
| `has_more` | Whether the feed has further pages. A client pages until this is false before it persists the cursor. |
| Sync event | What an upsert emits when something actually changed: `ADDED`, `MODIFIED` or `REMOVED`. An unchanged row emits nothing, which is what makes a whole tick safe to retry. |
| Reorg | A chain reorganisation. The affected rows are marked `removed`, and the replacements arrive as new rows. |
| Resurrection | A removed row coming back. It is re-added with `removed=False`, a bumped sequence and an `ADDED` event, so the history stays expressible. |
| Budget | The cap on how many source pages one `sync()` call may spend. `sync(budget=5)` bounds the work, and the cursor makes the next call resume. |
| No-op tick | A `sync()` inside `sync_min_interval_s` of the last one. It touches no transport and reports `no_op=True`. |

## Tenancy

| Term | What it means |
|---|---|
| Organisation | The top level. Holds projects. |
| Project | The isolation root. Every read and write is keyed by project first, and a token minted under one project can never verify under another's secret. |
| End user | One of your customers, inside one project. Ids are opaque and project-scoped; there is deliberately no method that lists users across projects. |
| Tenant id | The derived, deterministic id everything is stored under: `usr_` plus a truncated SHA-256 of `project_id` and your opaque user id. |
| Connection | One watched address bound to one tenant, on one chain. The id is chain-scoped, so the same address on two chains is two connections with two cursors. |
| Server key | `adk_live_…` or `adk_test_…`, issued by your backend, scoped to what it may do, and stored as a hash. |
| User token | A short-lived token minted from a server key for exactly one end user. Safe to hand to a browser or a mobile app. |

## Ports and offline work

| Term | What it means |
|---|---|
| Port | A collaborator the host may replace: `source`, `prices`, `ledger`, `sync_state`, `clock`. Each is a structural protocol, so an object with the right methods is the port. There is no base class and no registration step. See [Bring your own](bring-your-own.html). |
| Sandbox | `Auradefi.sandbox()`. A recording of one address' real traffic, bundled in the wheel and replayed locally through the production code path. No key, no network, no configuration. |
| Cassette | The recording itself: a JSON file of request and response pairs, matched on method, host, path and sorted query. |
| `CassetteMissError` | You asked a recording for something it does not hold. In Sandbox this means a different address, chain or page size, and no credential is missing. |
| `FrozenClock` | A `clock` port pinned to one instant. Because time is a port, quota windows and sync throttling are testable without sleeping. |
