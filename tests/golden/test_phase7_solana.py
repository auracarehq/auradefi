"""THE PHASE 7 GATE (SPEC §10 Solana row; SPEC §4.1 warning).

Wires the REAL ``SolanaRpc`` + ``SolanaBalances`` over one httpx client
replaying ``tests/cassettes/solana_balances.json`` and asserts hardcoded
golden numbers. A number changes -> this file goes red.

Solana is "the long pole" (SPEC §10) for two reasons this gate pins:

1. **Token-2022 ScaledUiAmount breaks ``raw / 10**decimals``.** The
   T22 account holds raw 1000000000 at 9 decimals, exactly 1 by the
   identity, while the node reports ``uiAmountString`` "2", because the
   mint carries a multiplier of 2. Both representations survive: the
   exact ``Quantity`` for arithmetic, the node's string for display, and
   ``scaled_ui`` True to say they diverge. An implementation that
   recomputed the display from raw would fail
   :func:`test_gate_token_2022_identity_break`.

2. **Token-2022 accounts live under a different program.** One
   ``getTokenAccountsByOwner`` cannot return both sets, so the balance
   path is pinned at two calls, TOKEN_PROGRAM then TOKEN_2022_PROGRAM.

Golden vectors, read off the cassette bodies by hand:

    native  3500000000 lamports @ 9  -> "3.5"     scaled False
    USDC     250000000 + 750000000   -> "1000"    scaled False  (SUM of
                                                   two token accounts)
    T22     1000000000 @ 9           -> "2"       scaled True   (str is "1")

CASSETTE ORDER IS THE WIRE CONTRACT. The replay harness matches on
method + host + path + sorted query only, so all five recorded POSTs share
ONE key and replay in recorded order. Each test therefore drives the same
pinned sequence inside its own ``Cassette`` instance: ``balances(ADDRESS)``
first (requests 1-3), then ``get_signatures(ADDRESS, limit=2)``
(requests 4-5). The harness repeats its LAST interaction forever, so a
paging loop that never terminated would silently re-read page 2 rather
than error: the exactly-five-POSTs assertion is what makes the stop rule
observable.
"""

from __future__ import annotations

import json

import httpx
import pytest

from auradefi.errors import SourceError, ValidationError
from auradefi.money.quantity import Quantity
from auradefi.sources.solana.rpc import (
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    SignatureRecord,
    SolanaBalances,
    SolanaRpc,
)
from auradefi.sources.solana.spl import SolanaBalance

ADDRESS = "9wFFyRfZBsuAha4YcuxcXLKwMxJR43S7fPfQLXMFxbAF"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
T22_MINT = "ScaLedUiAmountMint22222222222222222222222222"

SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
NATIVE_CAIP19 = f"{SOLANA}/slip44:501"

GOLDEN_BALANCES = [
    SolanaBalance(NATIVE_CAIP19, Quantity(3500000000, 9), None, "3.5", False),
    SolanaBalance(
        f"{SOLANA}/token:{USDC_MINT}", Quantity(1000000000, 6), USDC_MINT, "1000", False
    ),
    SolanaBalance(
        f"{SOLANA}/token:{T22_MINT}", Quantity(1000000000, 9), T22_MINT, "2", True
    ),
]

GOLDEN_SIGNATURES = [
    SignatureRecord("SigNewest1", 250000200, 1754000000, False),
    SignatureRecord("SigErr2", 250000100, 1753999000, True),
    SignatureRecord("SigLast3", 250000000, None, False),
]


def _pinned_run(cassette):
    """The whole Phase 7 stack over one cassette, in the recorded order."""
    seen: list[httpx.Request] = []
    cas = cassette("solana_balances")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return cas.handle(request)

    rpc = SolanaRpc(httpx.Client(transport=httpx.MockTransport(handler)))
    balances = SolanaBalances(rpc).balances(ADDRESS)
    signatures = rpc.get_signatures(ADDRESS, limit=2)
    return balances, signatures, seen


def test_gate_exactly_three_balances_in_order(cassette):
    balances, _, _ = _pinned_run(cassette)
    assert balances == GOLDEN_BALANCES


