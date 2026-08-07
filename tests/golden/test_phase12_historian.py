"""Phase 12 gate: prices that can look backwards.

The done-when, quoted verbatim from `docs/internal/RELEASE_0.2.0.md` section 5:

    a past-instant mark for a known asset resolves from a recorded feed; the
    second identical lookup makes zero requests; each oracle's absence hands
    off to the next in the declared order; a manual override wins over a live
    aggregator; an asset no oracle lists comes back declared-unpriced and the
    caller can tell that apart from "worth nothing".

Offline by construction. The only transport in this file is an
`httpx.MockTransport` over `RECORDED_FEED`, a literal in the committed
cassette schema (`{"interactions": [{"request": {...}, "response": {...}}]}`,
DECISIONS "Cassette recording"). Matching is the cassette matcher's own rule,
method plus host plus path plus sorted query, and a request that matches no
recorded interaction raises `UnrecordedRequest` rather than reaching a socket.
Every request is appended to a list first, so "zero requests" is a counted
fact and not a promise.

`RECORDED_FEED` holds exactly eight interactions and
`test_the_whole_journey_touches_only_the_recorded_feed` asserts the journey
consumes all eight and nothing else. `json.dumps(RECORDED_FEED)` is a valid
cassette, so the same eight can be lifted to
`tests/cassettes/phase12_prices.json` without changing a byte of the feed.

Golden values are derived from the algorithms pinned for this phase, never by
asking the code under test:

    bucket_start_ms(at_ms) = (at_ms // 3_600_000) * 3_600_000
    unix_seconds           = at_ms // 1000
    ratio  = Fraction(reserve_quote * 10**dp_base, reserve_base * 10**dp_quote)
    amount = Decimal(ratio.numerator) / Decimal(ratio.denominator)
             * quote_price.amount      # prec=28, ROUND_HALF_EVEN, one rounding

With reserves (10**24 base at 18 dp, 500 * 10**18 quote at 18 dp) the ratio is
exactly 1/2000, so a quote of 3584.17 gives 1.792085 and a quote of 2949.68
gives 1.474840.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from auradefi.money.fiat import Money
from auradefi.prices.inquirer import Inquirer
from auradefi.prices.oracles.defillama import DefiLlamaOracle

# The five modules phase 12 ships. Until they exist the gate reports a plain
# assertion naming what is missing, so collection stays clean and the red is
# readable. An ImportError that is not one of these five is a real contract
# breach and is deliberately allowed to escape.
PHASE_12_MODULES = (
    "auradefi.prices.store",
    "auradefi.prices.historian",
    "auradefi.prices.oracles.manual",
    "auradefi.prices.oracles.coingecko",
    "auradefi.prices.oracles.onchain_amm",
)
_ABSENT: list[str] = []
try:
    from auradefi.prices.historian import Historian
    from auradefi.prices.oracles import coingecko as coingecko_module
    from auradefi.prices.oracles.manual import ManualOracle
    from auradefi.prices.oracles.onchain_amm import AmmPool, OnchainAmmOracle
    from auradefi.prices.store import MemoryPriceStore
except ModuleNotFoundError as exc:  # pragma: no cover - pre-build state only
    if exc.name not in PHASE_12_MODULES:
        raise
    _ABSENT.append(f"{exc.name} ({exc})")


def needs_phase_12() -> None:
    """Fail with a plain assertion while the phase has not shipped."""
    if _ABSENT:
        raise AssertionError(
            "phase 12 is not built: " + "; ".join(_ABSENT) + ". Expected "
            + ", ".join(PHASE_12_MODULES)
        )


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

ETH = "eip155:1/slip44:60"
DAI = "eip155:1/erc20:0x6b175474e89094c44da98b954eedeac495271d0f"
LONGTAIL = "eip155:1/erc20:0x1111111111111111111111111111111111111111"
POOLONLY = "eip155:1/erc20:0x2222222222222222222222222222222222222222"
NOTHING = "eip155:1/erc20:0x3333333333333333333333333333333333333333"
WETH = "eip155:1/erc20:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

POOL_ADDRESS = "0x4444444444444444444444444444444444444444"
POOLONLY_ADDRESS = "0x2222222222222222222222222222222222222222"
WETH_ADDRESS = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
LONGTAIL_ADDRESS = "0x1111111111111111111111111111111111111111"
NOTHING_ADDRESS = "0x3333333333333333333333333333333333333333"
DAI_LLAMA_KEY = "ethereum:0x6b175474e89094c44da98b954eedeac495271d0f"

# 1620000000000 is 2021-05-03T00:00:00Z, exactly on an hour boundary.
PAST = 1_620_000_000_000
MID = 1_620_001_800_000  # 00:30, floors into the PAST bucket
EDGE = 1_620_003_599_999  # 00:59:59.999, still the PAST bucket
NEXT = 1_620_003_600_000  # 01:00, the next bucket

CG_API_KEY = "cg-demo-123"

# The stated absences, asserted verbatim.
CG_NOTE_PAST = (
    "CoinGeckoOracle prices the current instant only; "
    "1620000000000 is not reachable"
)
CG_NOTE_NEXT = (
    "CoinGeckoOracle prices the current instant only; "
    "1620003600000 is not reachable"
)
AMM_NOTE_NEXT = (
    "OnchainAmmOracle is pinned at 1620000000000; 1620003600000 is not reachable"
)
LEGACY_NOTE = (
    "RecordingOracle has no usd_prices_at; it answers the current instant only"
)

# The pinned wire layout. `coingecko:ethereum` sorts before every
# `ethereum:0x...` key, so 'c' before 'e' is the whole ordering rule.
LLAMA = "https://coins.llama.fi"
CURRENT_FOUR_KEY_URL = (
    f"{LLAMA}/prices/current/coingecko:ethereum"
    f",ethereum:{LONGTAIL_ADDRESS}"
    f",ethereum:{POOLONLY_ADDRESS}"
    f",ethereum:{NOTHING_ADDRESS}"
)
CURRENT_WETH_URL = f"{LLAMA}/prices/current/ethereum:{WETH_ADDRESS}"
PAST_TRIPLE_URL = (
    f"{LLAMA}/prices/historical/1620000000/coingecko:ethereum"
    f",ethereum:{POOLONLY_ADDRESS}"
    f",ethereum:{NOTHING_ADDRESS}"
)
PAST_WETH_URL = f"{LLAMA}/prices/historical/1620000000/ethereum:{WETH_ADDRESS}"
PAST_NOTHING_URL = (
    f"{LLAMA}/prices/historical/1620000000/ethereum:{NOTHING_ADDRESS}"
)
NEXT_ETH_URL = f"{LLAMA}/prices/historical/1620003600/coingecko:ethereum"
NEXT_POOLONLY_URL = (
    f"{LLAMA}/prices/historical/1620003600/ethereum:{POOLONLY_ADDRESS}"
)
COINGECKO_THREE_URL = (
    "https://api.coingecko.com/api/v3/simple/token_price/ethereum"
    f"?contract_addresses={LONGTAIL_ADDRESS}"
    f",{POOLONLY_ADDRESS}"
    f",{NOTHING_ADDRESS}"
    "&vs_currencies=usd"
)


def _llama(price: float, key: str, ts: int) -> dict:
    return {
        "coins": {
            key: {"price": price, "symbol": "TKN", "timestamp": ts, "confidence": 0.99}
        }
    }


_JSON = {"content-type": "application/json"}

RECORDED_FEED = {
    "interactions": [
        # 1. current, the four ids manual did not take
        {
            "request": {"method": "GET", "url": CURRENT_FOUR_KEY_URL},
            "response": {
                "status": 200,
                "headers": _JSON,
                "json": _llama(3584.17, "coingecko:ethereum", 1754089200),
            },
        },
        # 2. current, the AMM's quote side
        {
            "request": {"method": "GET", "url": CURRENT_WETH_URL},
            "response": {
                "status": 200,
                "headers": _JSON,
                "json": _llama(3584.17, f"ethereum:{WETH_ADDRESS}", 1754089200),
            },
        },
        # 3. 2021-05-03T00:00:00Z, the three ids manual did not take
        {
            "request": {"method": "GET", "url": PAST_TRIPLE_URL},
            "response": {
                "status": 200,
                "headers": _JSON,
                "json": _llama(2949.68, "coingecko:ethereum", 1620000000),
            },
        },
        # 4. 2021-05-03T00:00:00Z, the AMM's quote side
        {
            "request": {"method": "GET", "url": PAST_WETH_URL},
            "response": {
                "status": 200,
                "headers": _JSON,
                "json": _llama(2949.68, f"ethereum:{WETH_ADDRESS}", 1620000000),
            },
        },
        # 5. the next bucket, a different number for the same asset
        {
            "request": {"method": "GET", "url": NEXT_ETH_URL},
            "response": {
                "status": 200,
                "headers": _JSON,
                "json": _llama(3419.44, "coingecko:ethereum", 1620003600),
            },
        },
        # 6. the next bucket has no mark for the pool-only token
        {
            "request": {"method": "GET", "url": NEXT_POOLONLY_URL},
            "response": {"status": 200, "headers": _JSON, "json": {"coins": {}}},
        },
        # 7. nothing lists NOTHING, at any instant
        {
            "request": {"method": "GET", "url": PAST_NOTHING_URL},
            "response": {"status": 200, "headers": _JSON, "json": {"coins": {}}},
        },
        # 8. CoinGecko carries the long-tail token DefiLlama does not
        {
            "request": {"method": "GET", "url": COINGECKO_THREE_URL},
            "response": {
                "status": 200,
                "headers": _JSON,
                "json": {LONGTAIL_ADDRESS: {"usd": 0.42}},
            },
        },
    ]
}
RECORDED_URLS = [i["request"]["url"] for i in RECORDED_FEED["interactions"]]

POOL_READS = {
    (POOL_ADDRESS, "token0", ()): POOLONLY_ADDRESS,
    (POOL_ADDRESS, "token1", ()): WETH_ADDRESS,
    (POOL_ADDRESS, "getReserves", ()): (10**24, 500 * 10**18, 1620000000),
    (POOLONLY_ADDRESS, "decimals", ()): 18,
    (WETH_ADDRESS, "decimals", ()): 18,
}


# --------------------------------------------------------------------------
# The recorded feed, counted
# --------------------------------------------------------------------------


class UnrecordedRequest(Exception):
    """A request the feed never recorded. The offline guarantee, failing loud."""


class UnrecordedRead(Exception):
    """A pool read the fixture never recorded."""


def _match_key(method: str, url: httpx.URL) -> tuple:
    return (
        method.upper(),
        url.host,
        url.path,
        tuple(sorted(url.params.multi_items())),
    )


class Feed:
    """The recorded feed behind an httpx.MockTransport, counting every call."""

    def __init__(self, interactions):
        self._recorded = {}
        for entry in interactions:
            request = entry["request"]
            key = _match_key(request["method"], httpx.URL(request["url"]))
            self._recorded[key] = entry["response"]
        self.urls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        recorded = self._recorded.get(_match_key(request.method, request.url))
        if recorded is None:
            raise UnrecordedRequest(
                f"{request.method} {request.url} is not in the recorded feed; "
                f"recorded: {RECORDED_URLS}"
            )
        headers = dict(recorded.get("headers", {}))
        if "json" in recorded:
            return httpx.Response(
                recorded["status"], json=recorded["json"], headers=headers
            )
        return httpx.Response(
            recorded["status"], text=recorded["text"], headers=headers
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class FixtureReader:
    """Dict-backed pool reader that deliberately does NOT normalise.

    A reader that lowercased its address argument would hide an oracle that
    forgot to, so an unrecorded key raises instead.
    """

    def __init__(self, reads):
        self._reads = dict(reads)
        self.calls: list[tuple] = []

    def call(self, address: str, fn: str, args: tuple = ()) -> object:
        key = (address, fn, tuple(args))
        self.calls.append(key)
        if key not in self._reads:
            raise UnrecordedRead(f"{key!r} is not a recorded read")
        return self._reads[key]


class RecordingOracle:
    """A 0.1.x oracle. It carries `usd_prices` and nothing else."""

    def __init__(self, prices):
        self._prices = dict(prices)
        self.calls: list[list[str]] = []

    def usd_prices(self, caip19s):
        self.calls.append(list(caip19s))
        return {c: self._prices[c] for c in caip19s if c in self._prices}


def build(api_key: str | None = CG_API_KEY) -> SimpleNamespace:
    """The chain a host would build, in the declared precedence order."""
    feed = Feed(RECORDED_FEED["interactions"])
    client = feed.client()
    reader = FixtureReader(POOL_READS)
    manual = ManualOracle(
        marks={DAI: Money(Decimal("1.0500"), "USD")},
        dated_marks={(DAI, PAST): Money(Decimal("1.0500"), "USD")},
    )
    defillama = DefiLlamaOracle(client)
    coingecko = coingecko_module.configured(client, api_key)
    amm = OnchainAmmOracle(
        reader,
        [AmmPool("eip155:1", POOL_ADDRESS)],
        quotes=defillama,
        pinned_at_ms=PAST,
    )
    oracles = [o for o in (manual, defillama, coingecko, amm) if o is not None]
    inquirer = Inquirer(oracles)
    return SimpleNamespace(
        feed=feed,
        urls=feed.urls,
        reader=reader,
        manual=manual,
        defillama=defillama,
        coingecko=coingecko,
        amm=amm,
        oracles=oracles,
        inquirer=inquirer,
        historian=Historian(inquirer, MemoryPriceStore()),
    )


# --------------------------------------------------------------------------
# The clauses
# --------------------------------------------------------------------------


def test_a_past_instant_mark_is_fetched_from_the_historical_endpoint():
    # pins: a past instant is asked of `/prices/historical/{unix_seconds}/`
    #       at the floored bucket, so a current-price response can never be
    #       served as the answer to a 2021 question.
    needs_phase_12()
    rig = build()

    rig.historian.marks([ETH, DAI, POOLONLY, NOTHING], PAST)

    assert PAST_TRIPLE_URL in rig.urls
    assert [u for u in rig.urls if "/prices/current/" in u] == []
    assert all("/prices/historical/1620000000/" in u for u in rig.urls)


def test_the_past_instant_values_are_the_recorded_past_numbers():
    # pins: every mark in the report is the number the 2021 feed recorded, and
    #       the pool-only token is that number through the exact 1/2000 ratio,
    #       never the current-feed number.
    needs_phase_12()
    rig = build()

    report = rig.historian.marks([ETH, DAI, POOLONLY, NOTHING], PAST)

    assert report.prices[ETH] == Money(Decimal("2949.68"), "USD")
    assert report.prices[ETH] != Money(Decimal("3584.17"), "USD")
    assert report.prices[DAI] == Money(Decimal("1.0500"), "USD")
    assert report.prices[POOLONLY] == Money(Decimal("1.474840"), "USD")
    assert report.unpriced == (NOTHING,)
    assert report.at_ms == PAST
    assert report.bucket_ms == 1_620_000_000_000
    assert report.resolution_ms == 3_600_000


def test_the_second_identical_lookup_makes_zero_requests():
    # pins: a mark already in the store at the same bucket is answered without
    #       touching the transport, without reading the pool again, and
    #       without asking the chain for its absences.
    needs_phase_12()
    rig = build()

    first = rig.historian.marks([ETH, DAI, POOLONLY], PAST)
    spent = len(rig.urls)
    reads = len(rig.reader.calls)
    assert spent > 0

    second = rig.historian.marks([ETH, DAI, POOLONLY], MID)

    assert len(rig.urls) == spent
    assert len(rig.reader.calls) == reads
    assert dict(second.prices) == dict(first.prices)
    assert second.prices[ETH] == Money(Decimal("2949.68"), "USD")
    assert second.at_ms == MID
    assert second.bucket_ms == 1_620_000_000_000
    assert second.notes == ()


def test_the_resolution_boundary_is_where_a_new_request_starts():
    # pins: 00:59:59.999 and 01:00:00.000 are provably different cache
    #       entries, and the later one is fetched at its own unix second.
    needs_phase_12()
    rig = build()

    rig.historian.marks([ETH], PAST)
    spent = len(rig.urls)

    edge = rig.historian.marks([ETH], EDGE)
    assert len(rig.urls) == spent
    assert edge.bucket_ms == 1_620_000_000_000
    assert edge.prices[ETH] == Money(Decimal("2949.68"), "USD")

    later = rig.historian.marks([ETH], NEXT)
    assert NEXT_ETH_URL in rig.urls
    assert later.bucket_ms == 1_620_003_600_000
    assert later.prices[ETH] == Money(Decimal("3419.44"), "USD")


def test_a_manual_override_wins_over_a_live_aggregator():
    # pins: the highest-precedence oracle's mark is the answer, and the
    #       aggregator below it is never even asked for that asset.
    needs_phase_12()
    rig = build()

    report = rig.historian.marks([ETH, DAI], PAST)
    assert report.prices[DAI] == Money(Decimal("1.0500"), "USD")
    assert report.prices[ETH] == Money(Decimal("2949.68"), "USD")
    assert [u for u in rig.urls if DAI_LLAMA_KEY in u] == []

    spent = len(rig.urls)
    assert rig.inquirer.usd_prices([DAI]) == {DAI: Money(Decimal("1.0500"), "USD")}
    assert len(rig.urls) == spent


def test_each_oracle_absence_hands_off_to_the_next_in_the_declared_order():
    # pins: manual, then defillama, then coingecko, then onchain_amm, each
    #       asked only for the ids still unpriced, and each contributing the
    #       one asset the ones above it do not list.
    needs_phase_12()
    rig = build()

    got = rig.inquirer.usd_prices([ETH, DAI, LONGTAIL, POOLONLY, NOTHING])

    assert got == {
        DAI: Money(Decimal("1.0500"), "USD"),
        ETH: Money(Decimal("3584.17"), "USD"),
        LONGTAIL: Money(Decimal("0.42"), "USD"),
        POOLONLY: Money(Decimal("1.792085"), "USD"),
    }
    assert NOTHING not in got
    assert CURRENT_FOUR_KEY_URL in rig.urls
    assert COINGECKO_THREE_URL in rig.urls
    assert CURRENT_WETH_URL in rig.urls


def test_a_stated_absence_is_told_apart_from_an_asset_nothing_lists():
    # pins: an oracle that cannot reach the instant names itself and the
    #       instant in `notes`, while an asset no oracle lists is unpriced
    #       with no note about it at all.
    needs_phase_12()
    rig = build()

    unreachable = rig.historian.marks([POOLONLY], NEXT)
    assert unreachable.unpriced == (POOLONLY,)
    assert unreachable.notes == (CG_NOTE_NEXT, AMM_NOTE_NEXT)

    not_listed = rig.historian.marks([NOTHING], PAST)
    assert not_listed.unpriced == (NOTHING,)
    assert not_listed.notes == (CG_NOTE_PAST,)
    assert [n for n in not_listed.notes if NOTHING_ADDRESS in n] == []


def test_declared_unpriced_is_not_worth_nothing():
    # pins: an id no oracle lists answers `None`, is absent from `prices`, is
    #       listed in `unpriced`, and is never a zero Money.
    needs_phase_12()
    rig = build()

    assert rig.historian.mark(NOTHING, PAST) is None
    assert rig.historian.mark(NOTHING, PAST) != Money(Decimal("0"), "USD")
    assert rig.historian.mark(ETH, PAST) == Money(Decimal("2949.68"), "USD")

    report = rig.historian.marks([DAI, ETH, NOTHING], PAST)
    assert NOTHING not in report.prices
    assert report.unpriced == (NOTHING,)
    assert Money(Decimal("0"), "USD") not in list(report.prices.values())


def test_the_unpriced_negative_is_never_cached():
    # pins: a miss is not written to the store, so the very next identical
    #       lookup asks the feed again rather than replaying "unpriceable".
    needs_phase_12()
    rig = build()

    rig.historian.marks([NOTHING], PAST)
    assert rig.urls.count(PAST_NOTHING_URL) == 1

    again = rig.historian.marks([NOTHING], PAST)
    assert rig.urls.count(PAST_NOTHING_URL) == 2
    assert again.unpriced == (NOTHING,)


def test_a_keyless_coingecko_is_not_in_the_chain_and_says_so():
    # pins: without a key the oracle is not constructed and not in the chain,
    #       the long-tail token only it carried is left unpriced, no request
    #       reaches coingecko at all, and the absence is a stated sentence
    #       naming the environment variable that fixes it.
    needs_phase_12()
    rig = build(api_key=None)

    assert rig.coingecko is None
    assert len(rig.oracles) == 3

    got = rig.inquirer.usd_prices([ETH, DAI, LONGTAIL, POOLONLY, NOTHING])

    assert LONGTAIL not in got
    assert got[ETH] == Money(Decimal("3584.17"), "USD")
    assert got[DAI] == Money(Decimal("1.0500"), "USD")
    assert got[POOLONLY] == Money(Decimal("1.792085"), "USD")
    assert [u for u in rig.urls if "api.coingecko.com" in u] == []
    assert "AURADEFI_COINGECKO_API_KEY" in coingecko_module.ABSENCE_REASON


def test_the_coingecko_key_never_reaches_a_recorded_url():
    # pins: the key travels in a request header, so no URL a cassette would
    #       record can carry it.
    needs_phase_12()
    rig = build()

    rig.inquirer.usd_prices([ETH, LONGTAIL, POOLONLY])
    rig.historian.marks([ETH, POOLONLY, NOTHING], PAST)

    assert rig.urls
    for url in rig.urls:
        assert CG_API_KEY not in url
        assert "apikey" not in url
        assert "api_key" not in url


def test_a_legacy_oracle_is_named_rather_than_silently_skipped():
    # pins: an oracle carrying only `usd_prices` is never called at an instant
    #       and its inability is reported by name, first, in construction
    #       order, without changing a single mark.
    needs_phase_12()
    rig = build()
    legacy = RecordingOracle({ETH: Money(Decimal("999.99"), "USD")})
    historian = Historian(
        Inquirer([legacy, *rig.oracles]), MemoryPriceStore()
    )

    report = historian.marks([ETH, DAI, POOLONLY, NOTHING], PAST)

    assert legacy.calls == []
    assert report.notes == (LEGACY_NOTE, CG_NOTE_PAST)
    assert report.prices[ETH] == Money(Decimal("2949.68"), "USD")
    assert report.prices[DAI] == Money(Decimal("1.0500"), "USD")
    assert report.prices[POOLONLY] == Money(Decimal("1.474840"), "USD")
    assert report.unpriced == (NOTHING,)


def test_an_empty_ask_declares_nothing_and_spends_nothing():
    # pins: an empty id list is an empty report, not an error and not a
    #       fabricated mark, and it costs zero requests.
    needs_phase_12()
    rig = build()

    report = rig.historian.marks([], PAST)

    assert dict(report.prices) == {}
    assert report.unpriced == ()
    assert report.bucket_ms == 1_620_000_000_000
    assert rig.urls == []
    assert rig.reader.calls == []


def test_the_whole_journey_touches_only_the_recorded_feed():
    # pins: the entire phase-12 surface, past and current, resolves against
    #       exactly the eight recorded interactions and nothing else; an
    #       unrecorded request raises rather than escaping to a socket.
    needs_phase_12()
    rig = build()

    rig.historian.marks([ETH, DAI, POOLONLY, NOTHING], PAST)
    rig.historian.marks([ETH], NEXT)
    rig.historian.marks([POOLONLY], NEXT)
    rig.historian.marks([NOTHING], PAST)
    rig.inquirer.usd_prices([ETH, DAI, LONGTAIL, POOLONLY, NOTHING])

    assert sorted(set(rig.urls)) == sorted(RECORDED_URLS)

    with pytest.raises(UnrecordedRequest):
        rig.feed.client().get(f"{LLAMA}/prices/historical/1/coingecko:ethereum")
