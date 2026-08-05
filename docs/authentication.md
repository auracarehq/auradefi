# Authentication & keys

You need at most one key, and even that one is optional.

| Service | What it gives you | Key | Covers |
|---|---|---|---|
| Sandbox | Everything, recorded | none | one address, one chain, seven transactions |
| Etherscan V2 | EVM balances + history | optional | one key, *every* `eip155:*` chain |
| DefiLlama | USD prices | none, keyless | 6 EVM chains. No BTC or SOL prices at all |
| Blockstream Esplora | Bitcoin UTXO balances | none, keyless | Bitcoin; the network is the base URL |
| Solana JSON-RPC | SPL + Token-2022 balances | none for the public endpoint | mainnet-beta |
| Webhooks | Delivery to *you* | n/a; we sign, you verify | your endpoints |

There is no auradefi account, no dashboard and no credential of ours to
obtain. Every key above belongs to a third party, and you bring it.

## Environment variables

All configuration is read by `Settings.from_env()`, which `Auradefi.from_env()`
calls. Copy [`.env.example`](https://github.com/auracarehq/auradefi/blob/main/.env.example)
and fill in what you need.

| Variable | Default | Meaning |
|---|---|---|
| `AURADEFI_ETHERSCAN_API_KEY` | none | Etherscan V2 key. Optional. |
| `AURADEFI_HELIUS_API_KEY` | none | Parsed but not yet consumed. See Solana below. |
| `AURADEFI_HTTP_TIMEOUT_S` | `10.0` | Timeout for clients the library builds for you. |
| `AURADEFI_SYNC_MIN_INTERVAL_S` | `60` | Floor between two ticks for one connection. |
| `AURADEFI_PROJECT_ID` | `embed` | Namespace for derived tenant ids. |
| `AURADEFI_TRUSTED_PROXY_HOPS` | `0` | How many `X-Forwarded-For` hops *your* proxies add. |

The `AURADEFI_` prefix is mandatory. A bare `ETHERSCAN_API_KEY` in your shell
is ignored on purpose, so an unrelated variable can never silently become this
library's credential. A test pins that behaviour.

## Etherscan V2

This is the only key worth setting.

```bash
export AURADEFI_ETHERSCAN_API_KEY=…      # https://etherscan.io/apis
```

It is optional. Without it the `apikey` parameter is omitted from the request
entirely, rather than sent empty, and Etherscan's keyless tier applies.

One key covers every EVM chain. The chain travels in the request as `chainid`,
derived from the CAIP-2 id, so Ethereum, Polygon, Base and any other
`eip155:N` Etherscan supports all use the same key.

The free tier allows 3 requests per second and 100k per day. This package has
no retry and no rate limiting anywhere, so a burst surfaces immediately as
`SourceError` and you pace your own ticks with `sync(budget=…)`. Token
balances cost one request each, because there is no multicall yet, which makes
a wide address proportionally expensive.

A wrong or revoked key is not a distinct error type. Etherscan answers HTTP
200 with `{"status": "0", "message": "NOTOK", "result": "Invalid API Key"}`,
which surfaces as:

```
auradefi.errors.SourceError: etherscan balance error: message='NOTOK' result='Invalid API Key'
```

An empty history is a valid answer rather than an error. `status: "0"` with
`"No transactions found"` is an empty page, because a fresh address is a valid
address.

## DefiLlama

There is no key to set. Two limits matter more than the credential does.

It covers six chains: ERC-20 prices resolve on chain ids 1, 10, 56, 137, 8453
and 42161, and native coin prices resolve on the four ETH-native ones.

It has no Bitcoin or Solana prices at all. Nothing in this package can price
BTC or SOL. Those assets come back held but unpriced: listed in
`report.holdings` with `price=None`, named in `report.unpriced`, and never
counted as zero. To price them, bind your own `prices` port, described in
[Bring your own](bring-your-own.html).

## Bitcoin

Esplora needs no key. The base URL *is* the network selector:

```python
Esplora(client)                                              # mainnet
Esplora(client, base_url="https://blockstream.info/testnet/api")
```

The thing to budget for here is request volume. A gap-20 scan of an empty
wallet is about 40 requests, one per derived address, with no throttle.

The extended public key never leaves your process. Every request carries a
derived `bc1…` address, and the test suite asserts that against recorded
traffic.

## Solana

The public endpoint needs no key and is aggressively rate-limited upstream; a
429 surfaces as `SourceError: solana rpc HTTP 429`. For a keyed provider, pass
the entire URL. `AURADEFI_HELIUS_API_KEY` is parsed by `Settings` and consumed
by nothing, because the Helius adapter is declared in the spec and does not
ship:

```python
SolanaRpc(client, url="https://mainnet.helius-rpc.com/?api-key=…")
```

Solana transaction decode is not implemented. Balances and signature history
only.

## Webhooks

Here the direction is reversed: you hold no key of ours, because we sign and
you verify. Each endpoint gets a secret, shown once at registration, and every
delivery carries `X-Auradefi-Signature` (HMAC-SHA256 over `timestamp.body`)
plus `X-Auradefi-Timestamp`. The verifier ships:

```python
from auradefi.webhooks.sign import verify_signature

verify_signature(secret, timestamp_ms, raw_body, signature, now_ms)
```

It compares in constant time and refuses a stale timestamp, which gives a
captured request a shelf life. See
[guide 09](examples/09_deliver_signed_webhooks.html).

## The HTTP API's own credentials

If you run the [HTTP API](http.html), it has a second, unrelated credential
model, and these credentials are yours to issue.

`adk_live_…` and `adk_test_…` server keys are created by your backend and
scoped (`users:admin`, `accounts:read`, `accounts:write`, `sync:trigger`).
They are stored as hashes, so a database dump does not yield working
credentials.

Short-lived user tokens are minted from a server key for exactly one end user
and signed with that project's secret. They are safe for a browser or a mobile
app, and a token from one project can never verify under another's secret.

See [guide 05](examples/05_serve_the_http_api.html) and
[guide 06](examples/06_isolate_two_tenants.html).

## What `CassetteMissError` means

If you are in Sandbox and see this, nothing is broken and no credential is
missing:

```
auradefi.errors.CassetteMissError: GET https://api.etherscan.io/… is not
recorded in sandbox.json. Recorded interactions: …
```

It means you asked for something the recording does not contain, usually a
different address, chain or page size. Switch to `from_env()` with a real key,
or ask for what the recording holds, which
[Quickstart](quickstart.html#what-just-happened) lists.
