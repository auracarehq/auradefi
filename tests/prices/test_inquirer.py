"""Inquirer + PriceOracle seam: first-wins USD aggregation (SPEC §3.3).

Behaviour pinned: deduplicate preserving first occurrence; oracles
queried in construction order; each subsequent oracle asked only for the
still-unpriced ids; remaining oracles skipped once everything is priced;
unpriced ids ABSENT from the result — never an error; syntactically
invalid CAIP-19 raises CaipParseError BEFORE any oracle call. The seam
is structural: inquirer.py imports no oracle module and no httpx.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from auradefi.errors import CaipParseError
from auradefi.money.fiat import Money
from auradefi.prices.inquirer import Inquirer, PriceOracle

ETH = "eip155:1/slip44:60"
DAI = "eip155:1/erc20:0x6b175474e89094c44da98b954eedeac495271d0f"

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
