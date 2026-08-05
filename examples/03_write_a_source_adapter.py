"""How do I point this at MY chain data — my RPC, my vendor, my archive?

    pip install auradefi && python 03_write_a_source_adapter.py

`Auradefi` needs one object satisfying two seams. That object is the whole
of your integration surface:

    balances(chain_id, address) -> list[BalanceRecord]
        what the address holds NOW. Feeds holdings and pricing.

    fetch_txlist(chain_id, address, *, start_block, end_block,
                 page, offset, sort) -> list[dict]
        one PAGE of raw history rows for that window. Feeds the ledger.
        The engine drives the window; you just answer it.

Both are structural (`typing.Protocol`) — no base class to inherit, no
registration. This file writes a real HTTP adapter over Etherscan V2, run
against a cassette so it works offline, and shows what the engine actually
asks for and in what order. The row keys `fetch_txlist` returns are the
decoder's contract, listed below; return your own shape instead and bind a
`decoder=` of your own.

A note on failures: raise `auradefi.errors.SourceError` (or any
`AuradefiError`) for an upstream problem and the library contains it to the
one connection. Let a `KeyError` escape and it propagates — that is your
bug, and hiding it would be worse.
"""

from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from pathlib import Path

from auradefi import Auradefi
from auradefi.clock import FrozenClock
from auradefi.errors import SourceError
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.money.fiat import Money
from auradefi.sources.evm.etherscan import EtherscanV2
from auradefi.testing.cassettes import load

CHAIN, CHAIN_NUMBER = "eip155:1", 1
ADDRESS = "0x1111111111111111111111111111111111111111"
BASE = "https://api.etherscan.io/v2/api"

#: The nine keys `decode.pipeline`'s default decoder reads off a history
#: row. Extra keys are ignored; a missing or non-string one raises
#: SourceError naming the key, rather than being decoded as a zero.
ROW_KEYS = ("hash", "blockNumber", "timeStamp", "from", "to",
            "value", "gasUsed", "gasPrice", "isError")


class EtherscanSource:
    """Both seams over one Etherscan V2 key, for every chain it covers.

    `balances` delegates to the shipped `EtherscanV2` client. `fetch_txlist`
    is ~15 lines because the engine has already decided the window — your
    job is one HTTP request and the rows, in delivery order.
    """

    def __init__(self, client, api_key: str) -> None:
        self._client = client
        self._api_key = api_key
        self._balances = EtherscanV2(client, api_key=api_key)
        self.windows: list[str] = []      # so this example can show them

    def balances(self, chain_id: str, address: str):
        return self._balances.balances(chain_id, address)

    def fetch_txlist(self, chain_id: str, address: str, *, start_block: int,
                     end_block: int, page: int, offset: int, sort: str) -> list[dict]:
        self.windows.append(f"blocks {start_block}-{end_block} page={page} "
                            f"offset={offset} sort={sort}")
        chain_number = int(chain_id.split(":")[1])
        url = (f"{BASE}?chainid={chain_number}&module=account&action=txlist"
               f"&address={address}&startblock={start_block}&endblock={end_block}"
               f"&page={page}&offset={offset}&sort={sort}&apikey={self._api_key}")
        response = self._client.get(url)
        if response.status_code != 200:
            raise SourceError(f"etherscan {response.status_code} for {chain_id}")
        body = response.json()
        # Etherscan says "no data" with status "0", which is not an error.
        if body.get("status") == "0":
            if body.get("message") == "No transactions found":
                return []
            raise SourceError(f"etherscan: {body.get('message') or body.get('result')}")
        rows = body["result"]
        if not isinstance(rows, list):
            raise SourceError(f"etherscan returned {type(rows).__name__}, not a list")
        return rows


class LlamaPrices:
    """Your price port. Anything with `usd_prices(ids) -> {id: Money}`."""

    def usd_prices(self, caip19s):
        return {caip19: Money(Decimal("3000"), "USD") for caip19 in caip19s
                if caip19.endswith("slip44:60")}


def row(index: int) -> dict:
    return {"hash": "0x" + f"{index + 1:02x}" * 32,
            "blockNumber": str(18_000_000 + index),
            "timeStamp": str(1_753_000_000 + index * 600),
            "from": "0x" + "99" * 20, "to": ADDRESS, "value": str(10**18),
            "gasUsed": "21000", "gasPrice": "10000000000", "isError": "0"}


