"""HoldingsService: the pinned assembly algorithm over stub seams.

Pins (work order + SPEC §11 Phase 1): one source call and EXACTLY ONE
prices call with caip19 ids in record order; priced records get
value = quantity.as_decimal() * price.amount EXACTLY (no rounding, no
context-precision loss); unpriced records keep price=None/value=None and
appear in report.unpriced; record order preserved; address/chain_id
echoed verbatim; clock injected (FrozenClock) or defaulted (SystemClock);
holdings.py imports no httpx. Portfolio is NOT an I/O domain.
"""

from __future__ import annotations

import ast
import inspect
import time
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import httpx

from auradefi.clock import FrozenClock
from auradefi.errors import CurrencyMismatchError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.portfolio.holdings import BalanceSource, HoldingsService
from auradefi.portfolio.models import HoldingsReport
from auradefi.sources.evm.etherscan import BalanceRecord, EtherscanV2

ETH = "eip155:1/slip44:60"
DAI = "eip155:1/erc20:0x6b175474e89094c44da98b954eedeac495271d0f"
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

HOLDINGS_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "auradefi"
    / "portfolio"
    / "holdings.py"
)


class StubSource:
    """Conforming BalanceSource that logs every call verbatim."""

    def __init__(self, records: Sequence[BalanceRecord]) -> None:
        self.records = list(records)
        self.calls: list[tuple[str, str]] = []

    def balances(self, chain_id: str, address: str) -> Sequence[BalanceRecord]:
        self.calls.append((chain_id, address))
        return list(self.records)


class RecordingPrices:
    """PriceOracle-shaped stub that logs every usd_prices request."""

    def __init__(self, prices: dict[str, Money]) -> None:
        self.prices = dict(prices)
        self.calls: list[list[str]] = []

    def usd_prices(self, caip19s: Sequence[str]) -> dict[str, Money]:
        self.calls.append(list(caip19s))
        return {c: self.prices[c] for c in caip19s if c in self.prices}


def _record(
    caip19: str, symbol: str | None, raw: int, decimals: int
) -> BalanceRecord:
    contract = caip19.split("erc20:")[1] if "erc20:" in caip19 else None
    return BalanceRecord(
        caip19=caip19, symbol=symbol, quantity=Quantity(raw, decimals),
        contract_address=contract,
    )


def _usd(text: str) -> Money:
    return Money(Decimal(text), "USD")


# ---------------------------------------------------------------- happy path


def test_priced_records_become_holdings_with_exact_usd_values():
    # 1.5 ETH * 2000.10 = 3000.15 ; 2000 DAI * 1.25 = 2500 ; total 5500.15
    source = StubSource(
        [
            _record(ETH, "ETH", 1_500_000_000_000_000_000, 18),
            _record(DAI, "DAI", 2000 * 10**18, 18),
        ]
    )
    prices = RecordingPrices({ETH: _usd("2000.10"), DAI: _usd("1.25")})
    service = HoldingsService(source, prices, clock=FrozenClock(1_754_000_000_000))

    report = service.holdings("eip155:1", "0xabc")

    assert isinstance(report, HoldingsReport)
    assert len(report.holdings) == 2
    eth, dai = report.holdings
    assert eth.caip19 == ETH
    assert eth.symbol == "ETH"
    assert eth.quantity == Quantity(1_500_000_000_000_000_000, 18)
    assert eth.price == _usd("2000.10")
    assert eth.value is not None
    assert eth.value.amount == Decimal("3000.15")
    assert eth.value.currency == "USD"
    assert dai.price == _usd("1.25")
    assert dai.value is not None
    assert dai.value.amount == Decimal("2500")
    assert report.total_value == _usd("5500.15")
    assert report.unpriced == ()


