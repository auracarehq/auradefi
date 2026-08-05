"""DefiLlama oracle (SPEC §3.2, §6.3): pinned coin_key mapping, the
deterministic sorted-chunked request layout, Decimal(str(price)) conversion,
SourceError on failure: all offline (cassette + inline MockTransport)."""

from __future__ import annotations

import ast
import importlib
import inspect
import random
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from auradefi.errors import SourceError
from auradefi.money.fiat import Money
from auradefi.prices.oracles import defillama
from auradefi.prices.oracles.defillama import DefiLlamaOracle, chunk_keys, coin_key

DAI = "eip155:1/erc20:0x6B175474E89094C44Da98b954EedeAC495271d0F"
NATIVE_ETH = "eip155:1/slip44:60"
ETH_PRICE = Decimal("3584.17")


def _recording_client(payload, status: int = 200) -> tuple[httpx.Client, list[str]]:
    """A client whose transport records every request URL: zero-HTTP and
    request-layout assertions read the list."""
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler)), urls


# --- coin_key: pinned golden vectors ---------------------------------------


@pytest.mark.parametrize(
    ("caip19", "expected"),
    [
        (  # USDC on Ethereum: slug + address lowercased
            "eip155:1/erc20:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "ethereum:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        ),
        (  # mixed-case address on Base
            "eip155:8453/erc20:0xABCDEF0123456789abcdef0123456789ABCDEF01",
            "base:0xabcdef0123456789abcdef0123456789abcdef01",
        ),
        (
            "eip155:10/erc20:0x4200000000000000000000000000000000000042",
            "optimism:0x4200000000000000000000000000000000000042",
        ),
        (
            "eip155:56/erc20:0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56",
            "bsc:0xe9e7cea3dedca5984780bafc599bd69add087d56",
        ),
        (
            "eip155:137/erc20:0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
            "polygon:0x2791bca1f2de4661ed88a30c99a7a9449aa84174",
        ),
        (
            "eip155:42161/erc20:0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",
            "arbitrum:0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",
        ),
    ],
)
def test_coin_key_erc20_slug_table_and_lowercasing(caip19, expected):
    assert coin_key(caip19) == expected


@pytest.mark.parametrize(
    "caip19",
    ["eip155:1/slip44:60", "eip155:10/slip44:60",
     "eip155:8453/slip44:60", "eip155:42161/slip44:60"],
)
def test_coin_key_native_eth_chains_map_to_coingecko_ethereum(caip19):
    assert coin_key(caip19) == "coingecko:ethereum"


@pytest.mark.parametrize(
    "caip19",
    [
        "eip155:56/slip44:60",  # BNB is not ether
        "eip155:137/slip44:60",  # POL is not ether
        "bip122:000000000019d6689c085ae165831e93/slip44:0",  # BTC
        "eip155:250/erc20:0x04068DA6C83AFCFA0e13ba15A6696662335D5B75",  # no slug
        "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/token:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "eip155:1/erc721:0xBC4CA0EdA7647A8aB7C2061c2E118A18a936f13D",  # not erc20
        "",
        "garbage",
    ],
)
def test_coin_key_everything_else_is_none(caip19):
    assert coin_key(caip19) is None


# --- chunk_keys: the pure request layout ------------------------------------


def test_chunk_keys_250_keys_chunk_100_100_50_in_global_sorted_order():
    keys = [f"ethereum:0x{i:040x}" for i in range(250)]
    expected = sorted(keys)  # zero-padded hex: lexicographic == numeric
    scrambled = list(keys)
    random.Random(1754).shuffle(scrambled)
    scrambled += scrambled[:7]  # duplicates must collapse

    chunks = chunk_keys(scrambled)

    assert [len(chunk) for chunk in chunks] == [100, 100, 50]
    assert chunks == [expected[0:100], expected[100:200], expected[200:250]]


def test_chunk_keys_dedupes_and_sorts():
    assert chunk_keys(["b", "a", "b", "a", "c"]) == [["a", "b", "c"]]


def test_chunk_keys_empty_input_is_empty():
    assert chunk_keys([]) == []


def test_chunk_keys_exactly_one_over_the_boundary():
    keys = [f"k{i:03d}" for i in range(101)]
    chunks = chunk_keys(list(reversed(keys)))
    assert [len(chunk) for chunk in chunks] == [100, 1]
    assert chunks[0][0] == "k000"
    assert chunks[1] == ["k100"]


# --- usd_prices against the committed cassette ------------------------------


def test_cassette_happy_path_prices_eth_leaves_dai_unpriced(cassette):
    # The cassette records exactly GET /prices/current/
    # coingecko:ethereum,ethereum:0x6b175474e89094c44da98b954eedeac495271d0f
    #: any other URL (wrong sort, wrong case, wrong join) raises
    # CassetteMissError. DAI is in the recorded request but absent from the
    # response body, so it is unpriced: absent from the result.
    oracle = DefiLlamaOracle(cassette("defillama_prices").client())

    result = oracle.usd_prices([NATIVE_ETH, DAI])

    assert result == {NATIVE_ETH: Money(ETH_PRICE, "USD")}


