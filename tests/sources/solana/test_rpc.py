"""Contract tests for the Solana JSON-RPC source (SPEC §3.2/§3.3/§10).

All HTTP replays through committed cassettes (SPEC §13) or through
purpose-built ``httpx.MockTransport`` scripts for shapes no cassette
records. Golden records are hardcoded literals read off the cassette
bodies by hand.

CASSETTE ORDER IS THE WIRE CONTRACT. ``tests/cassettes/solana_balances.json``
records five POSTs to the SAME url, and the replay harness matches on
method + host + path + sorted query only — so all five share ONE key and
are served in recorded order:

    1  getBalance
    2  getTokenAccountsByOwner  programId=TOKEN_PROGRAM
    3  getTokenAccountsByOwner  programId=TOKEN_2022_PROGRAM
    4  getSignaturesForAddress  {"limit": 2}
    5  getSignaturesForAddress  {"limit": 2, "before": "SigErr2"}

Every test that touches that cassette therefore drives exactly that
sequence inside ONE ``Cassette`` instance. The harness repeats its final
recorded interaction forever, so a paging loop that failed to terminate
would silently re-read page 5 instead of erroring — which is why the
exactly-five-requests assertion below is the real guard on the stop rule.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import json
import sys
from pathlib import Path

import httpx
import pytest

from auradefi.errors import SourceError, ValidationError
from auradefi.money.quantity import Quantity
from auradefi.sources.solana import spl
from auradefi.sources.solana.rpc import (
    DEFAULT_URL,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    SignatureRecord,
    SolanaBalances,
    SolanaRpc,
)

ADDRESS = "9wFFyRfZBsuAha4YcuxcXLKwMxJR43S7fPfQLXMFxbAF"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
T22_MINT = "ScaLedUiAmountMint22222222222222222222222222"

NATIVE_CAIP19 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/slip44:501"
USDC_CAIP19 = f"solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/token:{USDC_MINT}"
T22_CAIP19 = f"solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/token:{T22_MINT}"

# Native 3.5 SOL; USDC is the SUM of two accounts (250 + 750); the
# Token-2022 mint carries a ScaledUiAmount multiplier of 2, so its
# displayed "2" diverges from raw/10^9 == "1".
GOLDEN_BALANCES = [
    spl.SolanaBalance(NATIVE_CAIP19, Quantity(3500000000, 9), None, "3.5", False),
    spl.SolanaBalance(USDC_CAIP19, Quantity(1000000000, 6), USDC_MINT, "1000", False),
    spl.SolanaBalance(T22_CAIP19, Quantity(1000000000, 9), T22_MINT, "2", True),
]

GOLDEN_SIGNATURES = [
    SignatureRecord("SigNewest1", 250000200, 1754000000, False),
    SignatureRecord("SigErr2", 250000100, 1753999000, True),
    SignatureRecord("SigLast3", 250000000, None, False),
]

JSON_PARSED = {"encoding": "jsonParsed"}

# The exact five request bodies the pinned sequence must put on the wire.
PINNED_BODIES = [
    {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [ADDRESS]},
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [ADDRESS, {"programId": TOKEN_PROGRAM}, JSON_PARSED],
    },
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [ADDRESS, {"programId": TOKEN_2022_PROGRAM}, JSON_PARSED],
    },
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [ADDRESS, {"limit": 2}],
    },
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [ADDRESS, {"limit": 2, "before": "SigErr2"}],
    },
]


def _recording_client(cas) -> tuple[httpx.Client, list[httpx.Request]]:
    """A cassette-backed client that also records every request issued."""
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


def _scripted_client(*responses: object) -> tuple[httpx.Client, list[httpx.Request]]:
    """A client replaying ``responses`` in order; the last one repeats.

    Each entry is a JSON-serialisable body, a ``(status, body)`` pair, or
    an exception INSTANCE to raise instead of responding.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        spec = responses[min(len(seen) - 1, len(responses) - 1)]
        if isinstance(spec, BaseException):
            raise spec
        status, body = spec if isinstance(spec, tuple) else (200, spec)
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def _ok(result: object) -> dict:
    """A well-formed JSON-RPC success envelope carrying ``result``."""
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _bodies(seen: list[httpx.Request]) -> list[dict]:
    return [json.loads(request.content) for request in seen]


