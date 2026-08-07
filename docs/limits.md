# Limits and cost

This package issues one request per thing it needs, and never retries. Two
numbers therefore decide what it costs you: how many requests an operation
makes, and what the service at the other end allows.

Both are knowable in advance, so they are on this page instead of in your
first bill.

## What each call costs

| Call | Requests it makes | Under `budget`? |
|---|---|---|
| Anything in Sandbox | zero. The recording is in the wheel and no socket opens | n/a |
| `sync(budget=n)` | at most `n` history pages, shared across every connection in that tick | yes, `n` is the cap |
| `holdings()`, `scalar_metrics()` | per connection: one native balance, then one `tokentx` page per page of that address' token-transfer history, then one per distinct token contract found. Plus one DefiLlama request covering the whole set | no |
| Bitcoin gap-20 scan | about 40 for an empty wallet: one per derived address, over the receive and change chains, stopping after 20 consecutive unused | no |
| Solana `balances()` | two: `getBalance` and `getTokenAccountsByOwner` | no |

Two things in that table surprise people.

`budget` covers history pages and nothing else. It is the cap on
`fetch_txlist` calls inside one `sync()`, so it bounds the backfill. It does
not bound `holdings()`.

`holdings()` is neither budgeted nor cached. Every call refetches every
balance for every connection. A dashboard that calls it per page view pays the
full cost per page view, so hold the report on your side and refresh it on
your own schedule.

Nothing in the holdings path batches its reads, so one token balance is one
request. An address that has touched two hundred tokens costs about two
hundred requests to value, and a wide address is expensive in proportion to
its width. `auradefi.sources.evm.multicall.Multicall3` ships and will collapse
a batch of contract reads into one `eth_call`, but the holdings path does not
use it yet: today it is a component you wire yourself.

## What the services allow

| Service | Published limit | Does a key change it? |
|---|---|---|
| Etherscan V2 | 3 requests per second, 100,000 per day on the free tier | yes. Without a key the keyless tier applies |
| DefiLlama | none published | there is no key |
| Blockstream Esplora | none published | there is no key |
| Solana public endpoint | none published, and rate-limited aggressively in practice | pass a keyed provider's full URL |

## What happens when you exceed one

Nothing absorbs it. No source and no oracle in this package retries, backs off
or throttles, so the failure arrives at your code on the first request that
crosses the line:

| Service | What you get |
|---|---|
| Etherscan V2 | `SourceError` carrying the envelope, since Etherscan answers HTTP 200 with `status: "0"` |
| Solana | `SourceError: solana rpc HTTP 429` |
| Esplora, DefiLlama | `SourceError` carrying the status |

Inside `sync()`, a `SourceError` is contained to the one connection that
raised it: its report row is marked failed and its siblings keep their share
of the budget. Outside `sync()`, it reaches you.

If you want backoff, own the transport. Every source takes an `httpx.Client`
in its constructor, so a client configured with your retry policy, your
timeouts and your connection limits replaces the one the library would build:

```python
from httpx import Client, HTTPTransport

from auradefi.sources.evm.source import EtherscanSource

client = Client(transport=HTTPTransport(retries=3), timeout=20.0)
source = EtherscanSource(client, api_key="…")
```

## How to pace yourself

Three controls, all yours.

`budget=n` caps the pages one tick may spend. Start small and raise it: a
decade-old wallet drains over many ticks, and the cursors mean no page is
fetched twice.

`AURADEFI_SYNC_MIN_INTERVAL_S` (default 60) is the floor between two ticks for
one connection. A `sync()` inside that window touches no transport and comes
back with `no_op=True`, so a host that ticks every integration on one fixed
timer costs nothing extra.

The schedule itself is yours. Nothing here starts a thread, and no work
happens unless you call `sync()`.

## The HTTP API's own quota

If you run the [HTTP API](http.html), it meters your callers on three windows
per tenant: per second, per day and per month. Every response carries nine
headers, `X-RateLimit-{Limit,Remaining,Reset}-{Second,Day,Month}`, and a
refusal is a 429 with `Retry-After` in whole seconds.

That quota is yours to set and has no relationship to Etherscan's. It limits
your customers; Etherscan limits you.

## Webhook delivery does retry

The one place this package retries at all. A delivery gets six attempts on a
pinned schedule, measured from when it was created: immediately, then after 1
minute, 5 minutes, 30 minutes, 2 hours and 24 hours. After the sixth it is
dead-lettered, and a dead letter can be replayed. See
[guide 09](examples/09_deliver_signed_webhooks.html).
