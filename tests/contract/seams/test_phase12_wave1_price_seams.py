"""Phase 12 wave-1 seam audit: the store, the instant seam, and the feed.

Three orders built three files that never import one another:

* ``prices/store.py`` declares ``RESOLUTION_MS`` and ``bucket_start_ms`` and
  the ``PriceStore`` port,
* ``prices/inquirer.py`` declares ``HistoricalPriceOracle`` and walks a chain
  at an instant,
* ``prices/oracles/defillama.py`` puts an instant on the wire.

Each is green on its own. This file looks at what sits between them, and
between them and what phase 1 already shipped.

WHAT IS BOUND HERE, AND WHY IT IS NOT AN IN-REPO CLASS. Every fake below is
written from the DECLARED interface only: one method for a
``HistoricalPriceOracle``, two for a ``PriceStore``, no extras, no borrowing
from ``Inquirer`` or ``MemoryPriceStore``. The in-repo suite cannot find a
seam that only works because the in-repo implementation carries something the
port never promised, because the in-repo suite uses the in-repo class.

THE FLOOR IS DERIVED TWICE ON PURPOSE. ``_declared_bucket`` below is
``at_ms - at_ms % 3_600_000``, written from the rule in RELEASE_0.2.0 section
5 and DECISIONS, never imported. ``bucket_start_ms`` is
``(at_ms // RESOLUTION_MS) * RESOLUTION_MS``. Two different expressions for
one logical value, compared by running both, which is the only comparison
that catches a resolution that moved on one side.

EVERY FIXTURE CAN EXPRESS ITS OWN NEGATION, and each test names the input
that flips it:

* ``HostHistoricalOracle`` prices an id with ``Money(Decimal(at_ms), 'USD')``,
  so the answer carries the instant it was asked at. An oracle handed the
  wrong instant returns a different number rather than the same one, and an
  oracle that ignored ``at_ms`` could not vary at all.
* ``HostPriceStore`` floors its key. ``UnbucketedStore`` is the same store
  with the floor removed, and ``test_an_unbucketed_store_fails_the_same_two
  _asks`` drives it to prove the bucket assertions are load bearing.
* the recorded feed answers a DIFFERENT number at each instant (2949.68 at
  1620000000, 3419.44 at 1620003600), so a URL built at the wrong second
  cannot pass by coincidence.

TWO TESTS IN THIS FILE ARE RED, and they are the findings. They are marked
``SEAM FINDING`` in their bodies. Do not weaken them to green.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from auradefi.errors import (
    AuradefiError,
    CurrencyMismatchError,
    ValidationError,
)
from auradefi.money.fiat import Money
from auradefi.prices.inquirer import (
    HistoricalPriceOracle,
    Inquirer,
    PriceOracle,
)
from auradefi.prices.oracles.defillama import DefiLlamaOracle, coin_key
from auradefi.prices.store import (
    RESOLUTION_MS,
    MemoryPriceStore,
    PriceStore,
    bucket_start_ms,
)
from auradefi.testing import cassettes

REPO = Path(__file__).resolve().parents[3]
CASSETTE = REPO / "tests" / "cassettes" / "phase12_prices.json"

ETH = "eip155:1/slip44:60"
DAI = "eip155:1/erc20:0x6b175474e89094c44da98b954eedeac495271d0f"
DAI_CHECKSUMMED = "eip155:1/erc20:0x6B175474E89094C44Da98b954EedeAC495271d0F"
POOLONLY = "eip155:1/erc20:0x2222222222222222222222222222222222222222"
NOTHING = "eip155:1/erc20:0x3333333333333333333333333333333333333333"

#: The four instants the phase pins, in the golden's own words.
PAST = 1_620_000_000_000  # 2021-05-03T00:00:00Z, on an hour boundary
MID = 1_620_001_800_000  # 00:30, floors into the PAST bucket
EDGE = 1_620_003_599_999  # 00:59:59.999, still the PAST bucket
NEXT = 1_620_003_600_000  # 01:00, the next bucket

LLAMA = "https://coins.llama.fi"

#: The URL the blind golden pinned for the phase's three-key past ask, quoted
#: from tests/golden/test_phase12_historian.py's PAST_TRIPLE_URL. Held here as
#: a literal rather than imported, so the two artifacts are two sources.
GOLDEN_PAST_TRIPLE_URL = (
    f"{LLAMA}/prices/historical/1620000000/coingecko:ethereum"
    f",ethereum:0x2222222222222222222222222222222222222222"
    f",ethereum:0x3333333333333333333333333333333333333333"
)

#: The generated sentence a legacy oracle contributes, quoted verbatim from
#: the golden's LEGACY_NOTE. `historian.PriceMarks.notes` carries it to a
#: caller unedited, so this string is a wire format and not a message.
GOLDEN_LEGACY_NOTE = (
    "RecordingOracle has no usd_prices_at; it answers the current instant only"
)

#: The declared resolution, restated from RELEASE_0.2.0 section 5 rather than
#: imported, so a change to RESOLUTION_MS fails here instead of moving with it.
DECLARED_RESOLUTION_MS = 3_600_000


def _declared_bucket(at_ms: int) -> int:
    """The declared floor, by subtraction rather than by floor division.

    ``at_ms - at_ms % R`` and ``(at_ms // R) * R`` are the same value for
    every non-negative ``at_ms`` and different expressions, so comparing them
    is a real comparison rather than a restatement.
    """
    return at_ms - at_ms % DECLARED_RESOLUTION_MS


# --------------------------------------------------------------------------
# Implementations written from the declared interfaces and nothing else
# --------------------------------------------------------------------------


class HostHistoricalOracle:
    """A host's ``HistoricalPriceOracle``: one method, exactly as declared.

    No ``usd_prices``, no ``unreachable_instant``, no ``absences_at``. The
    price it returns is ``Money(Decimal(at_ms), 'USD')``, which makes the
    instant it was asked at readable off the answer.
    """

    def __init__(self, known: frozenset[str]) -> None:
        self._known = known
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def usd_prices_at(self, caip19s, at_ms):
        self.calls.append((tuple(caip19s), at_ms))
        return {
            caip19: Money(Decimal(at_ms), "USD")
            for caip19 in caip19s
            if caip19 in self._known
        }


class HostLegacyOracle:
    """A host's 0.1.x ``PriceOracle``: ``usd_prices`` and nothing else."""

    def __init__(self, prices: dict[str, Money]) -> None:
        self._prices = dict(prices)
        self.calls: list[list[str]] = []

    def usd_prices(self, caip19s):
        self.calls.append(list(caip19s))
        return {c: self._prices[c] for c in caip19s if c in self._prices}


class HostPinnedOracle:
    """A host oracle that states one reachable instant and refuses the rest.

    It carries both optional members, so it exercises the rule that a stated
    reason skips the oracle WITHOUT the query being called.
    """

    def __init__(self, pinned_at_ms: int) -> None:
        self._pinned = pinned_at_ms
        self.calls: list[tuple[tuple[str, ...], int]] = []
        self.reason_calls: list[int] = []

    def unreachable_instant(self, at_ms):
        self.reason_calls.append(at_ms)
        if at_ms == self._pinned:
            return None
        return f"HostPinnedOracle is pinned at {self._pinned}; {at_ms} is not reachable"

    def usd_prices_at(self, caip19s, at_ms):
        self.calls.append((tuple(caip19s), at_ms))
        return {caip19: Money(Decimal("7"), "USD") for caip19 in caip19s}


class HostPriceStore:
    """A host's ``PriceStore``: ``get`` and ``put``, keyed as declared.

    The bucket comes from :func:`_declared_bucket`, so this backend agrees
    with ``MemoryPriceStore`` only if the two floors agree.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, int], Money] = {}

    def get(self, caip19, at_ms):
        return self._rows.get((caip19, _declared_bucket(at_ms)))

    def put(self, caip19, at_ms, price):
        self._rows[(caip19, _declared_bucket(at_ms))] = price


class UnbucketedStore:
    """``HostPriceStore`` with the floor removed: the negation fixture."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, int], Money] = {}

    def get(self, caip19, at_ms):
        return self._rows.get((caip19, at_ms))

    def put(self, caip19, at_ms, price):
        self._rows[(caip19, at_ms)] = price


def _recording_client() -> tuple[httpx.Client, list[str]]:
    """A client that records every URL and answers every key it is asked for."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        keys = str(request.url).rsplit("/", 1)[-1].split(",")
        return httpx.Response(
            200, json={"coins": {key: {"price": 1.5} for key in keys}}
        )

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


# --------------------------------------------------------------------------
# 1. The floor, derived in two places
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (0, 0),
        (PAST, 1_620_000_000_000),
        (1_620_001_831_000, 1_620_000_000_000),  # the spec's 12:00:31
        (1_620_001_859_000, 1_620_000_000_000),  # the spec's 12:00:59
        (EDGE, 1_620_000_000_000),
        (NEXT, 1_620_003_600_000),
    ],
)
def test_the_two_floors_agree_on_the_declared_bucket(instant, expected):
    # The expected column is derived from the rule in RELEASE_0.2.0 section 5,
    # not by asking either implementation. Move RESOLUTION_MS and this fails.
    assert RESOLUTION_MS == DECLARED_RESOLUTION_MS
    assert bucket_start_ms(instant) == expected
    assert _declared_bucket(instant) == expected


def test_the_bucket_survives_the_conversion_the_oracle_applies_to_it():
    # The historian floors to an hour, then defillama floors that to a second.
    # If the hour did not survive the second floor, a recorded historical URL
    # would not be reproducible. 3_600_000 ms is a whole 3600 seconds.
    for instant in (0, PAST, MID, EDGE, NEXT, 1_777_777_777_777):
        unix_seconds = bucket_start_ms(instant) // 1000
        assert unix_seconds * 1000 == bucket_start_ms(instant)
        assert unix_seconds % 3600 == 0


def test_flooring_twice_is_flooring_once():
    # The historian floors before it calls and MemoryPriceStore floors again
    # when it builds the key. A floor that moved on the second application
    # would put the write and the read in different buckets.
    for instant in (0, PAST, MID, EDGE, NEXT):
        assert bucket_start_ms(bucket_start_ms(instant)) == bucket_start_ms(instant)


# --------------------------------------------------------------------------
# 2. A host PriceStore, written from the port
# --------------------------------------------------------------------------


def test_a_host_store_written_from_the_port_is_a_price_store():
    assert isinstance(HostPriceStore(), PriceStore) is True
    assert isinstance(MemoryPriceStore(), PriceStore) is True
    assert isinstance(object(), PriceStore) is False


@pytest.mark.parametrize("store_type", [MemoryPriceStore, HostPriceStore])
def test_both_backends_answer_the_spec_worked_example_the_same_way(store_type):
    # 12:00:31 and 12:00:59 are one entry; 12:59:59.999 and 13:00:00 are two.
    # An implementation keyed by the raw instant fails the first assertion,
    # which is what UnbucketedStore below is for.
    store = store_type()
    mark = Money(Decimal("2949.68"), "USD")

    store.put(DAI, 1_620_001_831_000, mark)
    assert store.get(DAI, 1_620_001_859_000) == mark

    store.put(DAI, EDGE, mark)
    assert store.get(DAI, NEXT) is None


def test_an_unbucketed_store_fails_the_same_two_asks():
    # The negation of the test above, run rather than asserted in prose: with
    # the floor removed the 12:00:31 write is invisible at 12:00:59.
    store = UnbucketedStore()
    mark = Money(Decimal("2949.68"), "USD")

    store.put(DAI, 1_620_001_831_000, mark)
    assert store.get(DAI, 1_620_001_859_000) is None


def test_a_miss_is_none_and_never_a_zero_in_either_backend():
    for store in (MemoryPriceStore(), HostPriceStore()):
        assert store.get(NOTHING, PAST) is None
        assert store.get(NOTHING, PAST) != Money(Decimal("0"), "USD")


# --------------------------------------------------------------------------
# 3. A host HistoricalPriceOracle, written from the protocol
# --------------------------------------------------------------------------


def test_a_host_oracle_written_from_the_protocol_binds_to_the_chain():
    host = HostHistoricalOracle(frozenset({ETH}))

    assert isinstance(host, HistoricalPriceOracle) is True
    assert isinstance(host, PriceOracle) is False
    assert isinstance(Inquirer([]), HistoricalPriceOracle) is True


def test_the_chain_hands_a_host_oracle_the_ids_and_the_instant_it_was_given():
    # Both arguments are load bearing: the keys prove the id list arrived, the
    # amount proves the instant did. Asking at NEXT changes the amount, so an
    # oracle called at the wrong instant cannot pass this.
    host = HostHistoricalOracle(frozenset({ETH, DAI}))

    got = Inquirer([host]).usd_prices_at([ETH, DAI, ETH], PAST)

    assert host.calls == [((ETH, DAI), PAST)]
    assert got == {
        ETH: Money(Decimal(PAST), "USD"),
        DAI: Money(Decimal(PAST), "USD"),
    }
    assert got[ETH] != Money(Decimal(NEXT), "USD")


def test_first_wins_holds_for_two_host_oracles_at_an_instant():
    first = HostHistoricalOracle(frozenset({ETH}))
    second = HostHistoricalOracle(frozenset({ETH, DAI}))

    got = Inquirer([first, second]).usd_prices_at([ETH, DAI], PAST)

    assert first.calls == [((ETH, DAI), PAST)]
    assert second.calls == [((DAI,), PAST)]
    assert set(got) == {ETH, DAI}


def test_a_host_legacy_oracle_is_never_called_at_an_instant():
    legacy = HostLegacyOracle({ETH: Money(Decimal("999.99"), "USD")})
    host = HostHistoricalOracle(frozenset({ETH}))

    got = Inquirer([legacy, host]).usd_prices_at([ETH], PAST)

    assert legacy.calls == []
    assert got == {ETH: Money(Decimal(PAST), "USD")}
    assert got[ETH] != Money(Decimal("999.99"), "USD")


def test_the_generated_legacy_note_is_the_string_the_blind_golden_pinned():
    # `historian.PriceMarks.notes` carries this to a caller verbatim, and the
    # phase gate asserts the sentence letter for letter. Renaming the class
    # renames the note, which is why the class is built with the golden's own
    # name here rather than borrowed from it.
    recording_oracle = type(
        "RecordingOracle", (), {"usd_prices": lambda self, caip19s: {}}
    )

    assert Inquirer([recording_oracle()]).absences_at(PAST) == (GOLDEN_LEGACY_NOTE,)


def test_a_stated_reason_skips_the_oracle_without_asking_it_anything():
    pinned = HostPinnedOracle(PAST)
    fallback = HostHistoricalOracle(frozenset({ETH}))

    got = Inquirer([pinned, fallback]).usd_prices_at([ETH], NEXT)

    assert pinned.calls == []
    assert got == {ETH: Money(Decimal(NEXT), "USD")}
    assert Inquirer([pinned]).absences_at(NEXT) == (
        f"HostPinnedOracle is pinned at {PAST}; {NEXT} is not reachable",
    )
    assert Inquirer([pinned]).absences_at(PAST) == ()


def test_absences_at_asks_no_oracle_for_a_price():
    legacy = HostLegacyOracle({ETH: Money(Decimal("1"), "USD")})
    pinned = HostPinnedOracle(PAST)
    host = HostHistoricalOracle(frozenset({ETH}))

    notes = Inquirer([legacy, pinned, host]).absences_at(NEXT)

    assert len(notes) == 2
    assert notes[0] == (
        "HostLegacyOracle has no usd_prices_at; it answers the current "
        "instant only"
    )
    assert "HostPinnedOracle" in notes[1]
    assert legacy.calls == []
    assert pinned.calls == []
    assert host.calls == []


def test_a_nested_chain_reports_the_oracles_it_skipped():
    # An Inquirer is a HistoricalPriceOracle, so a chain can hold a chain. The
    # nested legacy oracle must still be named, at the nested chain's position.
    inner = Inquirer([HostLegacyOracle({})])
    outer = Inquirer([HostPinnedOracle(PAST), inner])

    notes = outer.absences_at(NEXT)

    assert len(notes) == 2
    assert "HostPinnedOracle" in notes[0]
    assert notes[1] == (
        "HostLegacyOracle has no usd_prices_at; it answers the current "
        "instant only"
    )


def test_an_empty_ask_costs_no_oracle_call_at_an_instant():
    host = HostHistoricalOracle(frozenset({ETH}))

    assert Inquirer([host]).usd_prices_at([], PAST) == {}
    assert host.calls == []


# --------------------------------------------------------------------------
# 4. The wire: the oracle does not bucket, the store does
# --------------------------------------------------------------------------


def test_the_oracle_puts_the_instant_on_the_wire_without_bucketing_it():
    # 00:30 floors to 00:00. If the oracle bucketed as well, the URL would
    # carry 1620000000 and the historian's own floor would be invisible.
    client, seen = _recording_client()

    DefiLlamaOracle(client).usd_prices_at([ETH], MID)

    assert seen == [f"{LLAMA}/prices/historical/1620001800/coingecko:ethereum"]
    assert "1620000000" not in seen[0]
    assert bucket_start_ms(MID) // 1000 == 1_620_000_000


def test_the_historian_floor_is_what_produces_the_recorded_second():
    # The composition the historian will perform, run here so the two floors
    # are shown to compose into the URL the cassette actually holds.
    client, seen = _recording_client()

    DefiLlamaOracle(client).usd_prices_at([ETH, POOLONLY, NOTHING], bucket_start_ms(MID))

    assert seen == [GOLDEN_PAST_TRIPLE_URL]


def test_the_wave_builds_the_url_the_blind_golden_and_the_cassette_both_hold():
    # Three artifacts, written independently: the oracle's URL builder, the
    # golden's pinned literal, and the committed recording. A sorted order
    # that moved, or a second that moved, separates them.
    client, seen = _recording_client()

    DefiLlamaOracle(client).usd_prices_at([ETH, POOLONLY, NOTHING], PAST)

    recorded = json.loads(CASSETTE.read_text(encoding="utf-8"))
    urls = [item["request"]["url"] for item in recorded["interactions"]]
    assert seen == [GOLDEN_PAST_TRIPLE_URL]
    assert GOLDEN_PAST_TRIPLE_URL in urls


def test_the_sub_second_remainder_never_reaches_the_wire():
    client, seen = _recording_client()

    DefiLlamaOracle(client).usd_prices_at([ETH], PAST)
    DefiLlamaOracle(client).usd_prices_at([ETH], PAST + 999)

    assert seen[0] == seen[1] == f"{LLAMA}/prices/historical/1620000000/coingecko:ethereum"


# --------------------------------------------------------------------------
# 5. The three orders composed, against the committed recording
# --------------------------------------------------------------------------


def test_the_recorded_feed_answers_a_different_number_at_each_bucket():
    # The whole wave in one line each: order 2 builds the URL, order 1 walks
    # the chain and checks the currency, order 0 keys the answer by bucket.
    # The two instants carry different numbers, so a URL built at the wrong
    # second fails rather than passing quietly.
    cassette = cassettes.load(CASSETTE)
    inquirer = Inquirer([DefiLlamaOracle(cassette.client())])
    store = MemoryPriceStore()

    past = inquirer.usd_prices_at([ETH, POOLONLY, NOTHING], PAST)
    later = inquirer.usd_prices_at([ETH], NEXT)

    assert past[ETH] == Money(Decimal("2949.68"), "USD")
    assert later[ETH] == Money(Decimal("3419.44"), "USD")
    assert POOLONLY not in past
    assert NOTHING not in past

    store.put(ETH, PAST, past[ETH])
    store.put(ETH, NEXT, later[ETH])

    assert store.get(ETH, MID) == Money(Decimal("2949.68"), "USD")
    assert store.get(ETH, EDGE) == Money(Decimal("2949.68"), "USD")
    assert store.get(ETH, NEXT) == Money(Decimal("3419.44"), "USD")


def test_a_defillama_oracle_states_no_unreachable_instant():
    # "not listed" is an absent response key; "cannot reach" is a stated
    # sentence. This oracle only ever does the first, so it contributes
    # nothing to the notes even for an instant it holds no mark for.
    cassette = cassettes.load(CASSETTE)
    inquirer = Inquirer([DefiLlamaOracle(cassette.client())])

    assert inquirer.absences_at(NEXT) == ()
    assert inquirer.usd_prices_at([POOLONLY], NEXT) == {}
    assert inquirer.absences_at(NEXT) == ()


# --------------------------------------------------------------------------
# 6. The USD boundary, held on both sides
# --------------------------------------------------------------------------


def test_a_eur_quote_is_refused_by_the_chain_and_by_the_store():
    eur = Money(Decimal("2949.68"), "EUR")

    class EurOracle:
        def usd_prices_at(self, caip19s, at_ms):
            return {ETH: eur}

    with pytest.raises(CurrencyMismatchError):
        Inquirer([EurOracle()]).usd_prices_at([ETH], PAST)
    with pytest.raises(CurrencyMismatchError):
        MemoryPriceStore().put(ETH, PAST, eur)


def test_a_non_money_quote_is_refused_by_the_chain_and_by_the_store():
    # SEAM FINDING (red). The wave declares one rule on two sides: "put
    # enforces USD, the same rule inquirer._checked_usd enforces on the oracle
    # side". It is not the same rule. store.put refuses a bare Decimal with
    # ValidationError; _checked_usd reaches for `.currency` and lets a bare
    # AttributeError out of a host-facing method, which is outside the
    # auradefi.errors taxonomy (rule #4). Oracles are host supplied, so this
    # is the input a host actually gets wrong.
    class BareDecimalOracle:
        def usd_prices_at(self, caip19s, at_ms):
            return {ETH: Decimal("2949.68")}

    with pytest.raises(AuradefiError):
        MemoryPriceStore().put(ETH, PAST, Decimal("2949.68"))
    with pytest.raises(AuradefiError):
        Inquirer([BareDecimalOracle()]).usd_prices_at([ETH], PAST)


# --------------------------------------------------------------------------
# 7. The domain of at_ms, as three modules of one wave see it
# --------------------------------------------------------------------------


def test_a_pre_epoch_instant_is_refused_before_it_reaches_a_host_oracle():
    # SEAM FINDING (red). bucket_start_ms refuses a negative instant and says
    # why ("this package prices nothing pre-1970"); DefiLlamaOracle
    # .usd_prices_at refuses it with SourceError. Inquirer.usd_prices_at
    # accepts it and hands it to every oracle in the chain, so whether a
    # pre-epoch instant is refused depends on which oracle happens to be
    # first, and a host oracle bound only by HistoricalPriceOracle is handed
    # a value the package's own oracle calls illegal. Inquirer is host facing:
    # embed/bootstrap.py builds one for every sandbox and live handle.
    host = HostHistoricalOracle(frozenset({ETH}))

    with pytest.raises(ValidationError):
        Inquirer([host]).usd_prices_at([ETH], -1)
    assert host.calls == []


def test_the_two_modules_that_do_refuse_a_pre_epoch_instant_agree():
    client, seen = _recording_client()

    with pytest.raises(ValidationError):
        bucket_start_ms(-1)
    with pytest.raises(AuradefiError):
        DefiLlamaOracle(client).usd_prices_at([ETH], -1)
    assert seen == []


# --------------------------------------------------------------------------
# 8. One asset, two spellings: the oracle collapses them, the store does not
# --------------------------------------------------------------------------


def test_the_wire_key_and_the_cache_key_disagree_about_one_asset():
    # parse_caip19 accepts a checksummed erc20 reference, and
    # Inquirer.usd_prices_at keys its answer by the spelling it was handed.
    # coin_key lowercases, so both spellings are ONE key on the wire.
    # The store compares byte for byte, so they are TWO entries in the cache.
    # Nothing between them canonicalises. Reported as a finding: the historian
    # is the party that must call assets.caip.canonical_caip19.
    assert coin_key(DAI) == coin_key(DAI_CHECKSUMMED)

    store = MemoryPriceStore()
    store.put(DAI, PAST, Money(Decimal("1.05"), "USD"))

    assert store.get(DAI_CHECKSUMMED, PAST) is None

    client, seen = _recording_client()
    got = Inquirer([DefiLlamaOracle(client)]).usd_prices_at([DAI_CHECKSUMMED], PAST)

    assert list(got) == [DAI_CHECKSUMMED]
    assert seen == [
        f"{LLAMA}/prices/historical/1620000000/ethereum:"
        "0x6b175474e89094c44da98b954eedeac495271d0f"
    ]