def _pinned_via_service(cas):
    """Drive the recorded order: balances(ADDRESS), then get_signatures."""
    client, seen = _recording_client(cas)
    rpc = SolanaRpc(client)
    balances = SolanaBalances(rpc).balances(ADDRESS)
    signatures = rpc.get_signatures(ADDRESS, limit=2)
    return balances, signatures, seen


def _pinned_via_rpc(cas):
    """The SAME five requests, driven through the raw RPC methods.

    ``SolanaBalances.balances`` is exactly get_balance +
    get_token_accounts_by_owner, so this issues a byte-identical wire
    sequence — it just exposes the untyped intermediates.
    """
    client, seen = _recording_client(cas)
    rpc = SolanaRpc(client)
    lamports = rpc.get_balance(ADDRESS)
    rows = rpc.get_token_accounts_by_owner(ADDRESS)
    signatures = rpc.get_signatures(ADDRESS, limit=2)
    return lamports, rows, signatures, seen


class TestInterface:
    def test_rpc_constructor_takes_a_required_injected_client(self):
        params = inspect.signature(SolanaRpc.__init__).parameters
        assert list(params) == ["self", "client", "url"]
        assert params["client"].default is inspect.Parameter.empty
        assert params["url"].default == DEFAULT_URL

    def test_balances_service_constructor_takes_a_required_rpc(self):
        params = inspect.signature(SolanaBalances.__init__).parameters
        assert list(params) == ["self", "rpc"]
        assert params["rpc"].default is inspect.Parameter.empty

    def test_get_signatures_limit_defaults_to_one_thousand(self):
        params = inspect.signature(SolanaRpc.get_signatures).parameters
        assert list(params) == ["self", "address", "limit"]
        assert params["limit"].default == 1000

    def test_constants_are_the_pinned_endpoint_and_programs(self):
        assert DEFAULT_URL == "https://api.mainnet-beta.solana.com"
        assert TOKEN_PROGRAM == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
        assert TOKEN_2022_PROGRAM == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
        assert TOKEN_PROGRAM != TOKEN_2022_PROGRAM

    def test_signature_record_is_frozen_with_slots(self):
        record = SignatureRecord("Sig", 1, None, False)
        assert (record.signature, record.slot) == ("Sig", 1)
        assert record.block_time is None and record.failed is False
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.slot = 2  # type: ignore[misc]
        assert not hasattr(record, "__dict__")  # slots=True

    def test_signature_record_field_order_is_positional_contract(self):
        fields = [f.name for f in dataclasses.fields(SignatureRecord)]
        assert fields == ["signature", "slot", "block_time", "failed"]


class TestNoConstructionIO:
    def test_constructing_against_a_hostile_transport_issues_no_request(self):
        client, seen = _tripwire_client()
        rpc = SolanaRpc(client)
        service = SolanaBalances(rpc)
        assert isinstance(rpc, SolanaRpc) and isinstance(service, SolanaBalances)
        assert seen == []

    def test_custom_url_is_accepted_without_touching_it(self):
        client, seen = _tripwire_client()
        SolanaRpc(client, url="https://rpc.example.invalid/v1")
        assert seen == []


