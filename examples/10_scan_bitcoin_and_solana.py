"""How do I handle a Bitcoin xpub and Solana's token zoo?

    pip install auradefi
    python examples/10_scan_bitcoin_and_solana.py

Two chains that are not EVM, and each breaks an assumption:

**Bitcoin has no account.** One wallet is an unbounded set of addresses
derived from an extended public key, and the balance is the sum over the
used ones. BIP32 derivation here is pure Python, no `secp256k1` C library,
no `bitcoinlib`, and, crucially, the xpub **never leaves the process**:
every HTTP request carries a derived `bc1…` address, which this file asserts
against the recorded traffic rather than promising in prose. The scan stops
after `gap` consecutive unused addresses (BIP44's gap limit, 20 by default),
and an address that was used and then swept still counts as used.

**Solana can lie about `raw / 10**decimals`.** A Token-2022 mint carrying
the ScaledUiAmount extension displays a multiple of the raw amount, so the
identity every other chain relies on does not hold. Both numbers are
carried, and the divergence is flagged: a wallet that only stores one of
them shows the wrong balance and cannot tell.

Both sections replay synthesised HTTP, so the file runs offline.
"""

from __future__ import annotations

import functools
import json
import tempfile
from pathlib import Path

import httpx

from auradefi.money.quantity import Quantity
from auradefi.sources.bitcoin.esplora import Esplora, scan
from auradefi.sources.bitcoin.xpub import derive_addresses, parse_xpub
from auradefi.sources.solana.rpc import (
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    SolanaBalances,
    SolanaRpc,
)
from auradefi.testing.cassettes import load

# BIP32 test vector 1's master public key: a published, keyless fixture.
XPUB = ("xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoC"
        "u1Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8")
ESPLORA = "https://blockstream.info/api"
GAP = 20

# ============================================================ bitcoin
parsed = parse_xpub(XPUB)
assert parsed.depth == 0 and len(parsed.chain_code) == 32 and len(parsed.pubkey) == 33
print(f"parsed xpub: depth={parsed.depth}, pubkey {parsed.pubkey.hex()[:20]}…")

# `derive(chain, start, count)` is the seam `scan` drives. Binding the key
# with partial() means the scanner never receives it.
derive = functools.partial(derive_addresses, XPUB, "p2wpkh")
external = derive(0, 0, 3)
assert external[0] == "bc1qp5wfcq48h6d63wyy9qz0awtpfqwwv4sma86mhz"
print("derived in-process, no network, no C dependency:")
for index, address in enumerate(external):
    print(f"  m/0/{index}  {address}")

# This wallet: two used receive addresses (one swept), one used change
# address, everything else empty. `funded - spent` is the confirmed balance.
FUNDED = {
    (0, 0): (100_000_000, 0, 3),        # 1 BTC, never spent
    (0, 1): (60_000_000, 60_000_000, 2),  # used and fully swept -> 0, still used
    (0, 2): (25_000, 0, 1),
    (1, 0): (999_000_000, 0, 12),       # change
}


def address_row(chain: int, index: int) -> dict:
    """One `GET /address/{addr}` response. mempool_stats is ignored by design:
    an unconfirmed balance is not a balance."""
    funded, spent, count = FUNDED.get((chain, index), (0, 0, 0))
    address = derive(chain, index, 1)[0]
    return {"request": {"method": "GET", "url": f"{ESPLORA}/address/{address}"},
            "response": {"status": 200, "json": {
                "address": address,
                "chain_stats": {"funded_txo_sum": funded, "spent_txo_sum": spent,
                                "tx_count": count},
                "mempool_stats": {"funded_txo_sum": 7_777, "spent_txo_sum": 0,
                                  "tx_count": 1}}}}


# Exactly the addresses a gap-20 scan can reach: the used ones plus the 20
# empties that stop each chain.
CASSETTE = {"interactions": [address_row(0, index) for index in range(23)]
                            + [address_row(1, index) for index in range(21)]}

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "esplora.json"
    path.write_text(json.dumps(CASSETTE), encoding="utf-8")
    cassette = load(path)

    requested: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return cassette.handle(request)

    client = httpx.Client(transport=httpx.MockTransport(recording))
    result = scan(Esplora(client, base_url=ESPLORA), derive, gap=GAP)

# THE security property, asserted against the traffic: every request is a
# derived address, and the xpub is in none of them.
assert all(url.rsplit("/", 1)[-1].startswith("bc1") for url in requested)
assert not any("xpub" in url for url in requested)
assert len(requested) == 44          # 23 external + 21 change: the stop rule

print(f"\n{len(requested)} address lookups; not one carried the extended key")
for row in result.addresses:
    print(f"  m/{row.chain}/{row.index}  {row.address}  "
          f"{row.balance_sats:>11} sats  ({row.tx_count} tx)")
