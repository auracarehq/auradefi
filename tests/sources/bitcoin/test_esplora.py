"""Contract tests for the Esplora client + gap-limit scanner (SPEC §3.2,
§10 Bitcoin row, §4.2 bip122; DECISIONS "Gap-limit scan" — PINNED).

HTTP behaviour replays through tests/cassettes/esplora_scan.json. Golden
values are hand-derived from the cassette body, never computed by the
code under test: balance = funded 5000 - spent 1000 = 4000 sats;
str(Quantity(4000, 8)) == "0.00004". The pinned gap=2 trace is
derive [(0,0,2), (0,2,2), (1,0,2)] with HTTP tb0x0, tb0x1, tb0x2,
tb1x0, tb1x1 — the cassette records nothing past those, so ANY
over-scan raises CassetteMissError. 2**53 + 1 does not survive a float
roundtrip, so the exact balance below kills float parsing of sats.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
from pathlib import Path

import httpx
import pytest

from auradefi.errors import SourceError, ValidationError
from auradefi.money.quantity import Quantity
from auradefi.sources.bitcoin.esplora import Esplora, scan
from auradefi.sources.bitcoin.utxo import (
    AddressBalance,
    AddressStats,
    Utxo,
    confirmed_sats,
    total_sats,
)

TXID_A = "a1" * 32
TXID_B = "b2" * 32
PINNED_CAIP19 = "bip122:000000000019d6689c085ae165831e93/slip44:0"
DEFAULT_BASE = "https://blockstream.info/api"
TEST_BASE = "https://esplora.test/api"
ZERO_MEMPOOL = {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 0}


def _golden_utxos() -> tuple[Utxo, Utxo]:
    """The two rows pinned at /address/tb0x0/utxo in the cassette."""
    return (
        Utxo(txid=TXID_A, vout=0, value_sats=4000, confirmed=True),
        Utxo(txid=TXID_B, vout=1, value_sats=250, confirmed=False),
    )


def _recording_client(cas) -> tuple[httpx.Client, list[httpx.Request]]:
    """A cassette-backed client that records every request issued."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return cas.handle(request)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def _tripwire_client() -> tuple[httpx.Client, list[httpx.Request]]:
    """A client that records and refuses every request — proves ZERO HTTP."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise RuntimeError("HTTP attempted where the contract forbids it")

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def _stats_client(
    stats: dict[str, tuple[int, int, int]],
    mempool: dict[str, dict] | None = None,
) -> tuple[httpx.Client, list[str]]:
    """Inline /address/{addr} transport: addr -> (funded, spent, tx_count).

    Unknown paths (over-scan, a /utxo leak) get a 404 the scanner cannot
    interpret as stats; the returned path list is the authoritative trail.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        address = request.url.path.removeprefix("/api/address/")
        if request.url.path.startswith("/api/address/") and address in stats:
            funded, spent, count = stats[address]
            return httpx.Response(
                200,
                json={
                    "chain_stats": {
                        "funded_txo_sum": funded,
                        "spent_txo_sum": spent,
                        "tx_count": count,
                    },
                    "mempool_stats": (mempool or {}).get(address, ZERO_MEMPOOL),
                },
            )
        return httpx.Response(404, text="not recorded (over-scan or /utxo leak)")

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def _json_client(payload) -> httpx.Client:
    """A client answering every request with one fixed 200 JSON body."""
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )


def _recording_derive(calls: list[tuple[int, int, int]]):
    """The acceptance stub: derive(c, s, n) -> ['tb{c}x{s}', ...] recorded."""

    def derive(chain: int, start: int, count: int) -> list[str]:
        calls.append((chain, start, count))
        return [f"tb{chain}x{index}" for index in range(start, start + count)]

    return derive


class TestInterface:
    def test_constructor_signature_client_required_keyless(self):
        params = inspect.signature(Esplora.__init__).parameters
        assert list(params) == ["self", "client", "base_url"]  # no api key, ever
        assert params["client"].default is inspect.Parameter.empty  # REQUIRED
        assert params["base_url"].default == DEFAULT_BASE

    def test_scan_signature_default_gap_is_20(self):
        params = inspect.signature(scan).parameters
        assert list(params) == ["esplora", "derive", "gap"]
        assert params["gap"].default == 20  # DECISIONS "Gap-limit scan"

    def test_construction_performs_no_io(self):
        client, seen = _tripwire_client()
        Esplora(client)
        Esplora(client, base_url=TEST_BASE)
        assert seen == []


