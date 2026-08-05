"""Contract tests for the Etherscan V2 balance source (SPEC §3.2/§3.3/§10).

All HTTP behaviour replays through committed cassettes (SPEC §13). Golden
records are hardcoded literals derived by hand from the cassette bodies.
The large raw amounts (4878123456789012345678 and
255000000000000000000000) do NOT survive an ``int(float(...))``
roundtrip, so the exact equalities below mechanically fail any
implementation that parses amounts through float (SPEC rules #1/#2).
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import sys
from pathlib import Path

import httpx
import pytest

from auradefi.errors import CassetteMissError, SourceError, ValidationError
from auradefi.money.quantity import Quantity
from auradefi.sources.evm.etherscan import BalanceRecord, EtherscanV2

ADDR_PAGED = "0x1111111111111111111111111111111111111111"
ADDR_RATE_LIMITED = "0x2222222222222222222222222222222222222222"
ADDR_NATIVE_ONLY = "0x3333333333333333333333333333333333333333"
ADDR_BAD_GATEWAY = "0x4444444444444444444444444444444444444444"
VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
VITALIK_MIXED_CASE = "0xD8Da6BF26964aF9D7eEd9e03E53415D37aA96045"

AAA = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BBB = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CCC = "0xcccccccccccccccccccccccccccccccccccccccc"
DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
SPAM = "0xdeaddeaddeaddeaddeaddeaddeaddeaddeaddead"

NATIVE_CAIP19 = "eip155:1/slip44:60"

PAGED_GOLDEN = [
    BalanceRecord(
        caip19=NATIVE_CAIP19,
        symbol="ETH",
        quantity=Quantity(2000000000000000000, 18),
        contract_address=None,
    ),
    BalanceRecord(
        caip19=f"eip155:1/erc20:{AAA}",
        symbol="AAA",
        quantity=Quantity(5000000000000000000, 18),
        contract_address=AAA,
    ),
    BalanceRecord(
        caip19=f"eip155:1/erc20:{BBB}",
        symbol="BBB",
        quantity=Quantity(12345678, 6),
        contract_address=BBB,
    ),
]

VITALIK_GOLDEN = [
    BalanceRecord(
        caip19=NATIVE_CAIP19,
        symbol="ETH",
        quantity=Quantity(4878123456789012345678, 18),
        contract_address=None,
    ),
    BalanceRecord(
        caip19=f"eip155:1/erc20:{DAI}",
        symbol="DAI",
        quantity=Quantity(255000000000000000000000, 18),
        contract_address=DAI,
    ),
    BalanceRecord(
        caip19=f"eip155:1/erc20:{USDC}",
        symbol="USDC",
        quantity=Quantity(1250000750000, 6),
        contract_address=USDC,
    ),
]


def _recording_client(cas) -> tuple[httpx.Client, list[httpx.Request]]:
    """A cassette-backed client that also records every request issued."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return cas.handle(request)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def _tripwire_client() -> tuple[httpx.Client, list[httpx.Request]]:
    """A client that records and refuses every request: proves ZERO HTTP."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise RuntimeError("HTTP attempted where the contract forbids it")

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


class TestInterface:
    def test_constructor_signature_client_required_and_defaults(self):
        params = inspect.signature(EtherscanV2.__init__).parameters
        assert list(params) == ["self", "client", "api_key", "base_url", "page_size"]
        assert params["client"].default is inspect.Parameter.empty  # injected, REQUIRED
        assert params["api_key"].default is None
        assert params["base_url"].default == "https://api.etherscan.io/v2/api"
        assert params["page_size"].default == 1000

    def test_balance_record_is_frozen_with_slots(self):
        record = BalanceRecord(
            caip19=NATIVE_CAIP19,
            symbol="ETH",
            quantity=Quantity(1, 18),
            contract_address=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.symbol = "MUT"  # type: ignore[misc]
        assert not hasattr(record, "__dict__")  # slots=True


class TestPagedCassette:
    def test_returns_exactly_three_records_in_order(self, cassette):
        source = EtherscanV2(
            cassette("etherscan_paged").client(), api_key="TESTKEY", page_size=2
        )
        records = source.balances("eip155:1", ADDR_PAGED)
        assert isinstance(records, list)
        assert records == PAGED_GOLDEN
        # CCC (tokenbalance result "0") is omitted; nothing zero survives.
        assert all(record.quantity.raw != 0 for record in records)

    def test_request_sequence_paginates_and_stops_after_short_page(self, cassette):
        client, seen = _recording_client(cassette("etherscan_paged"))
        source = EtherscanV2(client, api_key="TESTKEY", page_size=2)
        assert source.balances("eip155:1", ADDR_PAGED) == PAGED_GOLDEN

        for request in seen:
            assert request.method == "GET"
            assert request.url.host == "api.etherscan.io"
            assert request.url.path == "/v2/api"
            assert request.url.params["chainid"] == "1"
            assert request.url.params["apikey"] == "TESTKEY"
            assert request.url.params["address"] == ADDR_PAGED

        trail = [
            (
                request.url.params["action"],
                request.url.params.get("page"),
                request.url.params.get("contractaddress"),
            )
            for request in seen
        ]
        # A page-4 tokentx request would raise CassetteMissError: the test
        # passes only if the loop terminates on the short page (1 row < 2).
        assert trail == [
            ("balance", None, None),
            ("tokentx", "1", None),
            ("tokentx", "2", None),
            ("tokentx", "3", None),
            ("tokenbalance", None, AAA),
            ("tokenbalance", None, BBB),
            ("tokenbalance", None, CCC),
        ]

        tokentx = [r for r in seen if r.url.params["action"] == "tokentx"]
        assert all(r.url.params["offset"] == "2" for r in tokentx)
        assert all(r.url.params["sort"] == "asc" for r in tokentx)
        assert all(r.url.params["startblock"] == "0" for r in tokentx)
        assert all(r.url.params["endblock"] == "99999999" for r in tokentx)

    def test_rate_limited_discovery_raises_source_error(self, cassette):
        source = EtherscanV2(
            cassette("etherscan_paged").client(), api_key="TESTKEY", page_size=2
        )
        with pytest.raises(SourceError):
            source.balances("eip155:1", ADDR_RATE_LIMITED)

    def test_no_transactions_found_is_empty_discovery_not_an_error(self, cassette):
        source = EtherscanV2(
            cassette("etherscan_paged").client(), api_key="TESTKEY", page_size=2
        )
        records = source.balances("eip155:1", ADDR_NATIVE_ONLY)
        assert records == [
            BalanceRecord(
                caip19=NATIVE_CAIP19,
                symbol="ETH",
                quantity=Quantity(7000000000000000000, 18),
                contract_address=None,
            )
        ]

    def test_http_502_raises_source_error(self, cassette):
        source = EtherscanV2(
            cassette("etherscan_paged").client(), api_key="TESTKEY", page_size=2
        )
        with pytest.raises(SourceError):
            source.balances("eip155:1", ADDR_BAD_GATEWAY)

    def test_api_key_none_omits_the_apikey_param(self, cassette):
        client, seen = _recording_client(cassette("etherscan_paged"))
        source = EtherscanV2(client, api_key=None, page_size=2)
        # The cassette records every URL WITH apikey=TESTKEY, so a keyless
        # request cannot match; the point of this test is the request shape.
        with pytest.raises((CassetteMissError, SourceError)):
            source.balances("eip155:1", ADDR_PAGED)
        assert seen, "no request was issued at all"
        assert "apikey" not in seen[0].url.params


class TestChainIdValidation:
    @pytest.mark.parametrize(
        "chain_id",
        [
            "bip122:000000000019d6689c085ae165831e93",  # wrong namespace
            "eip155:notanumber",
            "eip155:0x89",  # hex is not base-10
            "eip155:",  # empty reference
            "eip155:1_000",  # int() accepts this; base-10 does not
            "eip155:-1",  # int() accepts this; a chain reference is unsigned
            "EIP155:1",  # CAIP-2 namespaces are lowercase; must be exactly 'eip155'
            "1",  # no namespace at all
            "",
        ],
    )
    def test_invalid_chain_id_raises_validation_error_with_zero_http(self, chain_id):
        client, seen = _tripwire_client()
        source = EtherscanV2(client, api_key="TESTKEY")
        with pytest.raises(ValidationError):
            source.balances(chain_id, ADDR_PAGED)
        assert seen == []


class TestVitalikCassette:
    def test_golden_records(self, cassette):
        source = EtherscanV2(cassette("phase1_vitalik").client(), api_key="TESTKEY")
        assert source.balances("eip155:1", VITALIK) == VITALIK_GOLDEN

    def test_discovery_dedupes_sorts_and_skips_spam(self, cassette):
        client, seen = _recording_client(cassette("phase1_vitalik"))
        source = EtherscanV2(client, api_key="TESTKEY")
        assert source.balances("eip155:1", VITALIK) == VITALIK_GOLDEN

        pages = [
            r.url.params["page"] for r in seen if r.url.params["action"] == "tokentx"
        ]
        assert pages == ["1"]  # 4 rows < default page_size 1000: loop stops
        assert all(
            r.url.params["offset"] == "1000"
            for r in seen
            if r.url.params["action"] == "tokentx"
        )

        queried = [
            r.url.params["contractaddress"]
            for r in seen
            if r.url.params["action"] == "tokenbalance"
        ]
        # Mixed-case USDC discovery row deduplicates against the lowercase
        # one; the spam row (tokenDecimal "") is skipped additively: its
        # contract is never queried; DAI sorts (and is requested) before
        # USDC. The cassette records nothing for the spam contract, so a
        # query for it would also raise CassetteMissError.
        assert queried == [DAI, USDC]
        assert SPAM not in queried

    def test_mixed_case_input_address_is_lowercased_everywhere(self, cassette):
        client, seen = _recording_client(cassette("phase1_vitalik"))
        source = EtherscanV2(client, api_key="TESTKEY")
        records = source.balances("eip155:1", VITALIK_MIXED_CASE)
        assert records == VITALIK_GOLDEN  # lowercased caip19s + contract_address
        assert seen, "no request was issued at all"
        assert all(request.url.params["address"] == VITALIK for request in seen)


FORBIDDEN_IMPORT_DOMAINS = {
    "accounting",
    "api",
    "decode",
    "jobs",
    "ledger",
    "portfolio",
    "positions",
    "prices",
    "project",
    "tenancy",
    "testing",
    "webhooks",
}


def test_reimport_does_no_io_and_module_stays_in_its_layer():
    name = "auradefi.sources.evm.etherscan"
    saved = sys.modules.pop(name, None)
    try:
        # The autouse socket guard is active: a connect at import time fails.
        module = importlib.import_module(name)
    finally:
        if saved is not None:
            sys.modules[name] = saved
    assert hasattr(module, "EtherscanV2")
    assert hasattr(module, "BalanceRecord")

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module or "")
    domains = {
        dotted.split(".")[1]
        for dotted in imported
        if dotted.startswith("auradefi.")
    }
    assert not domains & FORBIDDEN_IMPORT_DOMAINS, (
        f"sources/ must not import {sorted(domains & FORBIDDEN_IMPORT_DOMAINS)}"
    )