def test_gate_native_sol_golden_numbers(cassette):
    native = _pinned_run(cassette)[0][0]
    assert native.caip19 == "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/slip44:501"
    assert native.quantity == Quantity(3500000000, 9)
    assert native.ui_amount_string == "3.5"
    assert native.scaled_ui is False
    assert native.mint is None


def test_gate_usdc_sums_two_token_accounts(cassette):
    usdc = _pinned_run(cassette)[0][1]
    assert usdc.quantity == Quantity(1000000000, 6)  # 250000000 + 750000000
    assert usdc.ui_amount_string == "1000"
    assert usdc.scaled_ui is False
    assert usdc.mint == USDC_MINT


def test_gate_token_2022_identity_break(cassette):
    t22 = _pinned_run(cassette)[0][2]
    # raw/10^d says 1. The mint's ScaledUiAmount multiplier says 2. Both
    # are true, and both are carried: THIS is the Solana long pole.
    assert str(t22.quantity) == "1"
    assert t22.ui_amount_string == "2"
    assert t22.scaled_ui is True
    assert t22.quantity == Quantity(1000000000, 9)


def test_gate_signature_history_golden_records(cassette):
    _, signatures, _ = _pinned_run(cassette)
    assert signatures == GOLDEN_SIGNATURES
    # err -> failed True; a null blockTime -> None; seconds are verbatim.
    assert signatures[1].failed is True
    assert signatures[2].block_time is None


def test_gate_pinned_request_sequence_is_exactly_five_posts(cassette):
    balances, signatures, seen = _pinned_run(cassette)
    assert balances == GOLDEN_BALANCES
    assert signatures == GOLDEN_SIGNATURES

    bodies = [json.loads(request.content) for request in seen]
    assert len(bodies) == 5, f"expected 5 POSTs, got {len(bodies)}"
    assert [body["method"] for body in bodies] == [
        "getBalance",
        "getTokenAccountsByOwner",
        "getTokenAccountsByOwner",
        "getSignaturesForAddress",
        "getSignaturesForAddress",
    ]
    assert [body["params"][1]["programId"] for body in bodies[1:3]] == [
        TOKEN_PROGRAM,
        TOKEN_2022_PROGRAM,
    ]
    assert all(body["params"][2] == {"encoding": "jsonParsed"} for body in bodies[1:3])
    # Page 1 held 2 == limit so page 2 was fetched with before=<page 1 tail>;
    # page 2 held 1 < 2, so the loop stopped.
    assert bodies[3]["params"][1] == {"limit": 2}
    assert bodies[4]["params"][1] == {"limit": 2, "before": "SigErr2"}
    assert all(request.method == "POST" for request in seen)


@pytest.mark.parametrize("address", ["bad", "not-base58-0OIl"])
def test_gate_validation_error_before_any_http(address):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise RuntimeError("HTTP attempted before validation")

    rpc = SolanaRpc(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValidationError):
        SolanaBalances(rpc).balances(address)
    with pytest.raises(ValidationError):
        rpc.get_signatures(address, limit=2)
    assert seen == []


def test_gate_four_rpc_failure_shapes_raise_source_error(cassette):
    # solana_rpc_errors.json burns its own recorded order through four
    # sequential get_balance calls on ONE cassette instance.
    rpc = SolanaRpc(cassette("solana_rpc_errors").client())

    with pytest.raises(SourceError) as rpc_error:  # JSON-RPC error member
        rpc.get_balance(ADDRESS)
    assert "-32602" in str(rpc_error.value)
    assert "Invalid params: unable to parse pubkey" in str(rpc_error.value)

    with pytest.raises(SourceError) as http_error:  # HTTP 429
        rpc.get_balance(ADDRESS)
    assert "429" in str(http_error.value)

    with pytest.raises(SourceError):  # non-JSON text/html body
        rpc.get_balance(ADDRESS)

    with pytest.raises(SourceError):  # envelope with no 'result' member
        rpc.get_balance(ADDRESS)
