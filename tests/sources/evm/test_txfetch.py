"""Contract tests for the Etherscan V2 txlist/tokentx fetchers (SPEC §10).

All HTTP replays through the committed ``etherscan_txlist`` cassette
(SPEC §13) or synthetic ``httpx.MockTransport`` handlers — never a
socket. Golden records are hardcoded literals derived BY HAND from the
cassette bodies: ``int("1000000000000000000") == 10**18``,
``int("10000000000") == 10**10``, ``int("25000000") == 25 * 10**6``.
The 78-nines amount (``int("9"*78) == 10**78 - 1``) does NOT survive an
``int(float(...))`` roundtrip, so its exact equality mechanically fails
any implementation that parses amounts through float (SPEC rules #1/#2).
Request URLs are pinned byte-for-byte because the param order is part of
the contract.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from pathlib import Path

import httpx
import pytest

from auradefi.errors import CassetteMissError, SourceError, ValidationError
from auradefi.sources.evm.txfetch import fetch_tokentx, fetch_txlist
from auradefi.sources.evm.txlist import NormalTxRecord, TokenTxRecord

ADDR = "0x1111111111111111111111111111111111111111"
ADDR_EMPTY = "0x2222222222222222222222222222222222222222"
ADDR_UNRECORDED = "0x5555555555555555555555555555555555555555"
FROM_EXTERNAL = "0x9999999999999999999999999999999999999999"
TO_PLAIN = "0x4444444444444444444444444444444444444444"
TO_TOKEN_RECIPIENT = "0x3333333333333333333333333333333333333333"
USDC_LOWER = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

HASH_AA = "0x" + "aa" * 32
HASH_BB = "0x" + "bb" * 32
HASH_CC = "0x" + "cc" * 32

BASE = "https://api.etherscan.io/v2/api"

TXLIST_GOLDEN = (
    NormalTxRecord(
        tx_hash=HASH_AA,
        block_number=100,
        time_stamp=1700000000,
        from_address=FROM_EXTERNAL,
        to_address=ADDR,
        value_wei=1000000000000000000,
        gas_used=21000,
        gas_price_wei=10000000000,
        is_error=False,
    ),
    NormalTxRecord(
        tx_hash=HASH_BB,
        block_number=101,
        time_stamp=1700000100,
        from_address=ADDR,
        to_address=USDC_LOWER,
        value_wei=0,
        gas_used=50000,
        gas_price_wei=10000000000,
        is_error=False,
    ),
    NormalTxRecord(
        tx_hash=HASH_CC,
        block_number=102,
        time_stamp=1700000200,
        from_address=ADDR,
        to_address=TO_PLAIN,
        value_wei=1000000000000000000,
        gas_used=120000,
        gas_price_wei=10000000000,
        is_error=False,
    ),
)

TOKENTX_GOLDEN = (
    TokenTxRecord(
        tx_hash=HASH_BB,
        block_number=101,
        time_stamp=1700000100,
        from_address=ADDR,
        to_address=TO_TOKEN_RECIPIENT,
        contract_address=USDC_LOWER,  # lowercased from the cassette's mixed case
        value_raw=25000000,
        token_decimal=6,
        token_symbol="USDC",
        gas_used=50000,
        gas_price_wei=10000000000,
    ),
)


def _query(action: str, address: str, page: int, offset: int) -> str:
    """The pinned wire-format query string — param order is contractual."""
    return (
        f"chainid=1&module=account&action={action}&address={address}"
        f"&startblock=0&endblock=99999999&page={page}&offset={offset}"
        f"&sort=asc&apikey=TESTKEY"
    )


def _recording_client(cas) -> tuple[httpx.Client, list[httpx.Request]]:
    """A cassette-backed client that also records every request issued."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return cas.handle(request)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def _synthetic_client(handler) -> httpx.Client:
    """A client whose every response comes from ``handler`` — no cassette."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _json_client(body: dict, status: int = 200) -> httpx.Client:
    return _synthetic_client(lambda request: httpx.Response(status, json=body))


NORMAL_ROW_9S = {
    "blockNumber": "103",
    "timeStamp": "1700000300",
    "hash": HASH_AA,
    "from": FROM_EXTERNAL,
    "to": ADDR,
    "value": "9" * 78,
    "gasUsed": "21000",
    "gasPrice": "10000000000",
    "isError": "0",
}


class TestInterface:
    @pytest.mark.parametrize("function", [fetch_txlist, fetch_tokentx])
    def test_signature_client_positional_rest_keyword_only(self, function):
        params = inspect.signature(function).parameters
        assert list(params) == ["client", "chain_id", "address", "api_key", "page_size"]
        assert params["client"].default is inspect.Parameter.empty  # injected, REQUIRED
        assert params["client"].kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        for name in ("chain_id", "address", "api_key", "page_size"):
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params["chain_id"].default is inspect.Parameter.empty
        assert params["address"].default is inspect.Parameter.empty
        assert params["api_key"].default is inspect.Parameter.empty
        assert params["page_size"].default == 1000

    @pytest.mark.parametrize("function", [fetch_txlist, fetch_tokentx])
    def test_positional_call_beyond_client_is_a_type_error(self, function):
        seen: list[httpx.Request] = []

        def tripwire(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            raise RuntimeError("HTTP attempted on a call that must not bind")

        with pytest.raises(TypeError):
            function(_synthetic_client(tripwire), 1, ADDR, "TESTKEY")
        assert seen == []


class TestTxlistPagination:
    def test_golden_records_across_two_pages(self, cassette):
        records = fetch_txlist(
            cassette("etherscan_txlist").client(),
            chain_id=1,
            address=ADDR,
            api_key="TESTKEY",
            page_size=2,
        )
        assert isinstance(records, tuple)
        assert records == TXLIST_GOLDEN
        assert [r.tx_hash for r in records] == [HASH_AA, HASH_BB, HASH_CC]
        assert [r.block_number for r in records] == [100, 101, 102]
        assert records[0].value_wei == 10**18

    def test_request_trail_is_page1_page2_and_stops_byte_for_byte(self, cassette):
        client, seen = _recording_client(cassette("etherscan_txlist"))
        records = fetch_txlist(
            client, chain_id=1, address=ADDR, api_key="TESTKEY", page_size=2
        )
        assert records == TXLIST_GOLDEN
        assert all(request.method == "GET" for request in seen)
        # A page-3 request would raise CassetteMissError: the test passes
        # only if pagination terminates on the short page (1 row < 2).
        assert [str(request.url) for request in seen] == [
            f"{BASE}?{_query('txlist', ADDR, page=1, offset=2)}",
            f"{BASE}?{_query('txlist', ADDR, page=2, offset=2)}",
        ]


class TestTokentx:
    def test_golden_single_record(self, cassette):
        records = fetch_tokentx(
            cassette("etherscan_txlist").client(),
            chain_id=1,
            address=ADDR,
            api_key="TESTKEY",
            page_size=2,
        )
        assert isinstance(records, tuple)
        assert records == TOKENTX_GOLDEN
        assert records[0].contract_address == USDC_LOWER  # lowercased on parse
        assert records[0].value_raw == 25000000
        assert records[0].token_decimal == 6

    def test_single_short_page_issues_exactly_one_request(self, cassette):
        client, seen = _recording_client(cassette("etherscan_txlist"))
        assert (
            fetch_tokentx(client, chain_id=1, address=ADDR, api_key="TESTKEY", page_size=2)
            == TOKENTX_GOLDEN
        )
        assert [str(request.url) for request in seen] == [
            f"{BASE}?{_query('tokentx', ADDR, page=1, offset=2)}",
        ]


class TestEmptyHistory:
    def test_txlist_no_transactions_found_returns_empty_tuple(self, cassette):
        records = fetch_txlist(
            cassette("etherscan_txlist").client(),
            chain_id=1,
            address=ADDR_EMPTY,
            api_key="TESTKEY",
            page_size=2,
        )
        assert records == ()

    def test_tokentx_no_transactions_found_returns_empty_tuple(self):
        client = _json_client(
            {"status": "0", "message": "No transactions found", "result": []}
        )
        assert fetch_tokentx(client, chain_id=1, address=ADDR, api_key="TESTKEY") == ()


class TestErrors:
    def test_unrecorded_request_raises_cassette_miss(self, cassette):
        with pytest.raises(CassetteMissError):
            fetch_txlist(
                cassette("etherscan_txlist").client(),
                chain_id=1,
                address=ADDR_UNRECORDED,
                api_key="TESTKEY",
                page_size=2,
            )

    @pytest.mark.parametrize("function", [fetch_txlist, fetch_tokentx])
    def test_rate_limit_message_raises_source_error_carrying_it(self, function):
        client = _json_client(
            {"status": "0", "message": "Max rate limit reached", "result": []}
        )
        with pytest.raises(SourceError, match="Max rate limit reached"):
            function(client, chain_id=1, address=ADDR, api_key="TESTKEY")

    def test_http_502_raises_source_error(self):
        client = _synthetic_client(
            lambda request: httpx.Response(502, text="Bad Gateway")
        )
        with pytest.raises(SourceError):
            fetch_txlist(client, chain_id=1, address=ADDR, api_key="TESTKEY")

    def test_non_json_body_raises_source_error(self):
        client = _synthetic_client(
            lambda request: httpx.Response(200, text="<html>not json</html>")
        )
        with pytest.raises(SourceError):
            fetch_txlist(client, chain_id=1, address=ADDR, api_key="TESTKEY")

    def test_malformed_row_propagates_the_txlist_parser_source_error(self):
        row = {**NORMAL_ROW_9S, "value": 5}  # JSON number: parser must reject
        client = _json_client({"status": "1", "message": "OK", "result": [row]})
        with pytest.raises(SourceError, match="key 'value'"):
            fetch_txlist(client, chain_id=1, address=ADDR, api_key="TESTKEY")

    @pytest.mark.parametrize("function", [fetch_txlist, fetch_tokentx])
    @pytest.mark.parametrize("page_size", [0, -1, -1000])
    def test_page_size_below_one_raises_validation_error_before_any_request(
        self, function, page_size
    ):
        # Termination depends on "a page shorter than page_size is the last
        # page"; with page_size <= 0 no page is ever shorter, so an unguarded
        # implementation would loop against the live endpoint forever. The
        # tripwire proves the guard fires BEFORE any request leaves.
        seen: list[httpx.Request] = []

        def tripwire(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            raise RuntimeError("HTTP attempted with page_size < 1")

        with pytest.raises(ValidationError):
            function(
                _synthetic_client(tripwire),
                chain_id=1,
                address=ADDR,
                api_key="TESTKEY",
                page_size=page_size,
            )
        assert seen == []

    def test_78_digit_value_survives_exactly_never_via_float(self):
        client = _json_client(
            {"status": "1", "message": "OK", "result": [NORMAL_ROW_9S]}
        )
        records = fetch_txlist(client, chain_id=1, address=ADDR, api_key="TESTKEY")
        assert len(records) == 1
        assert records[0].value_wei == 10**78 - 1


def _module_path() -> Path:
    import auradefi.sources.evm.txfetch as module

    return Path(module.__file__)


def _imports_of(path: Path) -> set[str]:
    """Absolute dotted names imported by the module, relatives resolved."""
    package = ["auradefi", "sources", "evm"]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join(anchor + ([node.module] if node.module else []))
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names if base)
    return found


class TestModuleHygiene:
    def test_no_httpx_client_is_constructed_inside_the_module(self):
        offenders = []
        for node in ast.walk(ast.parse(_module_path().read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else ""
            )
            if name == "Client":
                offenders.append(f"line {node.lineno}")
        assert not offenders, (
            "txfetch must use the INJECTED client, never construct one: "
            + ", ".join(offenders)
        )

    def test_never_imports_decode_and_never_reads_env(self):
        imported = _imports_of(_module_path())
        assert not any(name.startswith("auradefi.decode") for name in imported)
        assert not any(
            name == "os" or name.startswith("os.") or "dotenv" in name
            for name in imported
        ), "txfetch never reads the environment"

    def test_parsing_is_delegated_to_txlist(self):
        imported = _imports_of(_module_path())
        assert any(
            name.startswith("auradefi.sources.evm.txlist") for name in imported
        ), "txlist.py is the only parsing authority — txfetch must import it"

    def test_reimport_does_no_io(self):
        name = "auradefi.sources.evm.txfetch"
        saved = sys.modules.pop(name, None)
        try:
            # The autouse socket guard is active: a connect at import fails.
            module = importlib.import_module(name)
        finally:
            if saved is not None:
                sys.modules[name] = saved
        assert hasattr(module, "fetch_txlist")
        assert hasattr(module, "fetch_tokentx")
