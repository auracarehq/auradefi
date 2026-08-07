"""Inquirer + PriceOracle seam: first-wins USD aggregation (SPEC §3.3).

Behaviour pinned: deduplicate preserving first occurrence; oracles
queried in construction order; each subsequent oracle asked only for the
still-unpriced ids; remaining oracles skipped once everything is priced;
unpriced ids ABSENT from the result, never an error; syntactically
invalid CAIP-19 raises CaipParseError BEFORE any oracle call. The seam
is structural: inquirer.py imports no oracle module and no httpx.

Phase 12 adds a SECOND protocol beside the first, `HistoricalPriceOracle`,
and two Inquirer methods that use it. `PriceOracle` itself is byte-stable:
it is runtime_checkable, so a second member on it would make `isinstance`
false for every host oracle carrying only `usd_prices`, and the rows that
prove it did not happen are here (`test_price_oracle_gains_no_second_member`
and the two signature rows). `usd_prices_at` walks the same chain at an
instant under two extra skip rules, and `absences_at` says which oracles
were skipped, because a `{}` from an oracle that was asked and a silence
from an oracle that was never asked reach the merged result identically.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from auradefi.errors import CaipParseError, CurrencyMismatchError, ValidationError
from auradefi.money.fiat import Money
from auradefi.prices import inquirer as inquirer_module
from auradefi.prices.inquirer import HistoricalPriceOracle, Inquirer, PriceOracle

ETH = "eip155:1/slip44:60"
DAI = "eip155:1/erc20:0x6b175474e89094c44da98b954eedeac495271d0f"

#: 2021-05-03T00:00:00Z, and one hour after it. Two instants, so a fixture
#: that can reach one and not the other is expressible.
PAST = 1_620_000_000_000
NEXT = 1_620_003_600_000

#: The generated sentence for an oracle that carries only `usd_prices`,
#: consumed verbatim by `historian.PriceMarks.notes`
#: (tests/golden/test_phase12_historian.py::LEGACY_NOTE).
LEGACY_NOTE = (
    "RecordingOracle has no usd_prices_at; it answers the current instant only"
)

INQUIRER_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "auradefi"
    / "prices"
    / "inquirer.py"
)


class RecordingOracle:
    """Conforming PriceOracle stub that logs every request verbatim."""

    def __init__(self, prices: dict[str, Money]) -> None:
        self.prices = dict(prices)
        self.calls: list[list[str]] = []

    def usd_prices(self, caip19s: Sequence[str]) -> dict[str, Money]:
        self.calls.append(list(caip19s))
        return {c: self.prices[c] for c in caip19s if c in self.prices}


class InstantOracle(RecordingOracle):
    """A phase-12 oracle: both members, both recorded with their arguments.

    `at_prices` defaults to `prices`. A test that has to prove the past
    answer is not today's answer passes both dicts with different numbers,
    so an implementation falling back to `usd_prices` is visible in the
    value and not only in the call log.
    """

    def __init__(
        self,
        prices: dict[str, Money],
        at_prices: dict[str, Money] | None = None,
    ) -> None:
        super().__init__(prices)
        self.at_prices = dict(prices if at_prices is None else at_prices)
        self.at_calls: list[tuple[list[str], int]] = []

    def usd_prices_at(
        self, caip19s: Sequence[str], at_ms: int
    ) -> dict[str, Money]:
        self.at_calls.append((list(caip19s), at_ms))
        return {c: self.at_prices[c] for c in caip19s if c in self.at_prices}


class PinnedOracle(InstantOracle):
    """An instant oracle that can reach ONE instant and states the rest.

    Both halves are reachable from one fixture on purpose: at
    `pinned_at_ms` it prices and must be called, at any other instant it
    returns `reason` and must never be called. A fixture that could only
    ever be unreachable would pass a skip test that skipped everything.
    """

    def __init__(
        self, prices: dict[str, Money], pinned_at_ms: int, reason: str
    ) -> None:
        super().__init__(prices)
        self.pinned_at_ms = pinned_at_ms
        self.reason = reason

    def unreachable_instant(self, at_ms: int) -> str | None:
        return None if at_ms == self.pinned_at_ms else self.reason


class BluntOracle(RecordingOracle):
    """`usd_prices_at` is present and is NOT callable: a string.

    This is the difference between `callable(getattr(...))` and both
    `hasattr` and a runtime_checkable `isinstance`, each of which calls
    this object historical and then fails on the call.
    """

    usd_prices_at = "not a method"


class SpeakingLegacyOracle(RecordingOracle):
    """No `usd_prices_at`, but it states its own reason for the absence."""

    def __init__(self, prices: dict[str, Money], reason: str) -> None:
        super().__init__(prices)
        self.reason = reason

    def unreachable_instant(self, at_ms: int) -> str | None:
        return self.reason


class ProxyOracle:
    """A wrapper forwarding to an inner oracle through `__getattr__`.

    `inspect.getattr_static` finds nothing on it, and a runtime_checkable
    `isinstance` uses `getattr_static`, so both call this a legacy oracle.
    `callable(getattr(...))` sees the wrapped method, which is the probe
    `embed/facade.py:113` settled on for exactly this shape.
    """

    def __init__(self, inner: object) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)


# ------------------------------------------------------- first-wins merging


def test_first_wins_dedup_and_second_oracle_asked_only_for_unpriced():
    # Acceptance vector: o1 prices ETH; o2 prices both (ETH at a DIFFERENT
    # number, so a wrong merge direction cannot pass by coincidence).
    oracle1 = RecordingOracle({ETH: Money(Decimal("1.00"), "USD")})
    oracle2 = RecordingOracle(
        {
            ETH: Money(Decimal("9.99"), "USD"),
            DAI: Money(Decimal("2.50"), "USD"),
        }
    )

    result = Inquirer([oracle1, oracle2]).usd_prices([ETH, DAI, ETH])

    assert result == {
        ETH: Money(Decimal("1.00"), "USD"),
        DAI: Money(Decimal("2.50"), "USD"),
    }
    # dedup preserved first occurrence: o1 saw [ETH, DAI] exactly once.
    assert oracle1.calls == [[ETH, DAI]]
    # o2 was asked for exactly the still-unpriced ids: [DAI].
    assert oracle2.calls == [[DAI]]


def test_dedup_preserves_first_occurrence_order():
    oracle = RecordingOracle({})
    Inquirer([oracle]).usd_prices([DAI, ETH, DAI, ETH])
    assert oracle.calls == [[DAI, ETH]]


def test_second_oracle_never_called_when_first_prices_everything():
    oracle1 = RecordingOracle(
        {
            ETH: Money(Decimal("1.00"), "USD"),
            DAI: Money(Decimal("2.50"), "USD"),
        }
    )
    oracle2 = RecordingOracle({ETH: Money(Decimal("9.99"), "USD")})

    result = Inquirer([oracle1, oracle2]).usd_prices([ETH, DAI])

    assert result == {
        ETH: Money(Decimal("1.00"), "USD"),
        DAI: Money(Decimal("2.50"), "USD"),
    }
    assert oracle2.calls == []


def test_remaining_oracles_skipped_once_everything_priced():
    oracle1 = RecordingOracle({ETH: Money(Decimal("1.00"), "USD")})
    oracle2 = RecordingOracle({DAI: Money(Decimal("2.50"), "USD")})
    oracle3 = RecordingOracle({ETH: Money(Decimal("7.77"), "USD")})

    result = Inquirer([oracle1, oracle2, oracle3]).usd_prices([ETH, DAI])

    assert result == {
        ETH: Money(Decimal("1.00"), "USD"),
        DAI: Money(Decimal("2.50"), "USD"),
    }
    assert oracle2.calls == [[DAI]]
    assert oracle3.calls == []


# ------------------------------------------------------- unpriced is silence


def test_no_oracles_means_empty_result_not_error():
    assert Inquirer([]).usd_prices([ETH]) == {}


def test_unpriced_ids_absent_from_result():
    oracle = RecordingOracle({ETH: Money(Decimal("1.00"), "USD")})
    result = Inquirer([oracle]).usd_prices([ETH, DAI])
    assert result == {ETH: Money(Decimal("1.00"), "USD")}
    assert DAI not in result


def test_empty_input_returns_empty_dict():
    oracle = RecordingOracle({ETH: Money(Decimal("1.00"), "USD")})
    assert Inquirer([oracle]).usd_prices([]) == {}


# --------------------------------------------------------------- validation


def test_invalid_caip19_raises_before_any_oracle_call():
    oracle = RecordingOracle({ETH: Money(Decimal("1.00"), "USD")})
    inquirer = Inquirer([oracle])
    with pytest.raises(CaipParseError):
        inquirer.usd_prices(["not-a-caip19"])
    assert oracle.calls == []


def test_invalid_caip19_amid_valid_ids_still_raises_before_any_call():
    oracle = RecordingOracle({ETH: Money(Decimal("1.00"), "USD")})
    inquirer = Inquirer([oracle])
    with pytest.raises(CaipParseError):
        inquirer.usd_prices([ETH, "eip155:1/erc20:0xdeadbeef"])
    assert oracle.calls == []


def test_non_string_id_raises_caip_parse_error_before_any_call():
    # delegation to auradefi.assets.caip: the parser rejects non-strings.
    oracle = RecordingOracle({ETH: Money(Decimal("1.00"), "USD")})
    inquirer = Inquirer([oracle])
    with pytest.raises(CaipParseError):
        inquirer.usd_prices([60])
    assert oracle.calls == []


# ----------------------------------------------------------- protocol seam


def test_conforming_stub_is_a_price_oracle_at_runtime():
    # runtime_checkable Protocol: structural conformance, no inheritance.
    assert isinstance(RecordingOracle({}), PriceOracle) is True


def test_non_conforming_object_is_not_a_price_oracle():
    assert not isinstance(object(), PriceOracle)


def test_inquirer_itself_conforms_to_the_oracle_protocol():
    # identical usd_prices method => inquirers compose as oracles.
    assert isinstance(Inquirer([]), PriceOracle)


@pytest.mark.parametrize("owner", [PriceOracle, Inquirer], ids=["protocol", "inquirer"])
def test_usd_prices_signature_is_self_caip19s(owner):
    parameters = list(inspect.signature(owner.usd_prices).parameters)
    assert parameters == ["self", "caip19s"]


# --------------------------------------------- structural independence gate


def _imported_names() -> list[str]:
    """Absolute dotted names imported by inquirer.py, relatives resolved."""
    tree = ast.parse(INQUIRER_SOURCE.read_text(encoding="utf-8"))
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


def test_inquirer_module_imports_no_httpx():
    offenders = [
        name
        for name in _imported_names()
        if name == "httpx" or name.startswith("httpx.")
    ]
    assert not offenders, f"inquirer.py performs no HTTP: {offenders}"


def test_inquirer_module_imports_no_oracle_module():
    banned = "auradefi.prices.oracles"
    offenders = [
        name
        for name in _imported_names()
        if name == banned or name.startswith(banned + ".")
    ]
    assert not offenders, (
        f"inquirer.py must not import any oracle module (structural seam): "
        f"{offenders}"
    )


# ------------------------------------------------------------- §5 #23 USD


def test_a_non_usd_quote_is_refused_and_names_the_oracle():
    # pins: the oracle contract this module documents ("every returned Money
    #       has currency USD") is ENFORCED, not merely stated. Unenforced, a
    #       EUR quote reached portfolio.holdings, which multiplied it by a
    #       quantity and stamped the product "USD": a total wrong by the FX
    #       rate, labelled as dollars, absent from `unpriced`, nothing
    #       raised. Oracles are host-supplied, so this boundary is the one
    #       place every composed oracle's output passes through.
    class EuroOracle:
        def usd_prices(self, caip19s):  # noqa: ANN001
            return {caip19s[0]: Money(Decimal("2000"), "EUR")}

    inquirer = Inquirer([EuroOracle()])

    with pytest.raises(CurrencyMismatchError) as excinfo:
        inquirer.usd_prices([ETH])

    assert "EuroOracle" in str(excinfo.value)
    assert "EUR" in str(excinfo.value)


def test_a_usd_quote_passes_through_untouched():
    # The control: the guard must not disturb a conforming oracle.
    class UsdOracle:
        def usd_prices(self, caip19s):  # noqa: ANN001
            return {caip19s[0]: Money(Decimal("2000"), "USD")}

    assert Inquirer([UsdOracle()]).usd_prices([ETH]) == {
        ETH: Money(Decimal("2000"), "USD")
    }


# ------------------------------------------ phase 12: the second protocol


def test_price_oracle_gains_no_second_member():
    # pins: `usd_prices` stays the WHOLE of PriceOracle. It is
    #       runtime_checkable, so a second member makes isinstance false for
    #       any object carrying only `usd_prices`, which silently unbinds
    #       every host oracle written against 0.1.x: the object stops being
    #       an oracle without one line of its code changing.
    assert "usd_prices_at" not in vars(PriceOracle)
    assert "unreachable_instant" not in vars(PriceOracle)
    assert isinstance(RecordingOracle({}), PriceOracle) is True


@pytest.mark.parametrize(
    "owner", [HistoricalPriceOracle, Inquirer], ids=["protocol", "inquirer"]
)
def test_usd_prices_at_signature_is_self_caip19s_at_ms(owner):
    # pins: the instant travels as its own parameter on its own method, so
    #       `usd_prices` is never threaded an `at_ms` and both owners stay
    #       callable by the same host code.
    parameters = list(inspect.signature(owner.usd_prices_at).parameters)
    assert parameters == ["self", "caip19s", "at_ms"]


def test_an_instant_oracle_conforms_and_a_legacy_one_does_not():
    # pins: HistoricalPriceOracle is structural and discriminating. A 0.1.x
    #       oracle is NOT one of these, which is the fact the skip rule and
    #       the stated absence are both built on.
    assert isinstance(InstantOracle({}), HistoricalPriceOracle) is True
    assert isinstance(RecordingOracle({}), HistoricalPriceOracle) is False


def test_inquirer_itself_conforms_to_the_historical_protocol():
    # pins: inquirers compose at instants too, so one can be handed to
    #       another as an oracle exactly as at the current instant.
    assert isinstance(Inquirer([]), HistoricalPriceOracle) is True


def test_the_module_contract_documents_both_optional_members():
    # pins: the two optional members are written down where an oracle author
    #       reads the contract. They are the only way a host learns that
    #       `unreachable_instant` returning None means "I can reach that
    #       instant", since no signature can say it.
    doc = inquirer_module.__doc__ or ""
    assert "usd_prices_at" in doc
    assert "unreachable_instant" in doc
    assert "optional" in doc.lower()


# ------------------------------------------- phase 12: first-wins at an instant


def test_first_wins_at_an_instant_dedups_and_narrows_the_second_ask():
    # pins: the instant walk is the same first-wins walk: dedup preserving
    #       first occurrence, construction order, each later oracle asked
    #       only for the still-unpriced ids, and `at_ms` passed through
    #       verbatim rather than rounded or dropped.
    oracle1 = InstantOracle({ETH: Money(Decimal("1.00"), "USD")})
    oracle2 = InstantOracle(
        {
            ETH: Money(Decimal("9.99"), "USD"),
            DAI: Money(Decimal("2.50"), "USD"),
        }
    )

    result = Inquirer([oracle1, oracle2]).usd_prices_at([ETH, DAI, ETH], PAST)

    assert result == {
        ETH: Money(Decimal("1.00"), "USD"),
        DAI: Money(Decimal("2.50"), "USD"),
    }
    assert oracle1.at_calls == [([ETH, DAI], PAST)]
    assert oracle2.at_calls == [([DAI], PAST)]


def test_remaining_oracles_are_skipped_once_everything_is_priced_at_an_instant():
    # pins: the walk stops early at an instant too, so the third oracle in a
    #       chain costs nothing once the first two have answered.
    oracle1 = InstantOracle({ETH: Money(Decimal("1.00"), "USD")})
    oracle2 = InstantOracle({DAI: Money(Decimal("2.50"), "USD")})
    oracle3 = InstantOracle({ETH: Money(Decimal("7.77"), "USD")})

    result = Inquirer([oracle1, oracle2, oracle3]).usd_prices_at([ETH, DAI], PAST)

    assert result == {
        ETH: Money(Decimal("1.00"), "USD"),
        DAI: Money(Decimal("2.50"), "USD"),
    }
    assert oracle2.at_calls == [([DAI], PAST)]
    assert oracle3.at_calls == []


def test_the_past_answer_is_never_the_current_one():
    # pins: `usd_prices_at` asks `usd_prices_at`. A fallback to `usd_prices`
    #       would answer a 2021 question with today's number and look
    #       perfectly healthy: same asset, same currency, wrong year.
    oracle = InstantOracle(
        {ETH: Money(Decimal("3584.17"), "USD")},
        at_prices={ETH: Money(Decimal("2949.68"), "USD")},
    )

    result = Inquirer([oracle]).usd_prices_at([ETH], PAST)

    assert result == {ETH: Money(Decimal("2949.68"), "USD")}
    assert result != {ETH: Money(Decimal("3584.17"), "USD")}
    assert oracle.calls == []
    assert oracle.at_calls == [([ETH], PAST)]


def test_unpriced_ids_are_absent_at_an_instant_and_not_an_error():
    # pins: an id nothing lists at that instant is silence, never a raise and
    #       never a zero Money (rule #8: incomplete data is declared).
    oracle = InstantOracle({ETH: Money(Decimal("1.00"), "USD")})
    result = Inquirer([oracle]).usd_prices_at([ETH, DAI], PAST)
    assert result == {ETH: Money(Decimal("1.00"), "USD")}
    assert DAI not in result


def test_an_empty_ask_at_an_instant_costs_no_oracle_call():
    # pins: the empty-collection contract for this door narrows to nothing
    #       and spends nothing (tests/style/
    #       test_an_empty_collection_argument_is_pinned.py). An empty ask
    #       that widened into "price everything you have" is the class that
    #       gate exists for.
    oracle = InstantOracle({ETH: Money(Decimal("1.00"), "USD")})
    assert Inquirer([oracle]).usd_prices_at([], PAST) == {}
    assert oracle.at_calls == []
    assert oracle.calls == []


def test_an_empty_chain_answers_at_an_instant_without_erroring():
    # pins: no oracles is an empty answer and an empty absence list, not a
    #       failure and not a fabricated mark.
    assert Inquirer([]).usd_prices_at([ETH], PAST) == {}
    assert Inquirer([]).absences_at(PAST) == ()


# --------------------------------------- phase 12: the two extra skip rules


def test_a_legacy_oracle_is_never_called_at_an_instant():
    # pins: an oracle carrying only `usd_prices` is skipped outright, so its
    #       current number cannot be served as a past mark. The fixture holds
    #       a price it WOULD have returned, so "never called" is proved by
    #       the value as well as by the empty call log.
    legacy = RecordingOracle({ETH: Money(Decimal("999.99"), "USD")})
    reachable = InstantOracle({ETH: Money(Decimal("2949.68"), "USD")})

    result = Inquirer([legacy, reachable]).usd_prices_at([ETH], PAST)

    assert result == {ETH: Money(Decimal("2949.68"), "USD")}
    assert legacy.calls == []
    assert reachable.at_calls == [([ETH], PAST)]


def test_a_legacy_oracle_names_itself_in_the_absences():
    # pins: the generated sentence, byte for byte. `historian.PriceMarks`
    #       shows it to a caller verbatim, so its wording is contract and not
    #       a debug string.
    legacy = RecordingOracle({ETH: Money(Decimal("999.99"), "USD")})
    assert Inquirer([legacy]).absences_at(PAST) == (LEGACY_NOTE,)


def test_an_oracle_that_cannot_reach_the_instant_is_not_called():
    # pins: `unreachable_instant` returning a string skips the oracle WITHOUT
    #       calling it, so an instant it cannot reach costs zero I/O. The
    #       fixture holds a price at ETH, so a call would change the answer.
    pinned = PinnedOracle(
        {ETH: Money(Decimal("5.55"), "USD")},
        pinned_at_ms=NEXT,
        reason="pinned elsewhere",
    )
    reachable = InstantOracle({ETH: Money(Decimal("2949.68"), "USD")})

    result = Inquirer([pinned, reachable]).usd_prices_at([ETH], PAST)

    assert result == {ETH: Money(Decimal("2949.68"), "USD")}
    assert pinned.at_calls == []
    assert pinned.calls == []


def test_an_oracle_that_can_reach_the_instant_is_called():
    # pins: the control on the rule above. `unreachable_instant` returning
    #       None means "I can reach that instant", so the same fixture at the
    #       instant it is pinned to must be asked and must win.
    pinned = PinnedOracle(
        {ETH: Money(Decimal("5.55"), "USD")},
        pinned_at_ms=NEXT,
        reason="pinned elsewhere",
    )
    reachable = InstantOracle({ETH: Money(Decimal("2949.68"), "USD")})

    result = Inquirer([pinned, reachable]).usd_prices_at([ETH], NEXT)

    assert result == {ETH: Money(Decimal("5.55"), "USD")}
    assert pinned.at_calls == [([ETH], NEXT)]
    assert reachable.at_calls == []


def test_a_stated_reason_is_reported_verbatim_and_only_when_it_applies():
    # pins: the oracle's own words reach `absences_at` unedited, and an
    #       oracle that CAN answer contributes nothing at all. One fixture,
    #       both instants, so a mutant that always reports or never reports
    #       fails one half of it.
    pinned = PinnedOracle(
        {ETH: Money(Decimal("5.55"), "USD")},
        pinned_at_ms=NEXT,
        reason="pinned elsewhere",
    )
    inquirer = Inquirer([pinned])
    assert inquirer.absences_at(PAST) == ("pinned elsewhere",)
    assert inquirer.absences_at(NEXT) == ()


def test_a_stated_reason_wins_over_the_generated_legacy_sentence():
    # pins: the generated sentence is the fallback, not the first answer. An
    #       oracle with no `usd_prices_at` that explains itself gets to keep
    #       its own explanation.
    speaking = SpeakingLegacyOracle({}, reason="manual marks are undated here")
    assert Inquirer([speaking]).absences_at(PAST) == (
        "manual marks are undated here",
    )


def test_absences_at_calls_nothing_on_any_oracle():
    # pins: `absences_at` is PURE. A caller asks it to decide whether a
    #       lookup is worth making, so an implementation that probed by
    #       trying the call would spend exactly the I/O it exists to avoid.
    legacy = RecordingOracle({ETH: Money(Decimal("999.99"), "USD")})
    pinned = PinnedOracle(
        {ETH: Money(Decimal("5.55"), "USD")},
        pinned_at_ms=NEXT,
        reason="pinned elsewhere",
    )
    reachable = InstantOracle({ETH: Money(Decimal("2949.68"), "USD")})

    Inquirer([legacy, pinned, reachable]).absences_at(PAST)

    assert legacy.calls == []
    assert pinned.calls == []
    assert pinned.at_calls == []
    assert reachable.calls == []
    assert reachable.at_calls == []


def test_absences_come_back_in_construction_order():
    # pins: construction order, which is precedence order, and nothing else.
    #       Sorted output or output grouped by kind would read as sensible
    #       and would misreport which oracle in a declared chain fell silent
    #       first. The same three oracles are built twice, in opposite
    #       orders, so a stable-but-wrong ordering cannot pass.
    def chain():
        return (
            RecordingOracle({ETH: Money(Decimal("999.99"), "USD")}),
            PinnedOracle(
                {ETH: Money(Decimal("5.55"), "USD")},
                pinned_at_ms=NEXT,
                reason="pinned elsewhere",
            ),
            InstantOracle({ETH: Money(Decimal("2949.68"), "USD")}),
        )

    legacy, pinned, reachable = chain()
    forwards = Inquirer([legacy, pinned, reachable]).absences_at(PAST)
    assert forwards == (LEGACY_NOTE, "pinned elsewhere")

    legacy, pinned, reachable = chain()
    backwards = Inquirer([reachable, pinned, legacy]).absences_at(PAST)
    assert backwards == ("pinned elsewhere", LEGACY_NOTE)


# ------------------------- phase 12: capability probing is callable(getattr)


def test_a_non_callable_usd_prices_at_is_not_a_capability():
    # pins: the probe is `callable(getattr(oracle, name, None))`. `hasattr`
    #       and a runtime_checkable `isinstance` both call this object
    #       historical, then the walk calls a string and dies with a
    #       TypeError outside the auradefi taxonomy.
    blunt = BluntOracle({ETH: Money(Decimal("999.99"), "USD")})
    reachable = InstantOracle({ETH: Money(Decimal("2949.68"), "USD")})

    result = Inquirer([blunt, reachable]).usd_prices_at([ETH], PAST)

    assert result == {ETH: Money(Decimal("2949.68"), "USD")}
    assert blunt.calls == []
    assert Inquirer([blunt]).absences_at(PAST) == (
        "BluntOracle has no usd_prices_at; it answers the current instant only",
    )


def test_a_wrapped_oracle_is_probed_by_method_not_by_static_lookup():
    # pins: an oracle reached through a `__getattr__` proxy still counts as
    #       historical. `inspect.getattr_static` is blind to the wrapper and
    #       a runtime_checkable `isinstance` uses it, so either probe would
    #       quietly demote a decorated oracle to a legacy one and report an
    #       absence for an oracle that could have answered.
    inner = InstantOracle({ETH: Money(Decimal("2949.68"), "USD")})
    proxy = ProxyOracle(inner)
    assert isinstance(proxy, HistoricalPriceOracle) is False

    result = Inquirer([proxy]).usd_prices_at([ETH], PAST)

    assert result == {ETH: Money(Decimal("2949.68"), "USD")}
    assert inner.at_calls == [([ETH], PAST)]
    assert Inquirer([proxy]).absences_at(PAST) == ()


# ------------------------------ phase 12: a chain nested inside a chain


def test_a_legacy_oracle_at_depth_two_still_names_itself():
    # pins: `absences_at` descends into a nested chain, so an oracle skipped
    #       at depth 2 is still named. An Inquirer carries a callable
    #       `usd_prices_at` and states no reason of its own, so without the
    #       descent the outer chain reads it as an oracle that can answer
    #       while the legacy oracle inside it is skipped unreported: an
    #       unpriced id with no note against it, which is the reading
    #       reserved for an asset no oracle in the chain lists at all.
    legacy = RecordingOracle({ETH: Money(Decimal("999.99"), "USD")})

    assert Inquirer([Inquirer([legacy])]).absences_at(PAST) == (LEGACY_NOTE,)
    assert legacy.calls == []

    # The same nesting around an oracle that CAN answer stays silent, so the
    # note above is the inner oracle's absence and not a remark about depth.
    reachable = InstantOracle({ETH: Money(Decimal("2949.68"), "USD")})
    assert Inquirer([Inquirer([reachable])]).absences_at(PAST) == ()
    assert reachable.calls == []
    assert reachable.at_calls == []


def test_a_nested_chains_absences_splice_in_at_its_own_position():
    # pins: a nested chain's strings arrive where that chain sits in the OUTER
    #       construction order, neither appended after the leaves nor sorted.
    #       Precedence order is the whole value of the list, since it says
    #       which oracle in a declared chain fell silent first. The same three
    #       oracles are built twice in opposite orders, so a flattened output
    #       that happens to come out sorted fails the second half.
    def chain():
        return (
            RecordingOracle({ETH: Money(Decimal("999.99"), "USD")}),
            PinnedOracle(
                {ETH: Money(Decimal("5.55"), "USD")},
                pinned_at_ms=NEXT,
                reason="pinned elsewhere",
            ),
            InstantOracle({ETH: Money(Decimal("2949.68"), "USD")}),
        )

    legacy, pinned, reachable = chain()
    forwards = Inquirer([legacy, Inquirer([pinned]), reachable]).absences_at(PAST)
    assert forwards == (LEGACY_NOTE, "pinned elsewhere")

    legacy, pinned, reachable = chain()
    backwards = Inquirer([reachable, Inquirer([pinned]), legacy]).absences_at(PAST)
    assert backwards == ("pinned elsewhere", LEGACY_NOTE)


def test_only_strings_splice_out_of_a_composite_oracles_absences():
    # pins: the descent reaches HOST-supplied surface, and only strings come
    #       back out of it. These reasons are shown to a caller verbatim, so a
    #       None or an int arriving from a third-party chain would be rendered
    #       to a user as a reason. `unreachable_instant` already filters the
    #       same way, and the two channels must not disagree.
    class ChattyChain:
        """A host oracle that is a chain in its own right, and untidy."""

        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.at_calls: list[tuple[list[str], int]] = []

        def usd_prices(self, caip19s):  # noqa: ANN001
            self.calls.append(list(caip19s))
            return {}

        def usd_prices_at(self, caip19s, at_ms):  # noqa: ANN001
            self.at_calls.append((list(caip19s), at_ms))
            return {}

        def absences_at(self, at_ms):  # noqa: ANN001
            return ("a real reason", None, 7, "another")

    chatty = ChattyChain()

    assert Inquirer([chatty]).absences_at(PAST) == ("a real reason", "another")
    assert chatty.calls == []
    assert chatty.at_calls == []


# ------------------------------- phase 12: currency and argument refusals


def test_a_non_usd_quote_at_an_instant_is_refused_and_names_the_oracle():
    # pins: the USD guard covers the instant path too. Unenforced there, a
    #       EUR mark from 2021 would be multiplied by a quantity and stamped
    #       "USD" in an accounting report, which is §5 #23 arriving through
    #       the door phase 12 opened.
    class EuroInstantOracle:
        def usd_prices(self, caip19s):  # noqa: ANN001
            return {}

        def usd_prices_at(self, caip19s, at_ms):  # noqa: ANN001
            return {caip19s[0]: Money(Decimal("2000"), "EUR")}

    inquirer = Inquirer([EuroInstantOracle()])

    with pytest.raises(CurrencyMismatchError) as excinfo:
        inquirer.usd_prices_at([ETH], PAST)

    assert "EuroInstantOracle" in str(excinfo.value)
    assert "EUR" in str(excinfo.value)


@pytest.mark.parametrize(
    "caip19s",
    [["not-a-caip19"], [60], [ETH, "eip155:1/erc20:0xdeadbeef"]],
    ids=["malformed", "non-string", "valid-then-malformed"],
)
def test_an_invalid_caip19_at_an_instant_raises_before_any_oracle_call(caip19s):
    # pins: every id is parsed up front, so a chain is never half-queried on
    #       an input that was never going to be answerable.
    oracle = InstantOracle({ETH: Money(Decimal("1.00"), "USD")})
    with pytest.raises(CaipParseError):
        Inquirer([oracle]).usd_prices_at(caip19s, PAST)
    assert oracle.at_calls == []
    assert oracle.calls == []


def test_a_malformed_id_is_refused_before_the_instant_is_checked():
    # pins: the order of the two guards. CaipParseError subclasses
    #       ValidationError, so a caller that catches the parse error
    #       specifically must still get it when both arguments are wrong.
    oracle = InstantOracle({ETH: Money(Decimal("1.00"), "USD")})
    with pytest.raises(CaipParseError):
        Inquirer([oracle]).usd_prices_at([60], "0")
    assert oracle.at_calls == []


@pytest.mark.parametrize(
    "at_ms",
    ["0", True, 1.0, None, 1_620_000_000_000.0],
    ids=["str", "bool", "float", "none", "float-instant"],
)
def test_a_non_integer_instant_is_refused_by_usd_prices_at(at_ms):
    # pins: `at_ms` is a millisecond-epoch INTEGER (rule #3), and bool is
    #       refused with it: True would otherwise pass as instant 1, which is
    #       1970 and prices nothing, silently.
    oracle = InstantOracle({ETH: Money(Decimal("1.00"), "USD")})
    with pytest.raises(ValidationError) as excinfo:
        Inquirer([oracle]).usd_prices_at([ETH], at_ms)
    assert excinfo.type is ValidationError
    assert "at_ms" in str(excinfo.value)
    assert oracle.at_calls == []


@pytest.mark.parametrize(
    "at_ms", ["0", True, 1.0, None], ids=["str", "bool", "float", "none"]
)
def test_a_non_integer_instant_is_refused_by_absences_at(at_ms):
    # pins: the pure method validates its one argument too. Unchecked, a
    #       string instant would flow into every oracle's
    #       `unreachable_instant` and each host implementation would fail
    #       differently, outside the auradefi taxonomy.
    pinned = PinnedOracle({}, pinned_at_ms=NEXT, reason="pinned elsewhere")
    with pytest.raises(ValidationError) as excinfo:
        Inquirer([pinned]).absences_at(at_ms)
    assert excinfo.type is ValidationError
    assert "at_ms" in str(excinfo.value)
