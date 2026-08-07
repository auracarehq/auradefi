"""DefiLlama oracle (SPEC §3.2, §6.3): pinned coin_key mapping, the
deterministic sorted-chunked request layout for both the current and the
historical endpoint, Decimal(str(price)) conversion, SourceError on failure:
all offline (cassette + inline MockTransport)."""

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
from auradefi.prices.oracles.defillama import (
    CHUNK_SIZE,
    DefiLlamaOracle,
    chunk_keys,
    coin_key,
)

# The historical seam lives in prices/inquirer.py, which phase 12 extends and
# this order does not own. Imported softly so a missing protocol reports a
# plain assertion in the one test that needs it, never a collection error.
try:
    from auradefi.prices.inquirer import HistoricalPriceOracle
except ImportError:  # pragma: no cover - pre-build state only
    HistoricalPriceOracle = None

DAI = "eip155:1/erc20:0x6B175474E89094C44Da98b954EedeAC495271d0F"
DAI_KEY = "ethereum:0x6b175474e89094c44da98b954eedeac495271d0f"
NATIVE_ETH = "eip155:1/slip44:60"
BITCOIN = "bip122:000000000019d6689c085ae165831e93/slip44:0"
ETH_PRICE = Decimal("3584.17")

# 1620000000000 is 2021-05-03T00:00:00Z. unix_seconds = at_ms // 1000, so the
# three instants below floor to 1620000000, 1620000000 and 1620003600.
PAST = 1_620_000_000_000
PAST_WITH_REMAINDER = 1_620_000_000_999
NEXT_HOUR = 1_620_003_600_000

LLAMA = "https://coins.llama.fi"
PAST_ETH_URL = f"{LLAMA}/prices/historical/1620000000/coingecko:ethereum"

# The body every non-2xx fixture carries, and the reason it is not the
# "Internal Server Error" text a real gateway would send. The status check in
# `_coins` sits ABOVE the JSON ladder, so a text/plain error body is refused
# twice: delete the status check and `response.json()` raises on the next
# line, SourceError still comes out, and the deleted branch leaves no red test
# behind. This body parses, carries a `coins` mapping and prices the key that
# was asked for, so nothing below the status check can refuse it and the
# status code becomes the only thing on trial.
PRICED_BODY = {"coins": {"coingecko:ethereum": {"price": 2949.68}}}


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
        # Non-2xx with a body that is beyond reproach, so the status is the
        # only ground for refusal: see PRICED_BODY. No retry, straight to
        # error. Both rows used to send error text and neither reached the
        # status check, which the phase-12 mutation gate caught.
        (PRICED_BODY, 429),
        (PRICED_BODY, 502),
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


# --- usd_prices_at: the historical endpoint ---------------------------------
#
# The whole point of this surface is that a 2021 question cannot be answered
# by a 2026 number. Every assertion below reads the URL that went on the wire,
# because the only thing separating a correct past mark from a plausible wrong
# one is which endpoint was asked.


def _quote(key: str, price: float, timestamp: int = 1620000000) -> dict:
    """One DefiLlama `coins` body, in the shape the live feed returns."""
    return {
        "coins": {
            key: {
                "price": price,
                "symbol": "TKN",
                "timestamp": timestamp,
                "confidence": 0.99,
            }
        }
    }


