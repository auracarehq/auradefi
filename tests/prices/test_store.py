"""The price cache port, its memory backend, and the declared resolution
(SPEC §3.2 ``prices/store.py``; RELEASE_0.2.0 §5).

RELEASE_0.2.0 §5 asks the resolution question and demands an answer that
is testable: "a mark asked for at 12:00:31 and a mark asked for at
12:00:59 must either be the same cache entry or provably different ones.
State which, in DECISIONS.md, and test the boundary." The answer is one
hour, so two instants inside one hour are the SAME entry and the pair
that straddles the hour are different ones. Both halves are pinned below
with a fixture that cannot pass vacuously: each "different entries" test
first proves the write landed, so a ``put`` that stored nothing at all
would fail rather than sail through on a ``None``.

Also pinned: flooring is idempotent (the historian floors, then the store
floors again); the key is the canonical CAIP-19 string and never the
``ast_`` registry id, which two chains' USDC would share; only marks are
stored, so a manual override written after a failed lookup is seen on the
next call; and every refusal stays inside the ``auradefi.errors``
taxonomy instead of leaking a builtin.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

import pytest

from auradefi.errors import AuradefiError, CurrencyMismatchError, ValidationError
from auradefi.money.fiat import Money
from auradefi.prices import store as store_module
from auradefi.prices.store import (
    RESOLUTION_MS,
    MemoryPriceStore,
    PriceStore,
    bucket_start_ms,
)

STORE_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "auradefi"
    / "prices"
    / "store.py"
)

ETH = "eip155:1/slip44:60"
DAI = "eip155:1/erc20:0x6b175474e89094c44da98b954eedeac495271d0f"

#: One token, two chains. DECISIONS.md mints ``asset_id`` as a hash over a
#: SET of canonical CAIP-19s, so a registry that holds both of these holds
#: them under ONE ``ast_`` id. Keyed by that id the two would be a single
#: cache entry at a single price.
USDC_ETHEREUM = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_POLYGON = "eip155:137/erc20:0x2791bca1f2de4661ed88a30c99a7a9449aa84174"

#: A token no registry has ever been told about. ``AssetRegistry()`` is not
#: constructed anywhere in the shipped runtime path, so every id is this
#: one as far as the store is concerned.
LONG_TAIL = "eip155:1/erc20:0x00000000000000000000000000000000000f00d5"

# 2021-05-03 UTC, an hour taken apart. The instants below are the shape
# RELEASE_0.2.0 §5 illustrates with 12:00:31 and 12:00:59: two readings
# inside one hour, then the pair that straddles the hour's end.
HOUR_START = 1_620_000_000_000  # 00:00:00.000
HALF_PAST = 1_620_001_800_000  # 00:30:00.000
AT_30_31 = 1_620_001_831_000  # 00:30:31.000
AT_30_59 = 1_620_001_859_000  # 00:30:59.000
HOUR_END = 1_620_003_599_999  # 00:59:59.999
NEXT_HOUR = 1_620_003_600_000  # 01:00:00.000

#: The far end of the domain: 9999-12-31T23:59:59.999Z and its hour.
YEAR_9999 = 253_402_300_799_999
YEAR_9999_HOUR = 253_402_297_200_000


def usd(amount: str) -> Money:
    return Money(Decimal(amount), "USD")


# --------------------------------------------------------- the resolution


# pins: the declared cache resolution is one hour expressed in
#       milliseconds, the single number the historian and every oracle
#       import so that "which instants share a mark" has one answer for
#       the whole prices domain.
def test_the_declared_resolution_is_one_hour_of_milliseconds():
    assert RESOLUTION_MS == 3_600_000
    assert RESOLUTION_MS == 60 * 60 * 1000


# ------------------------------------------------------------- flooring


# pins: every instant inside one hour floors to that hour's start, so all
#       of them name one cache entry. This is the "same entry" half of the
#       resolution question RELEASE_0.2.0 §5 requires an answer to.
@pytest.mark.parametrize(
    ("at_ms", "expected"),
    [
        (0, 0),
        (HOUR_START, HOUR_START),
        (HALF_PAST, HOUR_START),
        (AT_30_31, HOUR_START),
        (AT_30_59, HOUR_START),
        (HOUR_END, HOUR_START),
        (YEAR_9999, YEAR_9999_HOUR),
    ],
    ids=["epoch", "on-the-hour", "half-past", "30:31", "30:59", "59:59.999", "year-9999"],
)
def test_an_instant_floors_onto_the_hour_that_contains_it(at_ms, expected):
    assert bucket_start_ms(at_ms) == expected


# pins: the first millisecond of the next hour opens its own bucket, so
#       the hour's end and the hour's start are provably different
#       entries and a mark never leaks across the boundary.
def test_the_next_hour_opens_its_own_bucket():
    assert bucket_start_ms(NEXT_HOUR) == NEXT_HOUR
    assert bucket_start_ms(NEXT_HOUR) != bucket_start_ms(HOUR_END)


# pins: flooring is idempotent, so applying it twice lands on the same
#       bucket. The historian floors an instant before it asks and the
#       store floors again when it builds the key; a floor that moved on
#       the second application would file the read and the write apart.
@pytest.mark.parametrize("at_ms", [0, AT_30_31, HOUR_END], ids=["epoch", "30:31", "59:59.999"])
def test_flooring_a_bucket_start_returns_it_unchanged(at_ms):
    once = bucket_start_ms(at_ms)
    assert bucket_start_ms(once) == once


# --------------------------------------------------- flooring: refusals


# pins: a bool instant is refused. `bool` is an `int` subclass, so an
#       unguarded `True` floors as the instant 1 (bucket 0) and `False` as
#       the epoch, filing a flag as a timestamp and answering with a real
#       cached mark. The type check therefore runs before any arithmetic.
@pytest.mark.parametrize("at_ms", [True, False], ids=["true", "false"])
def test_a_bool_instant_is_refused_before_it_can_floor_as_a_number(at_ms):
    with pytest.raises(ValidationError):
        bucket_start_ms(at_ms)


# pins: a non-integer instant raises ValidationError rather than the
#       TypeError that `'0' < 0` and `'0' // 3_600_000` would produce, so
#       a caller's `except AuradefiError` holds at this door.
def test_a_string_instant_raises_validation_error():
    with pytest.raises(ValidationError):
        bucket_start_ms("0")


# pins: a negative instant is refused rather than floored. Floor division
#       carries a negative instant DOWN to a bucket before the epoch
#       (-1 // 3_600_000 is -1, naming bucket -3_600_000), and this
#       package prices nothing pre-1970.
@pytest.mark.parametrize(
    "at_ms",
    [-1, -RESOLUTION_MS, -RESOLUTION_MS - 1],
    ids=["minus-one", "exact-negative-hour", "just-below"],
)
def test_a_negative_instant_is_refused(at_ms):
    with pytest.raises(ValidationError):
        bucket_start_ms(at_ms)


# pins: no hostile instant escapes the auradefi.errors taxonomy. Each of
#       these reaches a different builtin when unguarded (AttributeError,
#       TypeError from the comparison, TypeError from the floor divide),
#       and only exception classes declared in errors.py may be raised.
@pytest.mark.parametrize(
    "at_ms",
    [None, "0", 1.5, Decimal("0"), True, -1, object()],
    ids=["none", "str", "float", "decimal", "bool", "negative", "object"],
)
def test_no_hostile_instant_leaks_a_builtin(at_ms):
    with pytest.raises(AuradefiError):
        bucket_start_ms(at_ms)


# --------------------------------------------------------- the memory store


# pins: a lookup for an id nothing was ever stored under answers None. A
#       miss is never an error and never a zero (rule #8): a caller that
#       could not tell a miss from a mark of zero would report a held
#       asset as worthless.
def test_a_miss_answers_none_and_does_not_raise():
    assert MemoryPriceStore().get(ETH, 0) is None


# pins: the worked example from RELEASE_0.2.0 §5. A mark written at
#       00:30:31 is returned for a lookup at 00:30:59, because both
#       instants floor to the same hour: they are ONE cache entry, which
#       is the answer this project owes that paragraph.
def test_two_instants_inside_one_hour_are_one_cache_entry():
    store = MemoryPriceStore()
    mark = usd("1.0002")
    store.put(DAI, AT_30_31, mark)
    assert store.get(DAI, AT_30_59) is mark


# pins: the hour boundary separates two entries. A mark written at
#       00:59:59.999 is absent at 01:00:00. The write is proved to have
#       landed first, so a `put` that stored nothing could not pass this
#       on the None alone.
def test_the_hour_boundary_separates_two_cache_entries():
    store = MemoryPriceStore()
    mark = usd("1.0002")
    store.put(DAI, HOUR_END, mark)
    assert store.get(DAI, HOUR_END) is mark
    assert store.get(DAI, NEXT_HOUR) is None


# pins: the key is the canonical CAIP-19 string, never the `ast_` registry
#       id. DECISIONS.md hashes `asset_id` over a SET of CAIP-19s, so USDC
#       on Ethereum and USDC on Polygon share one `ast_` id; keyed by that
#       id these two writes would be one entry and the second price would
#       overwrite the first.
def test_one_token_on_two_chains_keeps_two_marks():
    store = MemoryPriceStore()
    on_ethereum = usd("1.0001")
    on_polygon = usd("0.9987")
    store.put(USDC_ETHEREUM, AT_30_31, on_ethereum)
    store.put(USDC_POLYGON, AT_30_31, on_polygon)
    assert store.get(USDC_ETHEREUM, AT_30_31) is on_ethereum
    assert store.get(USDC_POLYGON, AT_30_31) is on_polygon


# pins: an id no asset registry knows is cached like any other, because
#       the store resolves nothing through the registry. A store that
#       looked the id up would refuse the long-tail tokens the on-chain
#       oracle exists to price, which is every token that matters here.
def test_an_unregistered_long_tail_id_caches_and_reads_back():
    store = MemoryPriceStore()
    mark = usd("0.000000000000123")
    store.put(LONG_TAIL, AT_30_31, mark)
    assert store.get(LONG_TAIL, AT_30_59) is mark


# pins: a miss is not remembered, so a mark written after a failed lookup
#       is returned on the very next call. "We could not price it then" is
#       not the claim "it is unpriceable", and a host adding a manual
#       override must not be answered from a cached absence.
def test_a_mark_written_after_a_miss_is_visible_immediately():
    store = MemoryPriceStore()
    assert store.get(DAI, AT_30_31) is None
    override = usd("1.0000")
    store.put(DAI, AT_30_31, override)
    assert store.get(DAI, AT_30_31) is override


# pins: marks live on the instance, so two stores never see each other's.
#       A dict built once at class level is the classic version of this
#       bug, and it makes one test's fixture answer another test's lookup.
def test_two_stores_do_not_share_marks():
    first = MemoryPriceStore()
    second = MemoryPriceStore()
    first.put(DAI, AT_30_31, usd("1.0002"))
    assert second.get(DAI, AT_30_31) is None


# pins: a second write into one bucket replaces the mark already there, so
#       a manual override supersedes an aggregator's number instead of
#       being dropped on the floor.
def test_a_second_write_into_one_bucket_replaces_the_mark():
    store = MemoryPriceStore()
    aggregated = usd("0.9903")
    override = usd("1.0000")
    store.put(DAI, AT_30_31, aggregated)
    store.put(DAI, AT_30_59, override)
    assert store.get(DAI, AT_30_31) is override


# ------------------------------------------------------ the store's refusals


# pins: a mark denominated in anything but USD is refused, the same
#       boundary `inquirer._checked_usd` holds on the oracle side, and
#       nothing is stored. Admitted here, a EUR number is later multiplied
#       by a quantity and stamped "USD": a total wrong by the FX rate with
#       nothing raised anywhere.
@pytest.mark.parametrize(
    "currency",
    ["EUR", "GBP", "eip155:1/slip44:60"],
    ids=["euro", "sterling", "caip19-denominated"],
)
def test_a_mark_that_is_not_usd_is_refused_and_stores_nothing(currency):
    store = MemoryPriceStore()
    with pytest.raises(CurrencyMismatchError):
        store.put(DAI, AT_30_31, Money(Decimal("1"), currency))
    assert store.get(DAI, AT_30_31) is None


# pins: a price that is not Money is refused with ValidationError, not the
#       AttributeError that reading `.currency` off a Decimal produces. A
#       bare Decimal is the natural mistake: it is the amount without the
#       tag, and stored untagged it becomes a number in no currency.
@pytest.mark.parametrize(
    "price",
    [Decimal("1"), "1", 1, 1.0, None],
    ids=["decimal", "str", "int", "float", "none"],
)
def test_a_price_that_is_not_money_is_refused(price):
    store = MemoryPriceStore()
    with pytest.raises(ValidationError):
        store.put(DAI, AT_30_31, price)


# pins: a non-string asset id is refused with ValidationError rather than
#       being keyed as itself. 60 is the slip44 coin type a caller reaches
#       for instead of the CAIP-19, and an unguarded store would file a
#       mark under the integer and never find it again.
@pytest.mark.parametrize(
    "caip19", [60, None, b"eip155:1/slip44:60"], ids=["int", "none", "bytes"]
)
def test_a_non_string_asset_id_is_refused(caip19):
    store = MemoryPriceStore()
    with pytest.raises(ValidationError):
        store.put(caip19, AT_30_31, usd("1"))


# pins: put builds its key through bucket_start_ms, so every instant the
#       floor refuses the store refuses too. Without that, `put(id, True,
#       mark)` files a mark under the raw flag and no lookup ever meets it.
@pytest.mark.parametrize(
    "at_ms", [True, -1, "0", None], ids=["bool", "negative", "str", "none"]
)
def test_a_write_at_an_instant_the_floor_refuses_is_refused(at_ms):
    store = MemoryPriceStore()
    with pytest.raises(ValidationError):
        store.put(DAI, at_ms, usd("1"))


# pins: a non-string asset id is refused on the READ door too, not only on
#       the write door. The port declares one refusal for both, so no
#       backend may answer None for `get(60, at_ms)` and let a question
#       nothing could ever have been written under be read as an empty
#       cache. The hour is populated first, so the refusal is proved to
#       come before the lookup rather than out of an empty dict.
@pytest.mark.parametrize(
    "caip19", [60, None, b"eip155:1/slip44:60"], ids=["int", "none", "bytes"]
)
def test_a_non_string_asset_id_is_refused_on_a_read(caip19):
    store = MemoryPriceStore()
    store.put(DAI, AT_30_31, usd("1.0002"))
    with pytest.raises(ValidationError):
        store.get(caip19, AT_30_31)


# pins: get builds its key through bucket_start_ms, so every instant the
#       floor refuses the read refuses too. A read that skipped the floor
#       would look under the raw flag, or under a bucket before the epoch,
#       and hand the resulting absence back as a miss.
@pytest.mark.parametrize(
    "at_ms",
    [True, -1, "0", None, 1.5],
    ids=["bool", "negative", "str", "none", "float"],
)
def test_a_read_at_an_instant_the_floor_refuses_is_refused(at_ms):
    store = MemoryPriceStore()
    store.put(DAI, AT_30_31, usd("1.0002"))
    with pytest.raises(ValidationError):
        store.get(DAI, at_ms)


def _read(store: MemoryPriceStore, caip19: object) -> object:
    return store.get(caip19, AT_30_31)


def _write(store: MemoryPriceStore, caip19: object) -> object:
    return store.put(caip19, AT_30_31, usd("1"))


# pins: an unhashable asset id is refused before the dict is touched, on
#       both doors. Reached, `self._marks[([], bucket)]` raises TypeError:
#       unhashable type: 'list', a builtin standing in front of a caller
#       whose `except AuradefiError` was supposed to hold (rule #4).
@pytest.mark.parametrize("caip19", [[], {}, set()], ids=["list", "dict", "set"])
@pytest.mark.parametrize("operation", [_read, _write], ids=["get", "put"])
def test_an_unhashable_asset_id_is_refused_before_the_dict_is_touched(
    operation, caip19
):
    store = MemoryPriceStore()
    with pytest.raises(ValidationError):
        operation(store, caip19)


# ------------------------------------------------------------ the protocol


# pins: the memory backend satisfies PriceStore structurally, with no base
#       class and no registration, which is what lets a host swap in Redis
#       or Postgres without this package growing a dependency.
def test_the_memory_backend_is_a_price_store_at_runtime():
    assert isinstance(MemoryPriceStore(), PriceStore) is True


# pins: the protocol is runtime_checkable against the METHODS it declares,
#       so an object that has neither is refused. Without the decorator,
#       or with an empty protocol body, everything would conform.
def test_an_arbitrary_object_is_not_a_price_store():
    assert isinstance(object(), PriceStore) is False


# pins: the read side of the seam is `get(self, caip19, at_ms)`, exactly.
#       A host implements this from the signature alone, so a renamed or
#       reordered parameter silently breaks every keyword call site.
@pytest.mark.parametrize(
    "owner", [PriceStore, MemoryPriceStore], ids=["protocol", "memory"]
)
def test_get_signature_is_self_caip19_at_ms(owner):
    assert list(inspect.signature(owner.get).parameters) == [
        "self",
        "caip19",
        "at_ms",
    ]


# pins: the write side of the seam is `put(self, caip19, at_ms, price)`,
#       exactly, with the instant before the price so both methods read
#       the same way round.
@pytest.mark.parametrize(
    "owner", [PriceStore, MemoryPriceStore], ids=["protocol", "memory"]
)
def test_put_signature_is_self_caip19_at_ms_price(owner):
    assert list(inspect.signature(owner.put).parameters) == [
        "self",
        "caip19",
        "at_ms",
        "price",
    ]


# pins: the module declares these four things and no more, so the
#       resolution, the floor, the port and the backend are the whole
#       public surface the historian and the oracles may import.
def test_the_module_declares_the_resolution_the_floor_the_port_and_the_backend():
    assert sorted(store_module.__all__) == [
        "MemoryPriceStore",
        "PriceStore",
        "RESOLUTION_MS",
        "bucket_start_ms",
    ]


# --------------------------------------------- structural independence gate


def _imported_names_in(source: str) -> list[str]:
    """Absolute dotted names imported by ``source``, relatives resolved."""
    tree = ast.parse(source)
    package = ["auradefi", "prices"]
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join(anchor + ([node.module] if node.module else []))
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names if base)
    return names


def _imported_names() -> list[str]:
    """Absolute dotted names imported by store.py, relatives resolved."""
    return _imported_names_in(STORE_SOURCE.read_text(encoding="utf-8"))


#: The two auradefi modules store.py may reach, and the only two.
ALLOWED_AURADEFI_MODULES = ("auradefi.errors", "auradefi.money.fiat")


def _foreign_auradefi_imports(names: Iterable[str]) -> list[str]:
    """Names in ``names`` that reach an auradefi module outside those two.

    Matched by PREFIX against each allowed module, never by parent
    segment. ``import auradefi.portfolio`` arrives here as the single
    dotted name ``auradefi.portfolio``, whose parent segment is the bare
    package root, so a filter that asked whether the PARENT was allowed
    had to carry ``"auradefi"`` in its allowed set and then waved every
    plain ``import auradefi.<anything>`` straight through, catching only
    the ``from X import Y`` spelling. The children the prefix test admits
    are the symbols imported FROM the two allowed modules, and those are
    the only children under either name.
    """
    return sorted(
        name
        for name in names
        if (name == "auradefi" or name.startswith("auradefi."))
        and not any(
            name == allowed or name.startswith(allowed + ".")
            for allowed in ALLOWED_AURADEFI_MODULES
        )
    )


# pins: the parser reaches this module's real imports. A helper that
#       silently returned nothing would make both gates below pass over
#       any import at all, which is the failure mode they exist to catch.
def test_the_import_scan_reads_the_module():
    names = _imported_names()
    assert "auradefi.errors" in names
    assert "auradefi.money.fiat.Money" in names


# pins: the store performs no HTTP, resolves nothing through the asset
#       registry, and reads no clock. Each of those would turn a pure
#       keying function into something with an outcome that depends on
#       when and where it ran.
@pytest.mark.parametrize(
    "banned",
    ["httpx", "auradefi.assets", "auradefi.clock", "time", "datetime"],
    ids=["httpx", "assets", "clock", "time", "datetime"],
)
def test_the_store_module_imports_nothing_it_must_not(banned):
    offenders = [
        name
        for name in _imported_names()
        if name == banned or name.startswith(banned + ".")
    ]
    assert not offenders, f"store.py must not import {banned}: {offenders}"


# pins: the only auradefi modules the store depends on are errors and
#       money.fiat. A store that reached further up the package would drag
#       the historian's dependencies into a module the oracles all import.
def test_the_store_module_imports_only_errors_and_money():
    offenders = _foreign_auradefi_imports(_imported_names())
    assert not offenders, f"store.py imports only errors and money.fiat: {offenders}"


#: Ways of reaching a third auradefi package, written as source so the scan
#: above parses them exactly as it parses the module. Both spellings are
#: here on purpose: the plain form went undetected for a release because it
#: yields one dotted name under the package root, while the ``from`` form
#: yields a two-segment child that a parent-segment filter did catch. A
#: gate proved against one spelling guards that spelling alone.
FOREIGN_IMPORT_SOURCES = [
    ("import auradefi.portfolio", ["auradefi.portfolio"]),
    ("import auradefi.ledger", ["auradefi.ledger"]),
    ("import auradefi.positions", ["auradefi.positions"]),
    ("import auradefi.money.crypto", ["auradefi.money.crypto"]),
    ("import auradefi", ["auradefi"]),
    ("from auradefi.assets import caip", ["auradefi.assets", "auradefi.assets.caip"]),
    (
        "from auradefi.prices.historian import Historian",
        ["auradefi.prices.historian", "auradefi.prices.historian.Historian"],
    ),
    ("from . import historian", ["auradefi.prices", "auradefi.prices.historian"]),
]

#: The module's real imports, one line each. The gate has to stay silent on
#: every one of these, symbols included, or it is a gate nothing can pass.
ALLOWED_IMPORT_SOURCES = [
    "from auradefi.errors import ValidationError",
    "from auradefi.errors import require_int, require_str",
    "from auradefi.money.fiat import Money",
    "from __future__ import annotations",
    "from typing import Protocol, runtime_checkable",
    "import ast",
]


# pins: the dependency gate flags a third auradefi package written EITHER
#       way round, plain `import auradefi.x` as well as `from auradefi.x
#       import y`. The plain form is the one that slips past a filter
#       comparing parent segments, so a gate proved only against the
#       `from` form is a gate the next stray import walks around.
@pytest.mark.parametrize(
    ("line", "expected"),
    FOREIGN_IMPORT_SOURCES,
    ids=[line for line, _ in FOREIGN_IMPORT_SOURCES],
)
def test_the_dependency_gate_flags_a_third_package_written_either_way(line, expected):
    assert _foreign_auradefi_imports(_imported_names_in(line)) == expected


# pins: the dependency gate stays silent on errors, money.fiat and the
#       symbols taken from them, so its verdict on store.py is a reading of
#       store.py and not a filter that flags whatever it is shown.
@pytest.mark.parametrize("line", ALLOWED_IMPORT_SOURCES, ids=ALLOWED_IMPORT_SOURCES)
def test_the_dependency_gate_stays_silent_on_what_the_store_may_import(line):
    assert _foreign_auradefi_imports(_imported_names_in(line)) == []