class TestPinnedCassette:
    def test_balances_are_the_three_golden_records_in_order(self, cassette):
        balances, _, _ = _pinned_via_service(cassette("solana_balances"))
        assert balances == GOLDEN_BALANCES
        assert [b.caip19 for b in balances] == [
            NATIVE_CAIP19,
            USDC_CAIP19,
            T22_CAIP19,
        ]

    def test_usdc_record_sums_two_token_accounts(self, cassette):
        balances, _, _ = _pinned_via_service(cassette("solana_balances"))
        usdc = balances[1]
        # 250000000 + 750000000, at the mint's shared 6 decimals.
        assert usdc.quantity == Quantity(1000000000, 6)
        assert usdc.ui_amount_string == "1000"
        assert usdc.scaled_ui is False

    def test_token_2022_breaks_the_raw_over_ten_to_the_decimals_identity(
        self, cassette
    ):
        balances, _, _ = _pinned_via_service(cassette("solana_balances"))
        t22 = balances[2]
        # THE POINT: the exact quantity says 1, the mint's scaled display
        # says 2. Anything that recomputed the display from raw would tie
        # these together and fail here.
        assert str(t22.quantity) == "1"
        assert t22.ui_amount_string == "2"
        assert t22.scaled_ui is True
        assert t22.quantity == Quantity(1000000000, 9)

    def test_native_record_is_lamports_at_nine_decimals(self, cassette):
        balances, _, _ = _pinned_via_service(cassette("solana_balances"))
        native = balances[0]
        assert native.mint is None
        assert native.quantity == Quantity(3500000000, 9)
        assert native.ui_amount_string == "3.5"
        assert native.scaled_ui is False

    def test_signatures_are_the_three_golden_records_in_received_order(
        self, cassette
    ):
        _, signatures, _ = _pinned_via_service(cassette("solana_balances"))
        assert signatures == GOLDEN_SIGNATURES

    def test_err_maps_to_failed_and_null_block_time_to_none(self, cassette):
        _, signatures, _ = _pinned_via_service(cassette("solana_balances"))
        assert [s.failed for s in signatures] == [False, True, False]
        assert [s.block_time for s in signatures] == [1754000000, 1753999000, None]
        # Seconds verbatim, NOT milliseconds: conversion is a decode concern.
        assert signatures[0].block_time == 1754000000

    def test_pinned_sequence_issues_exactly_five_posts_with_exact_bodies(
        self, cassette
    ):
        balances, signatures, seen = _pinned_via_service(cassette("solana_balances"))
        assert balances == GOLDEN_BALANCES
        assert signatures == GOLDEN_SIGNATURES

        assert len(seen) == 5, f"expected 5 POSTs, got {len(seen)}"
        for request in seen:
            assert request.method == "POST"
            assert request.url.host == "api.mainnet-beta.solana.com"

        assert _bodies(seen) == PINNED_BODIES

    def test_paging_stopped_because_the_last_page_was_short(self, cassette):
        _, signatures, seen = _pinned_via_service(cassette("solana_balances"))
        pages = [b for b in _bodies(seen) if b["method"] == "getSignaturesForAddress"]
        assert len(pages) == 2
        # Page 1 held 2 == limit, so a second page was fetched carrying
        # before=<last signature of page 1>. Page 2 held 1 < 2, so the loop
        # stopped. The harness would happily replay page 2 forever, so a
        # third request would show up here rather than erroring.
        assert pages[0]["params"][1] == {"limit": 2}
        assert pages[1]["params"][1] == {"limit": 2, "before": "SigErr2"}
        assert pages[1]["params"][1]["before"] == signatures[1].signature

    def test_token_program_then_token_2022_both_json_parsed(self, cassette):
        _, _, seen = _pinned_via_service(cassette("solana_balances"))
        token_calls = [
            b for b in _bodies(seen) if b["method"] == "getTokenAccountsByOwner"
        ]
        assert len(token_calls) == 2
        assert [call["params"][1]["programId"] for call in token_calls] == [
            TOKEN_PROGRAM,
            TOKEN_2022_PROGRAM,
        ]
        assert all(call["params"][2] == {"encoding": "jsonParsed"} for call in token_calls)


class TestRawRpcOverThePinnedCassette:
    def test_get_balance_returns_lamports_as_an_int(self, cassette):
        lamports, _, _, _ = _pinned_via_rpc(cassette("solana_balances"))
        assert lamports == 3500000000
        assert isinstance(lamports, int) and not isinstance(lamports, bool)

    def test_token_accounts_are_returned_concatenated_and_unparsed(self, cassette):
        _, rows, _, _ = _pinned_via_rpc(cassette("solana_balances"))
        assert isinstance(rows, list)
        # Two spl-token rows then one spl-token-2022 row: raw dicts, no
        # typed record in sight — parsing belongs to spl.py.
        assert [row["pubkey"] for row in rows] == ["UsdcAcctA1", "UsdcAcctB2", "T22AcctC3"]
        assert [row["account"]["data"]["program"] for row in rows] == [
            "spl-token",
            "spl-token",
            "spl-token-2022",
        ]
        assert all(isinstance(row, dict) for row in rows)

    def test_raw_methods_put_the_identical_five_requests_on_the_wire(self, cassette):
        _, _, signatures, seen = _pinned_via_rpc(cassette("solana_balances"))
        assert signatures == GOLDEN_SIGNATURES
        assert _bodies(seen) == PINNED_BODIES

    def test_unparsed_rows_feed_spl_to_the_same_golden_balances(self, cassette):
        lamports, rows, _, _ = _pinned_via_rpc(cassette("solana_balances"))
        assembled = spl.build_balances(
            lamports, spl.aggregate_by_mint(spl.parse_token_accounts(rows))
        )
        assert assembled == GOLDEN_BALANCES