class TestAddressStats:
    def test_golden_tb0x0_chain_stats_only(self, cassette):
        source = Esplora(cassette("esplora_scan").client())
        stats = source.address_stats("tb0x0")
        assert stats == AddressStats(5000, 1000, 2)
        # mempool never leaks: cassette mempool funded is 999, tx_count 1.
        assert stats.funded_txo_sum == 5000
        assert stats.tx_count == 2
        assert stats.confirmed_sats == 4000  # 5000 - 1000, hand-derived

    def test_all_zero_address(self, cassette):
        source = Esplora(cassette("esplora_scan").client())
        assert source.address_stats("tb0x1") == AddressStats(0, 0, 0)

    def test_request_shape_address_goes_straight_into_the_path(self, cassette):
        client, seen = _recording_client(cassette("esplora_scan"))
        Esplora(client).address_stats("tb0x0")
        assert len(seen) == 1
        assert seen[0].method == "GET"
        assert seen[0].url.host == "blockstream.info"
        assert seen[0].url.path == "/api/address/tb0x0"  # no syntax validation
        assert not seen[0].url.query

    def test_http_500_raises_source_error(self, cassette):
        source = Esplora(cassette("esplora_scan").client())
        with pytest.raises(SourceError):
            source.address_stats("err500")

    def test_non_json_body_raises_source_error(self, cassette):
        source = Esplora(cassette("esplora_scan").client())
        with pytest.raises(SourceError):
            source.address_stats("badjson")

    @pytest.mark.parametrize(
        "payload",
        [
            {"mempool_stats": ZERO_MEMPOOL},  # chain_stats missing entirely
            {"chain_stats": {"funded_txo_sum": 1, "spent_txo_sum": 0}},  # no tx_count
            {"chain_stats": {"spent_txo_sum": 0, "tx_count": 1}},  # no funded
            [],  # not an object at all
        ],
    )
    def test_missing_fields_raise_source_error(self, payload):
        source = Esplora(_json_client(payload), base_url=TEST_BASE)
        with pytest.raises(SourceError):
            source.address_stats("tb0x0")

    def test_transport_failure_raises_source_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        source = Esplora(
            httpx.Client(transport=httpx.MockTransport(handler)), base_url=TEST_BASE
        )
        with pytest.raises(SourceError):
            source.address_stats("tb0x0")


class TestUtxos:
    def test_golden_rows_in_response_order(self, cassette):
        source = Esplora(cassette("esplora_scan").client())
        rows = source.utxos("tb0x0")
        assert isinstance(rows, tuple)
        assert rows == _golden_utxos()  # order preserved: confirmed 4000 first
        assert confirmed_sats(rows) == 4000  # unconfirmed 250 excluded
        assert total_sats(rows) == 4250  # 4000 + 250

    def test_request_path_is_address_slash_utxo(self, cassette):
        client, seen = _recording_client(cassette("esplora_scan"))
        Esplora(client).utxos("tb0x0")
        assert [request.url.path for request in seen] == ["/api/address/tb0x0/utxo"]

    def test_empty_list_is_an_empty_tuple(self):
        source = Esplora(_json_client([]), base_url=TEST_BASE)
        assert source.utxos("tb0x0") == ()

    @pytest.mark.parametrize(
        "row",
        [
            {"vout": 0, "value": 1, "status": {"confirmed": True}},  # no txid
            {"txid": "a1" * 32, "value": 1, "status": {"confirmed": True}},  # no vout
            {"txid": "a1" * 32, "vout": 0, "status": {"confirmed": True}},  # no value
            {"txid": "a1" * 32, "vout": 0, "value": 1},  # no status
            {"txid": "a1" * 32, "vout": 0, "value": 1, "status": {}},  # no confirmed
            {"txid": "a1" * 32, "vout": 0, "value": -1, "status": {"confirmed": True}},
            "not-a-row",
        ],
    )
    def test_malformed_row_raises_source_error_strict(self, row):
        # UTXOs are money — strict, unlike etherscan's additive spam-skip.
        good = {"txid": "b2" * 32, "vout": 1, "value": 2, "status": {"confirmed": True}}
        source = Esplora(_json_client([good, row]), base_url=TEST_BASE)
        with pytest.raises(SourceError):
            source.utxos("tb0x0")

    def test_non_2xx_raises_source_error(self):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(500, text="oops"))
        )
        with pytest.raises(SourceError):
            Esplora(client, base_url=TEST_BASE).utxos("tb0x0")