assert [(row.chain, row.index, row.balance_sats) for row in result.addresses] == [
    (0, 0, 100_000_000), (0, 1, 0), (0, 2, 25_000), (1, 0, 999_000_000)]
assert result.total == Quantity(1_099_025_000, 8) and str(result.total) == "10.99025"
print(f"  {'TOTAL':>28} {result.total_sats:>17} sats = {result.total} BTC")
print(f"  asset id: {result.caip19}")
print("  m/0/1 was swept to zero and is still reported: it is part of the wallet")

# ============================================================= solana
OWNER = "9wFFyRfZBsuAha4YcuxcXLKwMxJR43S7fPfQLXMFxbAF"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
T22_MINT = "ScaLedUiAmountMint22222222222222222222222222"
RPC_URL = "https://api.mainnet-beta.solana.com"


def token_account(pubkey: str, program: str, mint: str, amount: str, decimals: int,
                  ui: str, extensions: list | None = None) -> dict:
    info = {"mint": mint, "owner": OWNER, "state": "initialized",
            "tokenAmount": {"amount": amount, "decimals": decimals,
                            "uiAmount": float(ui), "uiAmountString": ui}}
    if extensions is not None:
        info["extensions"] = extensions
    return {"pubkey": pubkey,
            "account": {"data": {"program": program,
                                 "parsed": {"type": "account", "info": info}}}}


def rpc_reply(method: str, params: list, result: object) -> dict:
    return {"request": {"method": "POST", "url": RPC_URL,
                        "json": {"jsonrpc": "2.0", "id": 1,
                                 "method": method, "params": params}},
            "response": {"status": 200,
                         "json": {"jsonrpc": "2.0", "id": 1, "result": result}}}


SOLANA = {"interactions": [
    rpc_reply("getBalance", [OWNER], {"context": {"slot": 1}, "value": 3_500_000_000}),
    # Two SPL accounts of the SAME mint: they sum, they do not shadow.
    rpc_reply("getTokenAccountsByOwner",
              [OWNER, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed"}],
              {"context": {"slot": 1}, "value": [
                  token_account("Acct1", "spl-token", USDC_MINT, "250000000", 6, "250"),
                  token_account("Acct2", "spl-token", USDC_MINT, "750000000", 6, "750"),
              ]}),
    # A Token-2022 mint with a x2 ScaledUiAmount multiplier.
    rpc_reply("getTokenAccountsByOwner",
              [OWNER, {"programId": TOKEN_2022_PROGRAM}, {"encoding": "jsonParsed"}],
              {"context": {"slot": 1}, "value": [
                  token_account("Acct3", "spl-token-2022", T22_MINT, "1000000000", 9, "2",
                                extensions=[{"extension": "scaledUiAmountConfig",
                                             "state": {"multiplier": "2"}}]),
              ]}),
]}

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "solana.json"
    path.write_text(json.dumps(SOLANA), encoding="utf-8")

    posted: list[str] = []
    solana_cassette = load(path)

    def recording_post(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content)["method"])
        return solana_cassette.handle(request)

    rpc = SolanaRpc(httpx.Client(transport=httpx.MockTransport(recording_post)),
                    url=RPC_URL)
    balances = SolanaBalances(rpc).balances(OWNER)

# Both token programs are asked, in a pinned order: a wallet that only knows
# the original SPL program silently misses every Token-2022 balance.
assert posted == ["getBalance", "getTokenAccountsByOwner", "getTokenAccountsByOwner"]
native, usdc, scaled = balances

print(f"\n{len(posted)} RPC calls -> {len(balances)} balances")
print(f"  {'raw/10^decimals':>16}{'node says':>12}  scaled_ui  asset")
for balance in balances:
    print(f"  {str(balance.quantity):>16}{balance.ui_amount_string:>12}"
          f"  {str(balance.scaled_ui):<9}  {balance.caip19.split('/')[-1][:34]}")

assert str(native.quantity) == "3.5" and native.mint is None
assert usdc.quantity == Quantity(1_000_000_000, 6)      # 250M + 750M, summed
assert usdc.ui_amount_string == "1000" and usdc.scaled_ui is False
# The interesting row: 1 by the usual identity, 2 by the mint's own rule.
assert str(scaled.quantity) == "1" and scaled.ui_amount_string == "2"
assert scaled.scaled_ui is True
print("\n  two token accounts of one mint summed to "
      f"{usdc.quantity.as_decimal():f} USDC")
print(f"  Token-2022: raw/10^9 = {scaled.quantity}, the mint displays "
      f"{scaled.ui_amount_string}, scaled_ui={scaled.scaled_ui}")
print("  both numbers are carried, so a caller can never quietly show the wrong one")

# Solana transaction DECODE is not implemented: balances and signature
# history only (README, *What is not there*). `rpc.get_signatures(address)`
# pages history if you want to build on it.
print("\nOK: an xpub that never left the process, and a token that breaks the identity.")