class TestAddressValidationHappensBeforeHttp:
    @pytest.mark.parametrize(
        "address",
        [
            "bad",
            "not-base58-0OIl",  # hyphens plus the four excluded glyphs
            "",
            "0" * 44,  # '0' is not in the base58 alphabet
            ADDRESS + "x" * 20,  # too long
        ],
    )
    def test_balances_validates_before_any_request(self, address):
        client, seen = _tripwire_client()
        with pytest.raises(ValidationError):
            SolanaBalances(SolanaRpc(client)).balances(address)
        assert seen == []

    @pytest.mark.parametrize("address", ["bad", "not-base58-0OIl"])
    def test_get_signatures_validates_before_any_request(self, address):
        client, seen = _tripwire_client()
        with pytest.raises(ValidationError):
            SolanaRpc(client).get_signatures(address, limit=2)
        assert seen == []

    def test_get_balance_validates_before_any_request(self):
        client, seen = _tripwire_client()
        with pytest.raises(ValidationError):
            SolanaRpc(client).get_balance("not-base58-0OIl")
        assert seen == []

    def test_get_token_accounts_validates_before_any_request(self):
        client, seen = _tripwire_client()
        with pytest.raises(ValidationError):
            SolanaRpc(client).get_token_accounts_by_owner("not-base58-0OIl")
        assert seen == []


class TestErrorCassette:
    def test_four_recorded_failure_shapes_each_raise_source_error(self, cassette):
        # ONE cassette instance, four sequential calls: the recorded order
        # IS the contract — error member, HTTP 429, non-JSON, no result.
        rpc = SolanaRpc(cassette("solana_rpc_errors").client())

        with pytest.raises(SourceError) as rpc_error:
            rpc.get_balance(ADDRESS)
        assert "-32602" in str(rpc_error.value)
        assert "Invalid params: unable to parse pubkey" in str(rpc_error.value)

        with pytest.raises(SourceError) as http_error:
            rpc.get_balance(ADDRESS)
        assert "429" in str(http_error.value)

        with pytest.raises(SourceError):  # text/html body, not JSON
            rpc.get_balance(ADDRESS)

        with pytest.raises(SourceError):  # envelope without 'result'
            rpc.get_balance(ADDRESS)