def _leaf(node: ast.AST) -> str:
    """Last dotted component of a call target: `decimal.Decimal` -> `Decimal`."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


# pins: a past instant is asked of /prices/historical/{unix_seconds}/, one GET
#       for one key, so a current-price endpoint can never answer it
def test_a_past_instant_issues_one_get_at_the_historical_url():
    client, urls = _recording_client(_quote("coingecko:ethereum", 2949.68))
    oracle = DefiLlamaOracle(client)

    oracle.usd_prices_at([NATIVE_ETH], PAST)

    assert urls == [
        "https://coins.llama.fi/prices/historical/1620000000/coingecko:ethereum"
    ]


# pins: unix_seconds is at_ms // 1000, floored, so a sub-second remainder is
#       dropped and does not round the instant up to the next second
def test_the_sub_second_remainder_is_floored_away_not_rounded():
    client, urls = _recording_client(_quote("coingecko:ethereum", 2949.68))
    oracle = DefiLlamaOracle(client)

    oracle.usd_prices_at([NATIVE_ETH], PAST)
    oracle.usd_prices_at([NATIVE_ETH], PAST_WITH_REMAINDER)

    # Rounding 1620000000999 gives 1620000001; flooring gives 1620000000, and
    # the two instants therefore build one identical URL.
    assert urls == [PAST_ETH_URL, PAST_ETH_URL]


# pins: a whole extra second is a DIFFERENT instant on the wire, so the floor
#       above is a floor and not a truncation to some coarser unit
def test_the_next_whole_second_is_a_different_url():
    client, urls = _recording_client(_quote("coingecko:ethereum", 2949.68))
    oracle = DefiLlamaOracle(client)

    oracle.usd_prices_at([NATIVE_ETH], 1_620_000_001_000)

    assert urls == [
        "https://coins.llama.fi/prices/historical/1620000001/coingecko:ethereum"
    ]


# pins: the historical path reuses the deduplicated lexicographic key sort, so
#       'coingecko:ethereum' precedes 'ethereum:0x…' in the joined coins list
def test_the_historical_key_list_is_deduplicated_and_lexicographically_sorted():
    client, urls = _recording_client({"coins": {}})
    oracle = DefiLlamaOracle(client)

    oracle.usd_prices_at([DAI, NATIVE_ETH, DAI], PAST)

    assert urls == [
        "https://coins.llama.fi/prices/historical/1620000000/coingecko:ethereum"
        ",ethereum:0x6b175474e89094c44da98b954eedeac495271d0f"
    ]


# pins: CHUNK_SIZE still splits on the historical path, one key over the
#       boundary makes a second GET, and both carry the same unix second
def test_101_keys_split_into_two_historical_gets_in_global_sorted_order():
    ids = [f"eip155:1/erc20:0x{index:040x}" for index in range(101)]
    keys = sorted(f"ethereum:0x{index:040x}" for index in range(101))
    client, urls = _recording_client({"coins": {}})
    oracle = DefiLlamaOracle(client)

    oracle.usd_prices_at(list(reversed(ids)), PAST)

    base = "https://coins.llama.fi/prices/historical/1620000000/"
    assert CHUNK_SIZE == 100
    assert urls == [
        base + ",".join(keys[:100]),
        base + ",".join(keys[100:101]),
    ]


# pins: an empty ask spends nothing; there is no /prices/historical/{t}/ with
#       an empty coins list
def test_an_empty_id_list_is_empty_with_zero_http():
    client, urls = _recording_client({"coins": {}})
    oracle = DefiLlamaOracle(client)

    assert oracle.usd_prices_at([], PAST) == {}
    assert urls == []


# pins: an id this oracle cannot map contributes no request key, so an ask
#       made entirely of unmapped ids costs no request at all
def test_an_unmapped_id_alone_is_empty_with_zero_http():
    client, urls = _recording_client({"coins": {}})
    oracle = DefiLlamaOracle(client)

    assert oracle.usd_prices_at([BITCOIN], PAST) == {}
    assert urls == []


# pins: the historical quote is Money(Decimal(str(price)), 'USD'), the same
#       conversion the current path uses, never Decimal from the raw float
def test_the_historical_quote_is_decimal_str_wrapped_in_usd_money():
    # The pin's point: Decimal from the raw float is NOT the quoted price.
    assert Decimal(2949.68) != Decimal("2949.68")

    client, _ = _recording_client(
        {"coins": {"coingecko:ethereum": {"price": 2949.68}}}
    )
    oracle = DefiLlamaOracle(client)

    result = oracle.usd_prices_at([NATIVE_ETH], PAST)

    assert result == {NATIVE_ETH: Money(Decimal("2949.68"), "USD")}
    assert result[NATIVE_ETH].amount.as_tuple() == Decimal("2949.68").as_tuple()


# pins: every Decimal built in this module is built from a string, so no
#       float-typed expression is ever handed to the constructor
def test_no_decimal_in_the_module_is_built_from_a_float_typed_expression():
    source = Path(inspect.getsourcefile(defillama)).read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and _leaf(node.func) == "Decimal"
    ]
    assert calls, "no Decimal( call found in defillama.py: the check went blind"

    offenders = []
    for call in calls:
        first = call.args[0] if call.args else None
        stringified = isinstance(first, ast.Call) and _leaf(first.func) == "str"
        literal = isinstance(first, ast.Constant) and isinstance(first.value, str)
        if not (stringified or literal):
            offenders.append(f"line {call.lineno}: {ast.unparse(call)}")
    assert not offenders, (
        "Decimal( must be handed a string: Decimal(2949.68) is "
        f"{Decimal(2949.68)}, which is not the quoted price:\n  "
        + "\n  ".join(offenders)
    )


# pins: a key the feed has no mark for at that instant is unpriced and absent,
#       never a zero Money, and its absence is not an error
def test_a_key_the_feed_does_not_carry_is_absent_and_never_zero():
    client, urls = _recording_client(_quote("coingecko:ethereum", 2949.68))
    oracle = DefiLlamaOracle(client)

    result = oracle.usd_prices_at([NATIVE_ETH, DAI], PAST)

    assert result == {NATIVE_ETH: Money(Decimal("2949.68"), "USD")}
    assert DAI not in result
    assert Money(Decimal("0"), "USD") not in list(result.values())
    assert urls == [
        "https://coins.llama.fi/prices/historical/1620000000/coingecko:ethereum"
        ",ethereum:0x6b175474e89094c44da98b954eedeac495271d0f"
    ]


# pins: a non-2xx historical response raises SourceError naming the historical
#       URL, so the message says which instant was asked for
def test_http_500_on_the_historical_path_names_the_historical_url():
    # PRICED_BODY, not "Internal Server Error": the status is what is on trial
    # and a text body would be refused by the JSON ladder instead, which names
    # the same URL and so satisfies the assertion below over a status check
    # that has been deleted.
    client, _ = _recording_client(PRICED_BODY, status=500)
    oracle = DefiLlamaOracle(client)

    with pytest.raises(SourceError) as excinfo:
        oracle.usd_prices_at([NATIVE_ETH], PAST)

    message = str(excinfo.value)
    assert PAST_ETH_URL in message
    # The status door's own words. Naming the URL alone is satisfied by every
    # other rung of the ladder, all of which quote the URL too.
    assert "500" in message
    assert "non-JSON" not in message


# pins: the historical path runs the same malformed-body ladder as the current
#       one, so a non-JSON body and a body with no 'coins' mapping both raise
@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ("this is not json", 200),  # text/plain 200 body
        ({"unexpected": "shape"}, 200),  # JSON but no 'coins' object
        # 429 with a body the ladder below the status check would accept, so
        # this row dies with the status check and the two rows above it do
        # not: one rung of the ladder each. See PRICED_BODY.
        (PRICED_BODY, 429),
    ],
)
def test_a_malformed_historical_body_or_non_2xx_raises_source_error(payload, status):
    client, _ = _recording_client(payload, status=status)
    oracle = DefiLlamaOracle(client)

    with pytest.raises(SourceError):
        oracle.usd_prices_at([NATIVE_ETH], PAST)


# pins: at_ms is refused at the entry door with SourceError, before any HTTP,
#       for a string, for a bool and for a negative instant
@pytest.mark.parametrize(
    "at_ms",
    [
        "0",  # a string that looks like an instant
        True,  # bool is an int subclass; require_int refuses it
        -1,  # before the epoch
        1.0,  # a float instant is not a millisecond-epoch integer
        None,
    ],
)
def test_a_bad_at_ms_raises_source_error_before_any_request(at_ms):
    client, urls = _recording_client(_quote("coingecko:ethereum", 2949.68))
    oracle = DefiLlamaOracle(client)

    with pytest.raises(SourceError):
        oracle.usd_prices_at([NATIVE_ETH], at_ms)
    assert urls == []


# pins: zero is a legal instant, so the refusal above is a negative check and
#       not a truthiness check that would also swallow the epoch
def test_the_epoch_itself_is_a_legal_instant():
    client, urls = _recording_client({"coins": {}})
    oracle = DefiLlamaOracle(client)

    assert oracle.usd_prices_at([NATIVE_ETH], 0) == {}
    assert urls == [
        "https://coins.llama.fi/prices/historical/0/coingecko:ethereum"
    ]


# pins: caip19s is refused at the same door usd_prices uses, so a bare string
#       is not iterated per character and a non-str element is not a builtin
@pytest.mark.parametrize(
    "caip19s",
    [
        NATIVE_ETH,  # a bare string is not a sequence of ids
        None,
        [None],
        [123],
        (index for index in ()),  # a generator cannot be measured twice
    ],
)
def test_a_bad_caip19s_raises_source_error_before_any_request(caip19s):
    client, urls = _recording_client(_quote("coingecko:ethereum", 2949.68))
    oracle = DefiLlamaOracle(client)

    with pytest.raises(SourceError):
        oracle.usd_prices_at(caip19s, PAST)
    assert urls == []


# pins: a scheme-less base_url reaches the caller as SourceError from the
#       historical path too, never as urllib's bare ValueError
def test_a_scheme_less_base_url_raises_source_error_on_the_historical_path():
    # httpx does not refuse 'localhost:8545' itself. It sends, and urllib's
    # cookie extraction then raises ValueError("unknown url type: …"), which
    # descends from Exception and NOT from httpx.HTTPError.
    client, _ = _recording_client({"coins": {}})
    oracle = DefiLlamaOracle(client, base_url="localhost:8545")

    with pytest.raises(SourceError) as excinfo:
        oracle.usd_prices_at([NATIVE_ETH], PAST)

    cause = excinfo.value.__cause__
    assert isinstance(cause, ValueError)
    assert not isinstance(cause, httpx.HTTPError)


# pins: the oracle satisfies the historical seam structurally, so a host can
#       hand it to the historian without this module importing the inquirer
def test_the_oracle_is_a_historical_price_oracle_structurally():
    assert HistoricalPriceOracle is not None, (
        "auradefi.prices.inquirer.HistoricalPriceOracle is not defined: "
        "phase 12 extends the oracle seam already in inquirer.py rather "
        "than inventing a second one"
    )
    client, _ = _recording_client({"coins": {}})

    assert isinstance(DefiLlamaOracle(client), HistoricalPriceOracle) is True


# pins: the historical method's signature is (caip19s, at_ms), asserted on the
#       signature alone so the conformance above cannot be bought by an import
def test_usd_prices_at_matches_the_historical_oracle_shape_structurally():
    parameters = list(inspect.signature(DefiLlamaOracle.usd_prices_at).parameters)
    assert parameters == ["self", "caip19s", "at_ms"]


# pins: replayed against the committed feed, the 01:00 instant returns the
#       01:00 number, which is not the 00:00 number, so a wrong instant cannot
#       pass by coincidence
def test_cassette_replay_returns_the_number_recorded_at_that_instant(cassette):
    # phase12_prices.json records /prices/historical/1620003600/ at 3419.44 and
    # /prices/historical/1620000000/… at 2949.68. Any other URL, from a wrong
    # floor or the current endpoint, raises CassetteMissError.
    oracle = DefiLlamaOracle(cassette("phase12_prices").client())

    result = oracle.usd_prices_at([NATIVE_ETH], NEXT_HOUR)

    assert result == {NATIVE_ETH: Money(Decimal("3419.44"), "USD")}
    assert result[NATIVE_ETH] != Money(Decimal("2949.68"), "USD")
    assert result[NATIVE_ETH] != Money(ETH_PRICE, "USD")


# pins: the current endpoint is untouched by the historical addition, so
#       /prices/current/ is still what usd_prices asks for
def test_the_current_endpoint_layout_is_unchanged_by_the_historical_one():
    client, urls = _recording_client({"coins": {}})
    oracle = DefiLlamaOracle(client)

    oracle.usd_prices([DAI, NATIVE_ETH])

    assert urls == [
        f"{LLAMA}/prices/current/coingecko:ethereum,{DAI_KEY}"
    ]
