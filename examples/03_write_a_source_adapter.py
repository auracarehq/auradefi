"""How do I point this at MY chain data: my RPC, my vendor, my archive?

    pip install auradefi
    python examples/03_write_a_source_adapter.py

**You may not have to.** `EtherscanSource` ships and satisfies both seams
over one Etherscan V2 key, and `Auradefi.from_env()` binds it for you. This
guide is for when you want something else behind it: an internal service, a
vendor SDK, an archive node, a queue.

A source is one object with two methods:

    balances(chain_id, address) -> list[BalanceRecord]
        what the address holds NOW. Feeds holdings and pricing.

    Fetch_txlist(chain_id, address, *, start_block, end_block,
                 page, offset, sort) -> list[dict]
        ONE page of raw history rows for exactly that window.

Both are structural (`typing.Protocol`), no base class, no registration,
no import. Get the shape right and the facade accepts it; get it wrong and
it refuses at BIND time rather than on a background tick.

The rule that matters: **the engine owns the window.** It picks the blocks,
the page and the sort order, and it learns that a window drained by getting
a page shorter than `offset`. So answer the window you were asked for,
never widen it, never page internally, never retry silently.
"""

from __future__ import annotations

from decimal import Decimal

from auradefi import Auradefi
from auradefi.embed.sync import PageFetcher
from auradefi.errors import SourceError, ValidationError
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.portfolio.holdings import BalanceSource
from auradefi.sources.evm.etherscan import BalanceRecord

ETH = "eip155:1/slip44:60"
WALLET = "0x1111111111111111111111111111111111111111"


class MySource:
    """Both seams over whatever you already have. This one is a dict."""

    #: Pretend history: block -> the rows mined in it.
    HISTORY = {block: [{"hash": "0x" + f"{block:02x}" * 32,
                        "blockNumber": str(block),
                        "timeStamp": str(1_753_000_000 + block),
                        "from": "0x" + "99" * 20, "to": WALLET,
                        "value": "1000000000000000000", "gasUsed": "21000",
                        "gasPrice": "10000000000", "isError": "0"}]
               for block in (100, 101, 102)}

    def __init__(self) -> None:
        self.windows: list[str] = []

    def balances(self, chain_id: str, address: str) -> list[BalanceRecord]:
        return [BalanceRecord(caip19=ETH, symbol="ETH",
                              quantity=Quantity(3 * 10**18, 18),
                              contract_address=None)]

    def fetch_txlist(self, chain_id: str, address: str, *, start_block: int,
                     end_block: int, page: int, offset: int, sort: str) -> list[dict]:
        self.windows.append(f"blocks {start_block}-{end_block} page={page} "
                            f"offset={offset} sort={sort}")
        blocks = sorted(b for b in self.HISTORY if start_block <= b <= end_block)
        if sort == "desc":
            blocks.reverse()
        rows = [row for block in blocks for row in self.HISTORY[block]]
        # Answer THIS page of THIS window. The engine did the arithmetic.
        window = rows[(page - 1) * offset:page * offset]
        if not window and page == 1 and start_block == 0:
            raise SourceError("upstream said nothing at all")   # never silent
        return window


class MyPrices:
    """Your price feed: CAIP-19 ids in, `Money` out. Absent is allowed.
    An asset you cannot price comes back unpriced, never as zero."""

    def usd_prices(self, caip19s):
        return {ETH: Money(Decimal("3000"), "USD")}


# ---------------------------------------------------- the seams, checked early
source = MySource()
assert isinstance(source, BalanceSource)   # has balances
assert isinstance(source, PageFetcher)     # has fetch_txlist
print("seams satisfied:", [name for name in ("balances", "fetch_txlist")])

# A source missing a seam is refused HERE, not on tick one.
try:
    Auradefi(MemoryLedger(), object(), MyPrices())
except ValidationError as exc:
    print(f"  a bad source at bind time: {exc}")

# ------------------------------------------------------------- drive it
aura = Auradefi(MemoryLedger(), source, MyPrices(), sync_page_size=2)
user = aura.user("user-42")
user.connect_address("eip155:1", WALLET)
report = aura.sync(budget=5)
(holdings,) = aura.holdings()

print("\nwhat the engine asked for, in order:")
for index, window in enumerate(source.windows, start=1):
    print(f"  {index}. {window}")
print(f"\nsync: {report.pages_fetched} pages, {report.transactions_ingested} "
      f"transactions; holdings {holdings.total_value}")

# The first request is always the cheapest possible one: a single-row probe
# at connect time, so a dead endpoint or a bad key fails while your user is
# still looking at the screen.
assert source.windows[0].endswith("offset=1 sort=desc")
print("\nnote the first window: offset=1: the connect-time liveness probe")

# ------------------------------------------------------ failure, on purpose
# Raise `SourceError` (or any `AuradefiError`) for an upstream problem and
# `sync()` contains it to that one connection's report row. Anything else
# propagates untouched, because a KeyError in your adapter is your bug and
# hiding it would be worse than a loud tick.
print("\nOK: two methods, and any data source you already have becomes one.")