class TestCallEnvelope:
    def test_transport_failure_becomes_source_error(self):
        client, _ = _scripted_client(httpx.ConnectError("dns exploded"))
        with pytest.raises(SourceError):
            SolanaRpc(client).get_balance(ADDRESS)

    def test_read_timeout_becomes_source_error(self):
        client, _ = _scripted_client(httpx.ReadTimeout("too slow"))
        with pytest.raises(SourceError):
            SolanaRpc(client).get_balance(ADDRESS)

    @pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
    def test_non_2xx_becomes_source_error(self, status):
        client, _ = _scripted_client((status, _ok({"value": 1})))
        with pytest.raises(SourceError):
            SolanaRpc(client).get_balance(ADDRESS)

    @pytest.mark.parametrize("body", ["<html>nope</html>", "", "not json at all"])
    def test_non_json_body_becomes_source_error(self, body):
        client, _ = _scripted_client((200, body))
        with pytest.raises(SourceError):
            SolanaRpc(client).get_balance(ADDRESS)

    @pytest.mark.parametrize("body", [[1, 2, 3], "null", 7])
    def test_non_object_body_becomes_source_error(self, body):
        client, _ = _scripted_client((200, body))
        with pytest.raises(SourceError):
            SolanaRpc(client).get_balance(ADDRESS)

    def test_error_member_embeds_code_and_message(self):
        client, _ = _scripted_client(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32001, "message": "Node is behind by 42 slots"},
            }
        )
        with pytest.raises(SourceError) as excinfo:
            SolanaRpc(client).get_balance(ADDRESS)
        assert "-32001" in str(excinfo.value)
        assert "Node is behind by 42 slots" in str(excinfo.value)

    def test_error_member_wins_even_when_a_result_is_present(self):
        client, _ = _scripted_client(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32000, "message": "partial"},
                "result": {"value": 5},
            }
        )
        with pytest.raises(SourceError):
            SolanaRpc(client).get_balance(ADDRESS)

    def test_missing_result_becomes_source_error(self):
        client, _ = _scripted_client({"jsonrpc": "2.0", "id": 1})
        with pytest.raises(SourceError):
            SolanaRpc(client).get_balance(ADDRESS)

    def test_the_request_body_is_exactly_the_json_rpc_envelope(self):
        client, seen = _scripted_client(_ok({"value": 0}))
        SolanaRpc(client).get_balance(ADDRESS)
        assert len(seen) == 1
        body = json.loads(seen[0].content)
        assert body == {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [ADDRESS],
        }
        assert list(body) == ["jsonrpc", "id", "method", "params"]

    def test_the_url_is_the_injected_one(self):
        client, seen = _scripted_client(_ok({"value": 0}))
        SolanaRpc(client, url="https://rpc.example.invalid/v1").get_balance(ADDRESS)
        assert str(seen[0].url) == "https://rpc.example.invalid/v1"


class TestGetBalanceResultShapes:
    def test_zero_lamports_is_a_valid_balance(self):
        client, _ = _scripted_client(_ok({"value": 0}))
        assert SolanaRpc(client).get_balance(ADDRESS) == 0

    def test_a_huge_lamport_count_survives_intact(self):
        huge = 10**30 + 1  # not representable as a float
        client, _ = _scripted_client(_ok({"value": huge}))
        assert SolanaRpc(client).get_balance(ADDRESS) == huge

    @pytest.mark.parametrize(
        "value",
        [
            True,  # bool is an int subclass; a balance is never a bool
            False,
            -1,
            3.5,  # a float amount is a defect (SPEC rules #1/#2)
            3500000000.0,
            "3500000000",  # getBalance reports a JSON integer, not a string
            None,
            [],
            {},
        ],
    )
    def test_malformed_value_becomes_source_error(self, value):
        client, _ = _scripted_client(_ok({"value": value}))
        with pytest.raises(SourceError):
            SolanaRpc(client).get_balance(ADDRESS)

    @pytest.mark.parametrize("result", [None, [], 5, "value", {"context": {}}])
    def test_result_without_a_value_member_becomes_source_error(self, result):
        client, _ = _scripted_client(_ok(result))
        with pytest.raises(SourceError):
            SolanaRpc(client).get_balance(ADDRESS)


class TestGetTokenAccountsResultShapes:
    def test_two_empty_pages_concatenate_to_an_empty_list(self):
        client, seen = _scripted_client(_ok({"value": []}))
        assert SolanaRpc(client).get_token_accounts_by_owner(ADDRESS) == []
        assert len(seen) == 2  # both programs are always asked

    def test_rows_concatenate_token_program_first(self):
        client, _ = _scripted_client(
            _ok({"value": [{"pubkey": "A"}]}),
            _ok({"value": [{"pubkey": "B"}, {"pubkey": "C"}]}),
        )
        rows = SolanaRpc(client).get_token_accounts_by_owner(ADDRESS)
        assert rows == [{"pubkey": "A"}, {"pubkey": "B"}, {"pubkey": "C"}]

    @pytest.mark.parametrize("result", [{"value": None}, {"value": {}}, {}, [], None])
    def test_a_result_without_a_value_list_becomes_source_error(self, result):
        client, _ = _scripted_client(_ok(result))
        with pytest.raises(SourceError):
            SolanaRpc(client).get_token_accounts_by_owner(ADDRESS)

    def test_a_bad_second_program_result_still_raises(self):
        client, _ = _scripted_client(_ok({"value": []}), _ok({"value": "nope"}))
        with pytest.raises(SourceError):
            SolanaRpc(client).get_token_accounts_by_owner(ADDRESS)


