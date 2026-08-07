"""How do I read a contract straight from a node, with no aggregator?

    pip install auradefi
    python examples/12_read_a_contract_from_a_node.py

Until 0.2.0 every EVM read in this package went through the Etherscan V2
aggregator, and the DeFi adapters ran against hand-written fixtures because
the package had no `eth_call` of its own. It has one now, in six pieces:

    codec/keccak.py    keccak256, stdlib only, so no dependency was added
    codec/abi.py       selectors, static words, and Multicall3's two shapes
    rpc.py             JSON-RPC 2.0: single calls and an id-matched batch
    multicall.py       Multicall3 aggregate3, one revert isolated to one call
    logs.py            eth_getLogs, chunked over a block range, typed rows
    reader.py          EvmContractReader: the seam the adapters already speak

This file drives all six against a stand-in node built with
`httpx.MockTransport`, so it runs offline. Pointing it at a real chain is the
URL on the line marked NODE_URL, and nothing else. The answers below are the
integers this project's golden vectors pin at block 20,450,000, packed into
words by hand. No recording in this repository came from a real node, so what
runs here proves the path and not the chain state.

Four properties worth watching for, because each is a defect somewhere else:

* a reverting call in a batch of five costs you the one call, not the batch;
* a batch is matched by JSON-RPC `id`, so a node that answers out of order is
  still answered correctly;
* an empty or short result raises `SourceError` instead of decoding to zero,
  so a call to a non-contract address never reads as an empty wallet;
* the block pin lives on the reader, so a report at block N cannot silently
  mix in a read at head.
"""

from __future__ import annotations

import json

import httpx

from auradefi.errors import SourceError
from auradefi.positions.adapters.staking.liquid import RocketPoolAdapter
from auradefi.positions.protocol import DiscoveryContext, ResolveContext
from auradefi.sources.evm.codec.abi import (
    encode,
    function_signature,
    selector,
)
from auradefi.sources.evm.codec.keccak import keccak256
from auradefi.sources.evm.logs import scan_logs
from auradefi.sources.evm.multicall import MULTICALL3_ADDRESS, Call, Multicall3
from auradefi.sources.evm.reader import EvmContractReader
from auradefi.sources.evm.rpc import EvmRpc, block_tag

NODE_URL = "https://evm-node.invalid/rpc"   # <- your node goes here
CHAIN = "eip155:1"
BLOCK = 20_450_000
HOLDER = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
RETH = "0xae78736cd615f374d3085123a210448e74fc6393"
DEAD = "0x000000000000000000000000000000000000dead"

# The same integers the shipped golden vectors pin at this block.
RETH_BALANCE = 2_500_000_000_000_000_000        # 2.5 rETH
RETH_RATE = 1_120_000_000_000_000_000           # getExchangeRate: 1.12
RETH_REDEEMED = 2_800_000_000_000_000_000       # 2.5 * 1.12, exact
USDC_BALANCE = 12_345_678_901

TRANSFER_TOPIC = "0x" + keccak256(b"Transfer(address,address,uint256)").hex()


# ------------------------------------------------------- 1. keccak and selectors
# hashlib ships sha3_256, which is NOT keccak256: the padding byte differs, so
# every selector derived from it addresses a function that does not exist. The
# published empty-string vector is the cheapest way to know which one you have.
assert keccak256(b"").hex() == (
    "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
)
assert selector("balanceOf(address)").hex() == "70a08231"
assert function_signature("getReserves", ()) == "getReserves()"
print("keccak256(b'') =", keccak256(b"").hex())
print("balanceOf(address) selector =", "0x" + selector("balanceOf(address)").hex())


# ------------------------------------------------------------- 2. a stand-in node
# A real node would answer these. Replace `client` with a plain httpx.Client
# and every line below this section is unchanged.
def _word(value: int) -> str:
    return f"{value:064x}"