class TestScanGolden:
    """The acceptance scenario: gap=2 over the pinned cassette."""

    def test_result_trace_and_totals(self, cassette):
        client, seen = _recording_client(cassette("esplora_scan"))
        calls: list[tuple[int, int, int]] = []
        result = scan(Esplora(client), _recording_derive(calls), gap=2)

        assert result.addresses == (AddressBalance("tb0x0", 0, 0, 4000, 2),)
        assert result.total_sats == 4000  # mempool 999 never leaks: 5000 - 1000
        assert result.total == Quantity(4000, 8)
        assert str(result.total) == "0.00004"  # exact BTC string, hand-derived
        assert result.caip19 == PINNED_CAIP19

        # Batch-of-gap derivation with the mid-batch stop, pinned exactly.
        assert calls == [(0, 0, 2), (0, 2, 2), (1, 0, 2)]

        # Ascending queries, chain 0 then 1; tb0x3 never queried (any
        # over-scan would have raised CassetteMissError); never /utxo.
        assert [request.url.path for request in seen] == [
            "/api/address/tb0x0",
            "/api/address/tb0x1",
            "/api/address/tb0x2",
            "/api/address/tb1x0",
            "/api/address/tb1x1",
        ]
        assert all(request.method == "GET" for request in seen)

    def test_gap_1_boundary(self, cassette):
        client, seen = _recording_client(cassette("esplora_scan"))
        calls: list[tuple[int, int, int]] = []
        result = scan(Esplora(client), _recording_derive(calls), gap=1)
        assert result.addresses == (AddressBalance("tb0x0", 0, 0, 4000, 2),)
        assert calls == [(0, 0, 1), (0, 1, 1), (1, 0, 1)]
        assert [request.url.path for request in seen] == [
            "/api/address/tb0x0",
            "/api/address/tb0x1",
            "/api/address/tb1x0",
        ]

    @pytest.mark.parametrize("gap", [0, -1])
    def test_gap_below_1_validation_error_with_zero_http(self, gap):
        client, seen = _tripwire_client()
        with pytest.raises(ValidationError):
            scan(Esplora(client), _recording_derive([]), gap=gap)
        assert seen == []  # BEFORE any HTTP, pinned


class TestScanSemantics:
    """DECISIONS pins exercised beyond the cassette scenario."""

    def test_run_resets_on_used_swept_kept_chain1_scanned(self):
        client, seen = _stats_client(
            {
                "tb0x0": (100, 0, 1),
                "tb0x1": (0, 0, 0),
                "tb0x2": (700, 700, 3),  # swept: balance 0, still used
                "tb0x3": (0, 0, 0),
                "tb0x4": (0, 0, 0),
                "tb1x0": (50, 0, 1),
                "tb1x1": (0, 0, 0),
                "tb1x2": (0, 0, 0),
            },
            # Mempool noise on an unused address: used-ness is
            # chain_stats.tx_count > 0, pinned — this must stay unused.
            mempool={"tb0x1": {"funded_txo_sum": 1234, "spent_txo_sum": 0, "tx_count": 5}},
        )
        calls: list[tuple[int, int, int]] = []
        result = scan(Esplora(client, base_url=TEST_BASE), _recording_derive(calls), gap=2)

        assert result.addresses == (
            AddressBalance("tb0x0", 0, 0, 100, 1),
            AddressBalance("tb0x2", 0, 2, 0, 3),  # balance-0 swept included
            AddressBalance("tb1x0", 1, 0, 50, 1),
        )
        assert result.total_sats == 150  # 100 + 0 + 50, hand-derived
        assert result.total == Quantity(150, 8)
        assert calls == [(0, 0, 2), (0, 2, 2), (0, 4, 2), (1, 0, 2), (1, 2, 2)]
        assert seen == [
            "/api/address/tb0x0",
            "/api/address/tb0x1",
            "/api/address/tb0x2",
            "/api/address/tb0x3",
            "/api/address/tb0x4",  # run hits 2 here: tb0x5 never queried
            "/api/address/tb1x0",
            "/api/address/tb1x1",
            "/api/address/tb1x2",  # run hits 2 here: tb1x3 never queried
        ]

    def test_balance_exact_above_2_53(self):
        # 2**53 + 1 does not survive float; this kills float sat parsing.
        client, _ = _stats_client(
            {"tb0x0": (2**53 + 1, 0, 1), "tb0x1": (0, 0, 0), "tb1x0": (0, 0, 0)}
        )
        result = scan(Esplora(client, base_url=TEST_BASE), _recording_derive([]), gap=1)
        assert result.addresses == (AddressBalance("tb0x0", 0, 0, 2**53 + 1, 1),)
        assert result.total_sats == 9007199254740993

    def test_nothing_used_is_an_empty_result(self):
        client, seen = _stats_client(
            {"tb0x0": (0, 0, 0), "tb0x1": (0, 0, 0), "tb1x0": (0, 0, 0), "tb1x1": (0, 0, 0)}
        )
        result = scan(Esplora(client, base_url=TEST_BASE), _recording_derive([]), gap=2)
        assert result.addresses == ()
        assert result.total_sats == 0
        assert result.total == Quantity(0, 8)
        assert len(seen) == 4  # one batch per chain, nothing more


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
    name = "auradefi.sources.bitcoin.esplora"
    saved = sys.modules.pop(name, None)
    try:
        # The autouse socket guard is active: a connect at import time fails.
        module = importlib.import_module(name)
    finally:
        if saved is not None:
            sys.modules[name] = saved
    assert hasattr(module, "Esplora")
    assert hasattr(module, "scan")

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module or "")
    domains = {
        dotted.split(".")[1] for dotted in imported if dotted.startswith("auradefi.")
    }
    assert not domains & FORBIDDEN_IMPORT_DOMAINS, (
        f"sources/ must not import {sorted(domains & FORBIDDEN_IMPORT_DOMAINS)}"
    )