def _signature_row(signature: str, slot: int, block_time: int | None = 1, err=None):
    return {
        "signature": signature,
        "slot": slot,
        "blockTime": block_time,
        "err": err,
        "memo": None,
        "confirmationStatus": "finalized",
    }


class TestGetSignaturesPaging:
    def test_an_empty_first_page_stops_immediately(self):
        client, seen = _scripted_client(_ok([]))
        assert SolanaRpc(client).get_signatures(ADDRESS, limit=2) == []
        assert len(seen) == 1

    def test_a_short_first_page_stops_immediately(self):
        client, seen = _scripted_client(_ok([_signature_row("S1", 10)]))
        records = SolanaRpc(client).get_signatures(ADDRESS, limit=2)
        assert records == [SignatureRecord("S1", 10, 1, False)]
        assert len(seen) == 1

    def test_a_full_page_followed_by_an_empty_page_stops_after_two_requests(self):
        client, seen = _scripted_client(
            _ok([_signature_row("S1", 10), _signature_row("S2", 9)]),
            _ok([]),
        )
        records = SolanaRpc(client).get_signatures(ADDRESS, limit=2)
        assert [r.signature for r in records] == ["S1", "S2"]
        assert len(seen) == 2
        # A full page ALWAYS costs one more request: zero rows is a stop,
        # not an error.
        assert json.loads(seen[1].content)["params"][1] == {
            "limit": 2,
            "before": "S2",
        }

    def test_three_pages_chain_before_through_each_page_tail(self):
        client, seen = _scripted_client(
            _ok([_signature_row("S1", 10), _signature_row("S2", 9)]),
            _ok([_signature_row("S3", 8), _signature_row("S4", 7)]),
            _ok([_signature_row("S5", 6)]),
        )
        records = SolanaRpc(client).get_signatures(ADDRESS, limit=2)
        assert [r.signature for r in records] == ["S1", "S2", "S3", "S4", "S5"]
        assert len(seen) == 3
        befores = [
            json.loads(request.content)["params"][1].get("before") for request in seen
        ]
        assert befores == [None, "S2", "S4"]

    def test_the_first_page_carries_no_before_key_at_all(self):
        client, seen = _scripted_client(_ok([]))
        SolanaRpc(client).get_signatures(ADDRESS, limit=2)
        assert json.loads(seen[0].content)["params"] == [ADDRESS, {"limit": 2}]

    def test_the_default_limit_of_one_thousand_reaches_the_wire(self):
        client, seen = _scripted_client(_ok([]))
        SolanaRpc(client).get_signatures(ADDRESS)
        assert json.loads(seen[0].content)["params"] == [ADDRESS, {"limit": 1000}]