def _aggregate3_return(results: list[tuple[bool, bytes]]) -> str:
    """Pack `(bool,bytes)[]` the way Multicall3 returns it."""
    elements = [
        _word(int(ok)) + _word(0x40) + _word(len(data)) + data.hex().ljust(
            64 * ((len(data) + 31) // 32), "0"
        )
        for ok, data in results
    ]
    offsets, running = [], 32 * len(elements)
    for element in elements:
        offsets.append(_word(running))
        running += len(element) // 2
    return "0x" + _word(0x20) + _word(len(elements)) + "".join(offsets + elements)


#: (target, selector) -> the result hex the node sends back.
ANSWERS: dict[tuple[str, str], str] = {
    (USDC, "0x313ce567"): "0x" + _word(6),                 # decimals()
    (USDC, "0x70a08231"): "0x" + _word(USDC_BALANCE),      # balanceOf(address)
    (WETH, "0x313ce567"): "0x" + _word(18),
    (RETH, "0x70a08231"): "0x" + _word(RETH_BALANCE),
    (RETH, "0xe6aa216c"): "0x" + _word(RETH_RATE),         # getExchangeRate()
}

LOG_ROWS = [
    {
        "address": USDC,
        "topics": [TRANSFER_TOPIC, "0x" + _word(0), "0x" + _word(int(HOLDER, 16))],
        "data": "0x" + _word(USDC_BALANCE),
        "blockNumber": hex(BLOCK - 1),
        "transactionHash": "0x" + "11" * 32,
        "logIndex": "0x0",
    }
]


def node(request: httpx.Request) -> httpx.Response:
    """The whole fake node: eth_call, eth_getBalance, eth_blockNumber, logs."""
    body = json.loads(request.content)
    if isinstance(body, list):                     # a JSON-RPC batch, answered
        return httpx.Response(200, json=[          # in REVERSED id order
            {"jsonrpc": "2.0", "id": item["id"], "result": _answer(item)}
            for item in reversed(body)
        ])
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _answer(body)})


def _answer(item: dict) -> object:
    method, params = item["method"], item["params"]
    if method == "eth_blockNumber":
        return hex(BLOCK)
    if method == "eth_getBalance":
        return hex(4_000_000_000_000_000_000)
    if method == "eth_getLogs":
        return LOG_ROWS if params[0]["fromBlock"] == block_tag(BLOCK - 1) else []
    to, data = params[0]["to"], params[0]["data"]
    if to == MULTICALL3_ADDRESS:
        return FIVE_RESULTS
    # "0x" is what a node returns for a call to an address holding no code,
    # and the reader raises on it rather than reading it as zero.
    return ANSWERS.get((to, data[:10]), "0x")


#: Four answers and one declared failure, in request order. The reverting leg
#: carries empty returndata: DECLARED, never a zero word substituted for it.
FIVE_RESULTS = _aggregate3_return([
    (True, bytes.fromhex(_word(6))),
    (True, bytes.fromhex(_word(RETH_BALANCE))),
    (True, bytes.fromhex(_word(18))),
    (False, b""),
    (True, bytes.fromhex(_word(RETH_RATE))),
])

client = httpx.Client(transport=httpx.MockTransport(node), base_url=NODE_URL)
rpc = EvmRpc(client, NODE_URL)


# ---------------------------------------------------------------- 3. the raw RPC
assert rpc.eth_block_number() == BLOCK
assert rpc.eth_get_balance(HOLDER, block_tag(BLOCK)) == 4_000_000_000_000_000_000
calldata = "0x" + (
    selector(function_signature("balanceOf", ("address",)))
    + encode(("address",), (HOLDER,))
).hex()
raw = rpc.eth_call(USDC, calldata, block_tag(BLOCK))
print(f"\neth_call USDC.balanceOf at block {BLOCK}: {raw}")
assert int(raw, 16) == USDC_BALANCE