def test_price_is_decimal_str_pinned_never_decimal_of_float(cassette):
    # The pin's point: Decimal from the raw float is NOT the quoted price.
    assert Decimal(3584.17) != Decimal("3584.17")

    oracle = DefiLlamaOracle(cassette("defillama_prices").client())
    amount = oracle.usd_prices([NATIVE_ETH, DAI])[NATIVE_ETH].amount

    assert isinstance(amount, Decimal)
    assert amount == Decimal("3584.17")
    # byte-exact representation, not merely numeric equality
    assert amount.as_tuple() == Decimal("3584.17").as_tuple()


def test_http_500_raises_source_error(cassette):
    oracle = DefiLlamaOracle(cassette("defillama_prices").client())
    with pytest.raises(SourceError):
        oracle.usd_prices(
            ["eip155:1/erc20:0x0000000000000000000000000000000000000bad"]
        )


# --- usd_prices behaviour not in the cassette (inline offline transports) ---


def test_no_mappable_input_returns_empty_dict_with_zero_http():
    client, urls = _recording_client({"coins": {}})
    oracle = DefiLlamaOracle(client)

    result = oracle.usd_prices(
        ["eip155:56/slip44:60", "bip122:000000000019d6689c085ae165831e93/slip44:0"]
    )

    assert result == {}
    assert urls == []


def test_construction_performs_zero_http():
    client, urls = _recording_client({"coins": {}})
    DefiLlamaOracle(client)
    assert urls == []


def test_ids_sharing_one_key_are_deduplicated_then_fanned_back_out():
    payload = {
        "coins": {
            "coingecko:ethereum": {
                "price": 3584.17, "symbol": "ETH",
                "timestamp": 1754089200, "confidence": 0.99,
            }
        }
    }
    client, urls = _recording_client(payload)
    oracle = DefiLlamaOracle(client)

    result = oracle.usd_prices(
        ["eip155:1/slip44:60", "eip155:42161/slip44:60", "eip155:1/slip44:60"]
    )

    assert urls == ["https://coins.llama.fi/prices/current/coingecko:ethereum"]
    assert result == {
        "eip155:1/slip44:60": Money(ETH_PRICE, "USD"),
        "eip155:42161/slip44:60": Money(ETH_PRICE, "USD"),
    }


def test_150_keys_issue_two_chunked_requests_in_global_sorted_order():
    ids = [f"eip155:1/erc20:0x{i:040x}" for i in range(150)]
    expected_keys = sorted(f"ethereum:0x{i:040x}" for i in range(150))
    client, urls = _recording_client({"coins": {}})
    oracle = DefiLlamaOracle(client)

    result = oracle.usd_prices(list(reversed(ids)))

    base = "https://coins.llama.fi/prices/current/"
    assert urls == [
        base + ",".join(expected_keys[:100]),
        base + ",".join(expected_keys[100:150]),
    ]
    assert result == {}  # every key absent from every response body


def test_custom_base_url_is_honoured():
    client, urls = _recording_client({"coins": {}})
    oracle = DefiLlamaOracle(client, base_url="https://mirror.invalid")

    oracle.usd_prices([NATIVE_ETH])

    assert urls == ["https://mirror.invalid/prices/current/coingecko:ethereum"]


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ("this is not json", 200),  # non-JSON 200 body
        ({"unexpected": "shape"}, 200),  # JSON but no 'coins' object
        ("Too Many Requests", 429),  # non-2xx (no retry: straight to error)
        ("Bad Gateway", 502),
    ],
)
def test_malformed_body_or_non_2xx_raises_source_error(payload, status):
    client, _ = _recording_client(payload, status=status)
    oracle = DefiLlamaOracle(client)
    with pytest.raises(SourceError):
        oracle.usd_prices([NATIVE_ETH])


# --- module hygiene: structural conformance, no inquirer, no import I/O -----


def test_module_never_imports_prices_inquirer():
    source = Path(inspect.getsourcefile(defillama)).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    offenders = [name for name in imported if "inquirer" in name]
    assert not offenders, f"defillama.py must not import the inquirer: {offenders}"


def test_import_performs_no_io_and_builds_no_client():
    # The autouse socket guard is active: any connect during import raises.
    module = importlib.reload(defillama)
    resident_clients = [
        name for name, value in vars(module).items()
        if isinstance(value, httpx.Client)
    ]
    assert not resident_clients, (
        f"module-level httpx.Client found (client must be injected): {resident_clients}"
    )


def test_constructor_signature_client_required_base_url_defaulted():
    parameters = list(inspect.signature(DefiLlamaOracle.__init__).parameters.values())
    assert [parameter.name for parameter in parameters] == ["self", "client", "base_url"]
    assert parameters[1].default is inspect.Parameter.empty  # client REQUIRED
    assert parameters[2].default == "https://coins.llama.fi"


def test_usd_prices_matches_the_price_oracle_shape_structurally():
    # Structural conformance to prices.inquirer.PriceOracle: asserted on
    # the signature alone, deliberately WITHOUT importing the inquirer.
    parameters = list(inspect.signature(DefiLlamaOracle.usd_prices).parameters)
    assert parameters == ["self", "caip19s"]