def txlist(start: int, end: int, page: int, offset: int, sort: str,
           rows: list[dict], key: str = "DEMOKEY", envelope: dict | None = None) -> dict:
    url = (f"{BASE}?chainid=1&module=account&action=txlist&address={ADDRESS}"
           f"&startblock={start}&endblock={end}&page={page}&offset={offset}"
           f"&sort={sort}&apikey={key}")
    body = envelope or {"status": "1", "message": "OK", "result": rows}
    return {"request": {"method": "GET", "url": url},
            "response": {"status": 200, "json": body}}


HEAD = 99_999_999
CASSETTE = {"interactions": [
    # 1. connect: the liveness probe — ONE row, newest first, cheapest
    #    possible request. A dead address or a dead key fails HERE.
    txlist(0, HEAD, 1, 1, "desc", [row(2)]),
    # 2. first sync ever: the ANCHOR page, newest first. It fixes the two
    #    cursors — the newest block seen (live) and the oldest (backfill).
    txlist(0, HEAD, 1, 3, "desc", [row(2), row(1), row(0)]),
    # 3. a full anchor page means there may be older history, so the
    #    backfill walks one FIXED window [0, oldest_seen] backwards. A short
    #    page — here, empty — is the proof it drained, and ends the phase.
    txlist(0, 18_000_000, 1, 3, "desc", []),
    # 4. holdings, when you ask for them.
    {"request": {"method": "GET",
                 "url": f"{BASE}?chainid=1&module=account&action=balance"
                        f"&address={ADDRESS}&tag=latest&apikey=DEMOKEY"},
     "response": {"status": 200, "json": {"status": "1", "message": "OK",
                                          "result": "4000000000000000000"}}},
    {"request": {"method": "GET",
                 "url": f"{BASE}?chainid=1&module=account&action=tokentx"
                        f"&address={ADDRESS}&startblock=0&endblock=99999999"
                        "&page=1&offset=1000&sort=asc&apikey=DEMOKEY"},
     "response": {"status": 200, "json": {"status": "0",
                                          "message": "No transactions found",
                                          "result": []}}},
    # 5. and the same window asked for with a bad key, for the last section.
    txlist(0, HEAD, 1, 1, "desc", [], key="WRONGKEY",
           envelope={"status": "0", "message": "NOTOK", "result": "Invalid API Key"}),
]}

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "adapter.json"
    path.write_text(json.dumps(CASSETTE), encoding="utf-8")

    source = EtherscanSource(load(path).client(), api_key="DEMOKEY")
    auradefi = Auradefi(MemoryLedger(), source, LlamaPrices(),
                        FrozenClock(1_754_000_000_000), sync_page_size=3)

    user = auradefi.user("user-42")
    connection = user.connect_address(CHAIN, ADDRESS)
    report = auradefi.sync(budget=5)
    (holdings,) = auradefi.holdings()

    # When upstream misbehaves: anything that is not a page of rows is an
    # AuradefiError, which the sync loop turns into that connection's
    # `failed=True` row. Nothing silently becomes an empty page — an empty
    # page means "no more history", and the cursor would then advance over
    # data you never saw.
    broken = EtherscanSource(load(path).client(), api_key="WRONGKEY")
    try:
        broken.fetch_txlist(CHAIN, ADDRESS, start_block=0, end_block=HEAD,
                            page=1, offset=1, sort="desc")
        raise AssertionError("a NOTOK envelope must not read as an empty page")
    except SourceError as exc:
        refusal = str(exc)

print("what the engine asked this adapter for, in order:")
for index, window in enumerate(source.windows, start=1):
    print(f"  {index}. {window}")

assert source.windows[0].endswith("offset=1 sort=desc"), "connect probes with one row"
assert report.transactions_ingested == 3
assert holdings.total_value == Money(Decimal("12000"), "USD")
print(f"\nconnected {connection.id}")
print(f"sync: {report.pages_fetched} pages, {report.transactions_ingested} transactions")
print(f"holdings: {holdings.total_value}")
print(f"\nrow keys the default decoder reads: {', '.join(ROW_KEYS)}")

print(f"\nbad key -> SourceError: {refusal} "
      "(contained per connection, never a silent empty page)")

print("\nOK — two methods, and any chain data source becomes an auradefi source.")