class TestGetSignaturesRowShapes:
    @pytest.mark.parametrize("result", [None, {}, {"value": []}, "rows", 5])
    def test_a_non_list_result_becomes_source_error(self, result):
        client, _ = _scripted_client(_ok(result))
        with pytest.raises(SourceError):
            SolanaRpc(client).get_signatures(ADDRESS, limit=2)

    @pytest.mark.parametrize(
        "row",
        [
            "not a dict",
            None,
            {"slot": 1, "blockTime": 1, "err": None},  # no signature
            {"signature": 5, "slot": 1, "blockTime": 1, "err": None},
            {"signature": None, "slot": 1, "blockTime": 1, "err": None},
            {"signature": "S", "blockTime": 1, "err": None},  # no slot
            {"signature": "S", "slot": "1", "blockTime": 1, "err": None},
            {"signature": "S", "slot": 1.5, "blockTime": 1, "err": None},
            {"signature": "S", "slot": True, "blockTime": 1, "err": None},
            {"signature": "S", "slot": None, "blockTime": 1, "err": None},
            {"signature": "S", "slot": 1, "blockTime": "1", "err": None},
            {"signature": "S", "slot": 1, "blockTime": 1.5, "err": None},
            {"signature": "S", "slot": 1, "blockTime": True, "err": None},
        ],
    )
    def test_a_malformed_row_becomes_source_error(self, row):
        client, _ = _scripted_client(_ok([row]))
        with pytest.raises(SourceError):
            SolanaRpc(client).get_signatures(ADDRESS, limit=2)

    @pytest.mark.parametrize(
        "err",
        [
            {"InstructionError": [0, "Custom"]},
            "AccountNotFound",
            0,  # any non-None err payload means the transaction failed
            [],
        ],
    )
    def test_any_non_null_err_marks_the_record_failed(self, err):
        client, _ = _scripted_client(_ok([_signature_row("S1", 10, 5, err)]))
        records = SolanaRpc(client).get_signatures(ADDRESS, limit=2)
        assert records == [SignatureRecord("S1", 10, 5, True)]

    def test_a_missing_err_key_is_treated_as_success(self):
        client, _ = _scripted_client(
            _ok([{"signature": "S1", "slot": 10, "blockTime": 5}])
        )
        records = SolanaRpc(client).get_signatures(ADDRESS, limit=2)
        assert records == [SignatureRecord("S1", 10, 5, False)]

    def test_a_missing_block_time_key_is_none(self):
        client, _ = _scripted_client(
            _ok([{"signature": "S1", "slot": 10, "err": None}])
        )
        records = SolanaRpc(client).get_signatures(ADDRESS, limit=2)
        assert records == [SignatureRecord("S1", 10, None, False)]

    def test_block_time_is_kept_in_upstream_seconds(self):
        # 1754000000 seconds, NOT 1754000000000 ms: the house ms rule is
        # applied downstream in decode, mirroring the Etherscan timeStamp pin.
        client, _ = _scripted_client(_ok([_signature_row("S1", 10, 1754000000)]))
        records = SolanaRpc(client).get_signatures(ADDRESS, limit=2)
        assert records[0].block_time == 1754000000

    def test_a_huge_slot_survives_as_an_exact_int(self):
        huge = 10**25 + 7
        client, _ = _scripted_client(_ok([_signature_row("S1", huge)]))
        records = SolanaRpc(client).get_signatures(ADDRESS, limit=2)
        assert records[0].slot == huge


class TestBase58CaseIsNeverLowered:
    def test_the_module_never_calls_lower(self):
        module = importlib.import_module("auradefi.sources.solana.rpc")
        text = Path(module.__file__).read_text(encoding="utf-8")
        # The EVM source lowercases hex; Solana base58 is case-SIGNIFICANT,
        # so no canonicalization may ever be applied here.
        assert ".lower()" not in text
        assert ".casefold()" not in text
        assert ".upper()" not in text

    def test_the_mixed_case_address_reaches_the_wire_verbatim(self, cassette):
        _, _, seen = _pinned_via_service(cassette("solana_balances"))
        assert ADDRESS != ADDRESS.lower()  # the fixture really is mixed case
        for body in _bodies(seen):
            assert body["params"][0] == ADDRESS

    def test_mint_case_survives_into_the_caip19(self, cassette):
        balances, _, _ = _pinned_via_service(cassette("solana_balances"))
        assert balances[1].mint == USDC_MINT
        assert balances[2].mint == T22_MINT
        assert balances[1].caip19.endswith(f"/token:{USDC_MINT}")
        assert balances[2].caip19.endswith(f"/token:{T22_MINT}")
        assert "ScaLedUiAmountMint" in balances[2].caip19


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
    name = "auradefi.sources.solana.rpc"
    saved = sys.modules.pop(name, None)
    try:
        # The autouse socket guard is active: a connect at import time fails.
        module = importlib.import_module(name)
    finally:
        if saved is not None:
            sys.modules[name] = saved
    assert hasattr(module, "SolanaRpc")
    assert hasattr(module, "SolanaBalances")
    assert hasattr(module, "SignatureRecord")

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module or "")
    domains = {dotted.split(".")[1] for dotted in imported if dotted.startswith("auradefi.")}
    assert not domains & FORBIDDEN_IMPORT_DOMAINS, (
        f"sources/ must not import {sorted(domains & FORBIDDEN_IMPORT_DOMAINS)}"
    )