def test_value_multiplication_is_exact_past_decimal_context_precision():
    # (10**77 + 1) base units at 18 decimals, price 3 USD. Exact product:
    #   3 * (10**77 + 1) / 10**18
    #   = 3e59 + 3e-18
    # A default-context (28-digit) Decimal multiply rounds this to exactly
    # 3E+59 and silently loses the +3e-18: rule #1's named failure mode.
    exact = Decimal(
        "300000000000000000000000000000000000000000000000000000000000"
        ".000000000000000003"
    )
    source = StubSource([_record(ETH, "ETH", 10**77 + 1, 18)])
    prices = RecordingPrices({ETH: _usd("3")})
    service = HoldingsService(source, prices, clock=FrozenClock(1))

    report = service.holdings("eip155:1", "0xabc")

    holding = report.holdings[0]
    assert holding.value is not None
    assert holding.value.amount == exact
    assert holding.value.amount != Decimal("3E+59")
    # assemble's sum is exact too (money/_exact_sum). The total survives.
    assert report.total_value.amount == exact


# ------------------------------------------------------------------ unpriced


def test_unpriced_record_keeps_none_price_none_value_and_is_listed():
    source = StubSource(
        [
            _record(ETH, "ETH", 1_500_000_000_000_000_000, 18),
            _record(DAI, "DAI", 7 * 10**18, 18),
        ]
    )
    prices = RecordingPrices({ETH: _usd("2")})  # DAI deliberately absent
    service = HoldingsService(source, prices, clock=FrozenClock(1))

    report = service.holdings("eip155:1", "0xabc")

    eth, dai = report.holdings
    assert eth.value is not None
    assert eth.value.amount == Decimal("3")
    assert dai.price is None
    assert dai.value is None
    assert report.unpriced == (DAI,)
    assert report.total_value == _usd("3")


def test_empty_source_yields_zero_total_and_one_empty_prices_call():
    source = StubSource([])
    prices = RecordingPrices({})
    service = HoldingsService(source, prices, clock=FrozenClock(1))

    report = service.holdings("eip155:1", "0xabc")

    assert report.holdings == ()
    assert report.unpriced == ()
    assert report.total_value == Money(Decimal("0"), "USD")
    # the pinned algorithm is unconditional: usd_prices([]) exactly once.
    assert prices.calls == [[]]


# ------------------------------------------------------- call-shape pinning


def test_exactly_one_prices_call_with_ids_in_record_order():
    # deliberately NOT sorted order: a sorted or deduplicating
    # implementation cannot pass by coincidence.
    source = StubSource(
        [
            _record(USDC, "USDC", 5 * 10**6, 6),
            _record(ETH, "ETH", 10**18, 18),
            _record(DAI, "DAI", 10**18, 18),
        ]
    )
    prices = RecordingPrices(
        {USDC: _usd("1"), ETH: _usd("2"), DAI: _usd("3")}
    )
    service = HoldingsService(source, prices, clock=FrozenClock(1))

    service.holdings("eip155:1", "0xabc")

    assert prices.calls == [[USDC, ETH, DAI]]


def test_record_order_is_preserved_in_holdings():
    source = StubSource(
        [
            _record(USDC, "USDC", 5 * 10**6, 6),
            _record(ETH, "ETH", 10**18, 18),
            _record(DAI, "DAI", 10**18, 18),
        ]
    )
    prices = RecordingPrices({ETH: _usd("2")})
    service = HoldingsService(source, prices, clock=FrozenClock(1))

    report = service.holdings("eip155:1", "0xabc")

    assert tuple(h.caip19 for h in report.holdings) == (USDC, ETH, DAI)


def test_source_called_once_with_chain_and_address_verbatim():
    source = StubSource([])
    service = HoldingsService(source, RecordingPrices({}), clock=FrozenClock(1))

    service.holdings("eip155:8453", "0xD8dA6BF26964aF9D7eEd9e03E53415D37aA96045")

    assert source.calls == [
        ("eip155:8453", "0xD8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
    ]


# --------------------------------------------------------- clock + identity


def test_frozen_clock_stamps_as_of_ms_and_identity_echoed_verbatim():
    # mixed-case address: the service must NOT normalise: the SOURCE
    # lowercases for its own requests; the report echoes the input.
    address = "0xD8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    service = HoldingsService(
        StubSource([]), RecordingPrices({}), clock=FrozenClock(1_754_000_000_000)
    )

    report = service.holdings("eip155:1", address)

    assert report.as_of_ms == 1_754_000_000_000
    assert report.address == address
    assert report.chain_id == "eip155:1"