# A batch of two, answered by the node in reversed id order and returned in
# REQUEST order regardless. Matching by array position is the defect here.
first, second = rpc.batch([
    ("eth_blockNumber", []),
    ("eth_call", [{"to": USDC, "data": "0x313ce567"}, block_tag(BLOCK)]),
])
assert int(first.result, 16) == BLOCK and int(second.result, 16) == 6
print(f"batch out of order, matched by id: head={int(first.result, 16)}, "
      f"USDC decimals={int(second.result, 16)}")


# ------------------------------------------------------------------- 4. the reader
# `EvmContractReader.call(address, fn, args)` IS the adapter seam. It resolves
# the ABI types from a registry keyed by function name, so a caller writes the
# name and never a signature string, and an unknown name with arguments is
# refused before any HTTP rather than guessed into a wrong selector.
reader = EvmContractReader(rpc, block_number=BLOCK)
assert reader.call(USDC, "decimals") == 6
assert reader.call(USDC, "balanceOf", (HOLDER,)) == USDC_BALANCE
print(f"\nreader: USDC decimals={reader.call(USDC, 'decimals')}, "
      f"balance={reader.call(USDC, 'balanceOf', (HOLDER,))}")

# A read the node cannot answer is a SourceError, never a zero. That matters
# most for an address that holds no contract at all: read as zero, an empty
# account reports as a balance of nothing owned rather than as a failed read.
try:
    reader.call(DEAD, "decimals")
    raise SystemExit("an empty result must not decode")
except SourceError as failure:
    print(f"empty result declared: {type(failure).__name__}: {failure}")


# -------------------------------------------- 5. a shipped adapter, over the node
# Nothing below knows a node exists. The adapter asks the seam; the seam is now
# a real one. These are the same numbers `tests/golden/test_positions_*.py`
# pin against hand-written fixtures.
adapter = RocketPoolAdapter()
contracts = adapter.discover(DiscoveryContext(chain_id=CHAIN, reader=reader))
(position,) = adapter.resolve(
    ResolveContext(chain_id=CHAIN, address=HOLDER, reader=reader, block_number=BLOCK),
    contracts,
)
(underlying,) = position.underlyings
assert underlying.quantity.raw == RETH_REDEEMED
print(f"\n{position.adapter_id}: {position.id} redeems "
      f"{underlying.quantity} ETH from 2.5 rETH at rate 1.12")


# ------------------------------------------------- 6. five reads, one round trip
# Multicall3's whole reason for existing here is `allowFailure`. One reverting
# call comes back declared, and its four neighbours keep their answers.
multicall = Multicall3(rpc)
results = multicall.aggregate3([
    Call(USDC, bytes.fromhex("313ce567")),
    Call(RETH, bytes.fromhex("70a08231") + encode(("address",), (HOLDER,))),
    Call(WETH, bytes.fromhex("313ce567")),
    Call(DEAD, bytes.fromhex("313ce567")),          # this one reverts
    Call(RETH, bytes.fromhex("e6aa216c")),
], block_number=BLOCK)
assert [result.success for result in results] == [True, True, True, False, True]
assert results[3].data == b""
assert int.from_bytes(results[1].data, "big") == RETH_BALANCE
print(f"\naggregate3 of 5 in one eth_call: "
      f"{sum(r.success for r in results)} answered, "
      f"{sum(not r.success for r in results)} declared failure")


# ----------------------------------------------------------------- 7. log scanning
# One eth_getLogs per chunk over an inclusive range, with typed rows out. The
# decode handlers in later releases read transfers through this.
records = scan_logs(
    rpc,
    from_block=BLOCK - 1,
    to_block=BLOCK,
    address=USDC,
    topics=[TRANSFER_TOPIC],
    chunk_blocks=1,
)
(record,) = records
assert record.address == USDC and record.topics[0] == TRANSFER_TOPIC
assert int.from_bytes(record.data, "big") == USDC_BALANCE
print(f"\nscan_logs over 2 blocks in 1-block chunks: {len(records)} Transfer, "
      f"block {record.block_number}, removed={record.removed}")

print("\nOK: selectors, one call, a batch, five in one, logs, and an adapter.")