def test_clock_defaults_to_system_clock():
    service = HoldingsService(StubSource([]), RecordingPrices({}))

    before = time.time_ns() // 1_000_000
    report = service.holdings("eip155:1", "0xabc")
    after = time.time_ns() // 1_000_000

    assert isinstance(report.as_of_ms, int)
    assert before <= report.as_of_ms <= after


# ------------------------------------------------------------ protocol seam


def test_stub_source_conforms_to_balance_source_protocol():
    assert isinstance(StubSource([]), BalanceSource)


def test_non_conforming_object_is_not_a_balance_source():
    assert not isinstance(object(), BalanceSource)


def test_etherscan_v2_conforms_to_balance_source_protocol():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    assert isinstance(EtherscanV2(client, api_key=None), BalanceSource)


def test_init_signature_is_source_prices_optional_clock():
    parameters = inspect.signature(HoldingsService.__init__).parameters
    assert list(parameters) == ["self", "source", "prices", "clock"]
    assert parameters["clock"].default is None


def test_holdings_signature_is_chain_id_address():
    parameters = list(inspect.signature(HoldingsService.holdings).parameters)
    assert parameters == ["self", "chain_id", "address"]


# --------------------------------------------- transport-free assembly gate


def _imported_names() -> list[str]:
    """Absolute dotted names imported by holdings.py, relatives resolved."""
    tree = ast.parse(HOLDINGS_SOURCE.read_text(encoding="utf-8"))
    package = ["auradefi", "portfolio"]
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


def test_holdings_module_imports_no_httpx():
    offenders = [
        name
        for name in _imported_names()
        if name == "httpx" or name.startswith("httpx.")
    ]
    assert not offenders, (
        f"portfolio assembly is transport-free: no httpx: {offenders}"
    )


# --------------------------------------------------------- §5 #23 currency


def test_a_non_usd_price_is_never_relabelled_usd():
    # pins: the value carries the PRICE's currency, never a hardcoded "USD".
    #       Stamping Money(..., "USD") over a EUR price produced a total off
    #       by the FX rate, labelled USD, with nothing in `unpriced` and
    #       nothing raised. The caller cannot tell it is wrong, which is
    #       worse than a missing price. A EUR price is a host-oracle contract
    #       violation (Inquirer: "every returned Money has currency USD"),
    #       so the mislabelling must not be the thing that hides it.
    source = StubSource([_record(ETH, "ETH", 10**18, 18)])
    prices = RecordingPrices({ETH: Money(Decimal("2000"), "EUR")})
    service = HoldingsService(source, prices, FrozenClock(1_700_000_000_000))

    try:
        report = service.holdings("eip155:1", "0xabc")
    except CurrencyMismatchError:
        return  # refusing outright is also correct. It is not silent
    values = [holding.value for holding in report.holdings if holding.value]
    assert values, "the record was priced, so it must have a value"
    assert {value.currency for value in values} == {"EUR"}, (
        f"a EUR price was relabelled: {[(v.amount, v.currency) for v in values]}"
    )


def test_a_usd_price_still_produces_a_usd_value():
    # The control: carrying the currency through must not change the normal
    # path, where every price is already USD.
    source = StubSource([_record(ETH, "ETH", 10**18, 18)])
    prices = RecordingPrices({ETH: _usd("2000")})
    service = HoldingsService(source, prices, FrozenClock(1_700_000_000_000))

    report = service.holdings("eip155:1", "0xabc")

    assert report.holdings[0].value == _usd("2000")
    assert report.total_value == _usd("2000")
