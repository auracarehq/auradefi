"""api/wire.py — the pure HTTP body projections.

Every assertion here is a wire-format contract: exact key sets, exact
strings, exact ordering. Nothing is mocked and nothing touches the
network — the whole point of keeping this module pure is that its output
contract is testable from fixtures alone (SPEC rule #11).

Golden values are derived from the pinned algorithms in docs/DECISIONS.md
and hardcoded as literals; they are never recomputed by calling the code
under test.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from auradefi.api.wire import (
    CAPABILITY_NAMES,
    batch_envelope,
    batch_error,
    batch_result,
    batch_warning,
    coverage_payload,
    holdings_wire,
    sync_envelope,
    transaction_wire,
)
from auradefi.chains.families import ChainFamily
from auradefi.chains.registry import Chain, ChainRegistry
from auradefi.errors import UnknownChainError, ValidationError
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.ledger.models import (
    Direction,
    Entry,
    LedgerTransaction,
    SyncEvent,
    SyncPage,
    transaction_id,
)
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

WIRE_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "auradefi" / "api" / "wire.py"
)

# --- golden literals (derived once, pinned here) ----------------------
# transaction_id = "txn_" + sha256(f"{chain}|{hash}|{account}")[:16]
HASH_A = "0x" + "ab" * 32
HASH_B = "0x" + "cd" * 32
TXN_A = "txn_bc0309930796dd5d"  # sha256("eip155:1|0xabab…|acct_eth")
TXN_B = "txn_998d1bcc8d81b34f"  # sha256("eip155:1|0xcdcd…|acct_eth")
ETH = "eip155:1/slip44:60"
USDC = "eip155:1/erc20:0x1111111111111111111111111111111111111111"

# A 78-digit raw — past 2**256 in digit count, far past Number.MAX_SAFE_INTEGER.
HUGE_RAW = int("9" * 78)
HUGE_NUMERIC = (
    "999999999999999999999999999999999999999999999999999999999999"
    ".999999999999999999"
)

# Raw-amount fields (rule #2): a JSON int or float in any of these is a defect.
_RAW_AMOUNT_KEYS = frozenset({"raw", "amount", "quantity_raw"})


def _rule_2_violations(node: object, path: str = "$") -> list[str]:
    """Every raw-amount field in ``node`` that is not a JSON string."""
    found: list[str] = []
    if isinstance(node, Mapping):
        for key, value in node.items():
            here = f"{path}.{key}"
            if (
                key in _RAW_AMOUNT_KEYS
                and not isinstance(value, str)
                and value is not None
            ):
                found.append(f"{here} = {value!r} ({type(value).__name__})")
            found.extend(_rule_2_violations(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_rule_2_violations(value, f"{path}[{index}]"))
    return found


def _roundtrip(body: object) -> object:
    """The body as it actually reaches a client: through real JSON."""
    return json.loads(json.dumps(body))


def _txn(
    *,
    txn_id: str,
    tx_hash: str,
    block_number: int | None,
    confirmed_at: int | None,
    entries: tuple[Entry, ...],
    initiated_at: int = 1_753_000_000_000,
) -> LedgerTransaction:
    return LedgerTransaction(
        id=txn_id,
        chain_id="eip155:1",
        tx_hash=tx_hash,
        account_id="acct_eth",
        block_number=block_number,
        initiated_at=initiated_at,
        confirmed_at=confirmed_at,
        entries=entries,
    )


@pytest.fixture
def txn_confirmed() -> LedgerTransaction:
    """A mined transaction with two entries of different scales."""
    return _txn(
        txn_id=TXN_A,
        tx_hash=HASH_A,
        block_number=21_000_000,
        confirmed_at=1_753_000_000_000,
        entries=(
            Entry(ETH, Quantity(1_500_000_000_000_000_000, 18), Direction.IN),
            Entry(USDC, Quantity(HUGE_RAW, 18), Direction.OUT),
        ),
    )


@pytest.fixture
def txn_pending() -> LedgerTransaction:
    """An unconfirmed transaction: block_number and confirmed_at are None."""
    return _txn(
        txn_id=TXN_B,
        tx_hash=HASH_B,
        block_number=None,
        confirmed_at=None,
        initiated_at=1_753_000_100_000,
        entries=(Entry(ETH, Quantity(0, 18), Direction.SELF),),
    )


@pytest.fixture
def reorg_page(txn_confirmed, txn_pending) -> SyncPage:
    """A real MemoryLedger page: two upserts, then the first removed.

    Seqs land 1 (A), 2 (B), 3 (A removed), so the ascending
    last-modified order of the page is B(ADDED) then A(REMOVED).
    """
    ledger = MemoryLedger()
    ledger.upsert("usr_1", [txn_confirmed, txn_pending])
    ledger.mark_removed("usr_1", [txn_confirmed.id])
    return ledger.sync("usr_1")


# --- transaction_wire -------------------------------------------------


def test_transaction_wire_has_exactly_eight_keys(txn_confirmed):
    wire = transaction_wire(txn_confirmed)
    assert sorted(wire) == [
        "account_id",
        "block_number",
        "chain_id",
        "confirmed_at_ms",
        "entries",
        "initiated_at_ms",
        "transaction_id",
        "tx_hash",
    ]


def test_transaction_wire_scalar_fields_are_verbatim(txn_confirmed):
    wire = transaction_wire(txn_confirmed)
    assert wire["transaction_id"] == TXN_A
    assert wire["account_id"] == "acct_eth"
    assert wire["chain_id"] == "eip155:1"
    assert wire["tx_hash"] == HASH_A
    assert wire["block_number"] == 21_000_000
    assert wire["initiated_at_ms"] == 1_753_000_000_000
    assert wire["confirmed_at_ms"] == 1_753_000_000_000


def test_transaction_wire_entries_keep_order_and_shape(txn_confirmed):
    entries = transaction_wire(txn_confirmed)["entries"]
    assert [e["asset_id"] for e in entries] == [ETH, USDC]
    assert [e["direction"] for e in entries] == ["in", "out"]
    assert all(sorted(e) == ["asset_id", "direction", "quantity"] for e in entries)


def test_entry_quantity_is_the_pinned_four_field_wire_dict(txn_confirmed):
    quantity = transaction_wire(txn_confirmed)["entries"][0]["quantity"]
    assert set(quantity) == {"raw", "decimals", "numeric", "float"}
    assert isinstance(quantity["raw"], str)
    assert quantity == {
        "raw": "1500000000000000000",
        "decimals": 18,
        "numeric": "1.5",
        "float": 1.5,
    }


def test_a_78_digit_raw_survives_exactly_with_no_scientific_notation(
    txn_confirmed,
):
    quantity = transaction_wire(txn_confirmed)["entries"][1]["quantity"]
    assert quantity["raw"] == "9" * 78
    assert quantity["numeric"] == HUGE_NUMERIC
    assert "E" not in quantity["numeric"] and "e" not in quantity["numeric"]
    assert len(quantity["raw"]) == 78


def test_none_block_and_confirmation_round_trip_as_json_null(txn_pending):
    raw_json = json.dumps(transaction_wire(txn_pending), sort_keys=True)
    body = json.loads(raw_json)
    assert "block_number" in body and body["block_number"] is None
    assert "confirmed_at_ms" in body and body["confirmed_at_ms"] is None
    assert '"block_number": null' in raw_json
    assert '"confirmed_at_ms": null' in raw_json


def test_zero_and_negative_raws_project_exactly():
    txn = _txn(
        txn_id=TXN_A,
        tx_hash=HASH_A,
        block_number=0,
        confirmed_at=0,
        entries=(
            Entry(ETH, Quantity(0, 18), Direction.SELF),
            Entry(USDC, Quantity(-HUGE_RAW, 6), Direction.OUT),
        ),
    )
    entries = transaction_wire(txn)["entries"]
    assert entries[0]["quantity"]["raw"] == "0"
    assert entries[0]["quantity"]["numeric"] == "0"
    assert entries[0]["direction"] == "self"
    assert entries[1]["quantity"]["raw"] == "-" + "9" * 78
    assert entries[1]["quantity"]["decimals"] == 6


def test_transaction_wire_never_emits_a_json_int_for_a_raw_amount(
    txn_confirmed,
):
    assert _rule_2_violations(_roundtrip(transaction_wire(txn_confirmed))) == []


# --- sync_envelope ----------------------------------------------------


def test_sync_envelope_has_exactly_the_five_plaid_keys(reorg_page):
    assert sorted(sync_envelope(reorg_page)) == [
        "added",
        "has_more",
        "modified",
        "next_cursor",
        "removed",
    ]


def test_sync_envelope_splits_added_and_removed_in_last_modified_order(
    reorg_page,
):
    body = sync_envelope(reorg_page)
    assert [item["transaction_id"] for item in body["added"]] == [TXN_B]
    assert body["removed"] == [
        {"transaction_id": TXN_A, "account_id": "acct_eth"}
    ]


def test_removed_entries_carry_exactly_two_keys(reorg_page):
    for item in sync_envelope(reorg_page)["removed"]:
        assert sorted(item) == ["account_id", "transaction_id"]


def test_added_entries_are_full_transaction_wires(reorg_page, txn_pending):
    added = sync_envelope(reorg_page)["added"]
    assert added == [transaction_wire(txn_pending)]


def test_sync_envelope_carries_cursor_and_has_more_verbatim(reorg_page):
    body = sync_envelope(reorg_page)
    assert body["next_cursor"] == "00000000000000000003"
    assert body["has_more"] is False


def test_modified_is_always_empty(reorg_page):
    assert sync_envelope(reorg_page)["modified"] == []


def test_modified_is_a_fresh_list_every_call(reorg_page):
    first = sync_envelope(reorg_page)["modified"]
    second = sync_envelope(reorg_page)["modified"]
    assert first is not second
    first.append("leak")
    assert sync_envelope(reorg_page)["modified"] == []


def test_sync_envelope_of_an_empty_page():
    body = sync_envelope(
        SyncPage(events=(), next_cursor="00000000000000000000", has_more=True)
    )
    assert body == {
        "added": [],
        "modified": [],
        "removed": [],
        "next_cursor": "00000000000000000000",
        "has_more": True,
    }


def test_sync_envelope_preserves_multi_event_ordering():
    ledger = MemoryLedger()
    txns = [
        _txn(
            txn_id=transaction_id("eip155:1", f"0x{index:064x}", "acct_eth"),
            tx_hash=f"0x{index:064x}",
            block_number=21_000_000 + index,
            confirmed_at=1_753_000_000_000 + index,
            entries=(Entry(ETH, Quantity(index, 18), Direction.IN),),
        )
        for index in range(4)
    ]
    ledger.upsert("usr_1", txns)
    ledger.mark_removed("usr_1", [txns[0].id, txns[2].id])
    body = sync_envelope(ledger.sync("usr_1"))
    # seqs: 1..4 for the upserts, 5 and 6 for the removals; the surviving
    # rows keep their original seqs, so added is [1, 3] and removed [0, 2].
    assert [item["transaction_id"] for item in body["added"]] == [
        txns[1].id,
        txns[3].id,
    ]
    assert [item["transaction_id"] for item in body["removed"]] == [
        txns[0].id,
        txns[2].id,
    ]


def test_sync_envelope_never_emits_a_json_int_for_a_raw_amount(reorg_page):
    assert _rule_2_violations(_roundtrip(sync_envelope(reorg_page))) == []


# --- coverage_payload -------------------------------------------------

BOUND = {"eip155:1": frozenset({"balances", "transactions", "prices"})}
SEED_ORDER = [
    "bip122:000000000019d6689c085ae165831e93",
    "eip155:1",
    "eip155:137",
    "eip155:8453",
    "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
]


def test_capability_names_is_the_pinned_five_tuple():
    assert CAPABILITY_NAMES == (
        "balances",
        "transactions",
        "positions",
        "prices",
        "xpub",
    )
    assert isinstance(CAPABILITY_NAMES, tuple)


def test_coverage_payload_top_level_shape():
    body = coverage_payload(ChainRegistry().chains(), BOUND, 1_754_000_000_000)
    assert sorted(body) == ["capabilities", "chains", "generated_at_ms"]
    assert body["generated_at_ms"] == 1_754_000_000_000
    assert body["capabilities"] == list(CAPABILITY_NAMES)
    assert isinstance(body["capabilities"], list)


def test_coverage_payload_orders_the_five_seed_chains_by_chain_id():
    body = coverage_payload(ChainRegistry().chains(), BOUND, 1_754_000_000_000)
    assert [row["chain_id"] for row in body["chains"]] == SEED_ORDER


def test_coverage_payload_sorts_even_when_the_input_is_shuffled():
    shuffled = tuple(reversed(ChainRegistry().chains()))
    body = coverage_payload(shuffled, BOUND, 1_754_000_000_000)
    assert [row["chain_id"] for row in body["chains"]] == SEED_ORDER


def test_the_bound_ethereum_row_is_exact():
    body = coverage_payload(ChainRegistry().chains(), BOUND, 1_754_000_000_000)
    row = next(r for r in body["chains"] if r["chain_id"] == "eip155:1")
    assert row == {
        "chain_id": "eip155:1",
        "name": "Ethereum",
        "family": "evm",
        "native_asset": "eip155:1/slip44:60",
        "native_symbol": "ETH",
        "native_decimals": 18,
        "capabilities": {
            "balances": True,
            "transactions": True,
            "positions": False,
            "prices": True,
            "xpub": False,
        },
    }


def test_the_unbound_bitcoin_row_reports_all_five_false():
    body = coverage_payload(ChainRegistry().chains(), BOUND, 1_754_000_000_000)
    row = next(
        r
        for r in body["chains"]
        if r["chain_id"] == "bip122:000000000019d6689c085ae165831e93"
    )
    assert row["family"] == "bitcoin"
    assert row["native_decimals"] == 8
    assert row["native_symbol"] == "BTC"
    assert row["native_asset"] == (
        "bip122:000000000019d6689c085ae165831e93/slip44:0"
    )
    assert row["capabilities"] == dict.fromkeys(CAPABILITY_NAMES, False)


def test_every_row_carries_exactly_the_five_capability_keys():
    body = coverage_payload(ChainRegistry().chains(), BOUND, 1_754_000_000_000)
    for row in body["chains"]:
        assert sorted(row["capabilities"]) == sorted(CAPABILITY_NAMES)
        assert all(
            isinstance(flag, bool) for flag in row["capabilities"].values()
        )
        assert sorted(row) == [
            "capabilities",
            "chain_id",
            "family",
            "name",
            "native_asset",
            "native_decimals",
            "native_symbol",
        ]


def test_no_bindings_reports_every_capability_false_for_every_chain():
    body = coverage_payload(ChainRegistry().chains(), {}, 1_754_000_000_000)
    for row in body["chains"]:
        assert row["capabilities"] == dict.fromkeys(CAPABILITY_NAMES, False)


def test_a_family_never_implies_a_capability_for_a_sibling_chain():
    """Base and Polygon are also 'evm'; only eip155:1 was bound.

    SPEC §12 risk 6: the matrix comes from live bindings, never from a
    hardcoded family table.
    """
    body = coverage_payload(ChainRegistry().chains(), BOUND, 1_754_000_000_000)
    for chain_id in ("eip155:137", "eip155:8453"):
        row = next(r for r in body["chains"] if r["chain_id"] == chain_id)
        assert row["family"] == "evm"
        assert row["capabilities"] == dict.fromkeys(CAPABILITY_NAMES, False)


def test_a_binding_outside_the_vocabulary_never_reaches_the_wire():
    body = coverage_payload(
        ChainRegistry().chains(),
        {"eip155:1": frozenset({"balances", "nfts", "mev"})},
        1_754_000_000_000,
    )
    row = next(r for r in body["chains"] if r["chain_id"] == "eip155:1")
    assert row["capabilities"] == {
        "balances": True,
        "transactions": False,
        "positions": False,
        "prices": False,
        "xpub": False,
    }


def test_a_string_binding_never_grants_a_capability_by_substring():
    """A ``str`` binding is NO binding — the invented-``True`` guard.

    ``"xpub" in "no xpub support here"`` is SUBSTRING membership, so a
    naive ``name in binding`` would report ``xpub: True`` for prose
    asserting the exact opposite. That is precisely the invented ``True``
    rule #10 and SPEC §12 risk 6 exist to prevent, and this string is
    chosen to contain FOUR of the five names as substrings.
    """
    prose = "no xpub support here: balances/transactions/prices only"
    assert "xpub" in prose and "balances" in prose  # the naive trap
    body = coverage_payload(
        ChainRegistry().chains(), {"eip155:1": prose}, 1_754_000_000_000
    )
    row = next(r for r in body["chains"] if r["chain_id"] == "eip155:1")
    assert row["capabilities"] == dict.fromkeys(CAPABILITY_NAMES, False)


@pytest.mark.parametrize(
    "binding",
    [
        pytest.param("xpub", id="str-exact-name"),
        pytest.param(b"xpub", id="bytes"),
        pytest.param(bytearray(b"xpub"), id="bytearray"),
        pytest.param(42, id="int-not-iterable"),
        pytest.param(True, id="bool-not-iterable"),
        pytest.param(object(), id="object-not-iterable"),
    ],
)
def test_a_malformed_binding_under_claims_all_five_instead_of_raising(binding):
    """A host typing mistake must under-claim, never 500 and never invent.

    Even ``"xpub"`` — the exact capability name as a bare string — grants
    nothing: only a *collection of names* can raise a flag.
    """
    body = coverage_payload(
        ChainRegistry().chains(), {"eip155:1": binding}, 1_754_000_000_000
    )
    row = next(r for r in body["chains"] if r["chain_id"] == "eip155:1")
    assert row["capabilities"] == dict.fromkeys(CAPABILITY_NAMES, False)


def test_a_non_string_member_is_dropped_and_the_rest_still_count():
    """One junk member does not poison the whole binding."""
    body = coverage_payload(
        ChainRegistry().chains(),
        {"eip155:1": {"balances", 7, None}},
        1_754_000_000_000,
    )
    row = next(r for r in body["chains"] if r["chain_id"] == "eip155:1")
    assert row["capabilities"] == {
        "balances": True,
        "transactions": False,
        "positions": False,
        "prices": False,
        "xpub": False,
    }


def test_a_list_binding_is_honoured_like_a_frozenset():
    """Any collection of names works — the guard rejects shape, not type."""
    body = coverage_payload(
        ChainRegistry().chains(),
        {"eip155:1": ["prices", "xpub"]},
        1_754_000_000_000,
    )
    row = next(r for r in body["chains"] if r["chain_id"] == "eip155:1")
    assert row["capabilities"] == {
        "balances": False,
        "transactions": False,
        "positions": False,
        "prices": True,
        "xpub": True,
    }


def test_a_binding_for_an_unregistered_chain_adds_no_row():
    body = coverage_payload(
        ChainRegistry().chains(),
        {"eip155:42161": frozenset({"balances"})},
        1_754_000_000_000,
    )
    assert [row["chain_id"] for row in body["chains"]] == SEED_ORDER


def test_a_host_registered_chain_appears_with_its_bound_flags():
    registry = ChainRegistry()
    registry.register(
        Chain(
            caip2="eip155:10",
            family=ChainFamily.EVM,
            name="Optimism",
            native_caip19="eip155:10/slip44:60",
            native_symbol="ETH",
            native_decimals=18,
        )
    )
    body = coverage_payload(
        registry.chains(),
        {"eip155:10": frozenset({"xpub"})},
        1_754_000_000_000,
    )
    assert [row["chain_id"] for row in body["chains"]] == [
        "bip122:000000000019d6689c085ae165831e93",
        "eip155:1",
        "eip155:10",
        "eip155:137",
        "eip155:8453",
        "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
    ]
    row = next(r for r in body["chains"] if r["chain_id"] == "eip155:10")
    assert row["capabilities"]["xpub"] is True
    assert row["capabilities"]["balances"] is False


def test_coverage_payload_serialises_the_family_as_a_plain_json_string():
    body = _roundtrip(
        coverage_payload(ChainRegistry().chains(), BOUND, 1_754_000_000_000)
    )
    assert {row["family"] for row in body["chains"]} == {
        "evm",
        "bitcoin",
        "solana",
    }


# --- batch envelope ---------------------------------------------------


def test_batch_envelope_has_exactly_two_keys():
    assert sorted(batch_envelope([], [])) == ["items", "warnings"]


def test_batch_result_is_tagged_and_carries_no_error_key():
    item = batch_result("eip155:1", "0xAbCd", {"total": "1"})
    assert item == {
        "status": "ok",
        "chain": "eip155:1",
        "address": "0xAbCd",
        "result": {"total": "1"},
    }
    assert "error" not in item


def test_batch_error_is_tagged_and_carries_no_result_key():
    item = batch_error("eip155:99", "0xdead", UnknownChainError("unknown chain"))
    assert item == {
        "status": "error",
        "chain": "eip155:99",
        "address": "0xdead",
        "error": {"type": "UnknownChainError", "message": "unknown chain"},
    }
    assert "result" not in item


def test_batch_error_renders_the_exception_type_and_message():
    rendered = batch_error(
        "eip155:99", "0xdead", UnknownChainError("unknown chain")
    )["error"]
    assert rendered == {"type": "UnknownChainError", "message": "unknown chain"}


def test_batch_error_echoes_a_mixed_case_address_verbatim():
    address = "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01"
    item = batch_error("eip155:1", address, ValueError("boom"))
    assert item["address"] == address
    assert item["error"] == {"type": "ValueError", "message": "boom"}


def test_batch_warning_has_exactly_four_keys_nulls_present():
    warning = batch_warning("partial_prices", "3 assets unpriced")
    assert warning == {
        "code": "partial_prices",
        "message": "3 assets unpriced",
        "chain": None,
        "address": None,
    }


def test_batch_warning_can_be_scoped_to_one_item():
    warning = batch_warning(
        "stale_price", "price older than 1h", "eip155:1", "0xAbCd"
    )
    assert warning["chain"] == "eip155:1"
    assert warning["address"] == "0xAbCd"


def test_batch_envelope_preserves_item_length_and_order():
    items = [
        batch_result("eip155:1", "0xa", {"n": 1}),
        batch_error("eip155:99", "0xb", UnknownChainError("unknown chain")),
        batch_result("eip155:137", "0xc", {"n": 2}),
    ]
    body = batch_envelope(items, [batch_warning("w", "m")])
    assert len(body["items"]) == 3
    assert [item["address"] for item in body["items"]] == ["0xa", "0xb", "0xc"]
    assert [item["status"] for item in body["items"]] == ["ok", "error", "ok"]
    assert body["warnings"] == [batch_warning("w", "m")]


def test_batch_envelope_defaults_warnings_to_an_empty_list():
    body = batch_envelope([batch_result("eip155:1", "0xa", None)])
    assert body["warnings"] == []


def test_batch_envelope_lists_are_fresh_not_the_caller_sequence():
    items = [batch_result("eip155:1", "0xa", None)]
    warnings = [batch_warning("w", "m")]
    body = batch_envelope(items, warnings)
    assert body["items"] is not items
    assert body["warnings"] is not warnings
    body["items"].append("leak")
    assert len(items) == 1


def test_batch_items_carry_mutually_exclusive_result_and_error_keys():
    items = [
        batch_result("eip155:1", "0xa", {"n": 1}),
        batch_error("eip155:99", "0xb", UnknownChainError("unknown chain")),
    ]
    for item in batch_envelope(items, [])["items"]:
        assert ("result" in item) != ("error" in item)
        assert item["status"] == ("ok" if "result" in item else "error")


# --- holdings_wire ----------------------------------------------------


@dataclass(frozen=True)
class _FakeHolding:
    """Duck-typed stand-in — api/ may not import auradefi.portfolio."""

    caip19: str
    symbol: str | None
    quantity: Quantity
    price: Money | None
    value: Money | None


@dataclass(frozen=True)
class _FakeReport:
    """Duck-typed HoldingsReport."""

    address: str
    chain_id: str
    holdings: tuple[_FakeHolding, ...]
    total_value: Money
    unpriced: tuple[str, ...]
    as_of_ms: int


PRICED = _FakeHolding(
    caip19=ETH,
    symbol="ETH",
    quantity=Quantity(1_500_000_000_000_000_000, 18),
    price=Money(Decimal("2500.123456789012345678"), "USD"),
    value=Money(Decimal("3750.185185183518518517"), "USD"),
)
UNPRICED = _FakeHolding(
    caip19=USDC,
    symbol=None,
    quantity=Quantity(HUGE_RAW, 18),
    price=None,
    value=None,
)


@pytest.fixture
def fake_report() -> _FakeReport:
    return _FakeReport(
        address="0xAbCd",
        chain_id="eip155:1",
        holdings=(PRICED, UNPRICED),
        total_value=Money(Decimal("3750.185185183518518517"), "USD"),
        unpriced=(USDC,),
        as_of_ms=1_754_000_000_000,
    )


def test_holdings_wire_top_level_shape(fake_report):
    body = holdings_wire(fake_report)
    assert sorted(body) == [
        "address",
        "as_of_ms",
        "chain_id",
        "holdings",
        "total_value",
        "unpriced",
    ]
    assert body["address"] == "0xAbCd"
    assert body["chain_id"] == "eip155:1"
    assert body["as_of_ms"] == 1_754_000_000_000


def test_holdings_wire_total_is_a_tagged_decimal_string(fake_report):
    assert holdings_wire(fake_report)["total_value"] == {
        "amount": "3750.185185183518518517",
        "currency": "USD",
    }


def test_holdings_wire_lists_the_unpriced_caip19(fake_report):
    body = holdings_wire(fake_report)
    assert body["unpriced"] == [USDC]
    assert isinstance(body["unpriced"], list)


def test_holdings_wire_priced_row_is_exact(fake_report):
    row = holdings_wire(fake_report)["holdings"][0]
    assert row == {
        "asset_id": ETH,
        "symbol": "ETH",
        "quantity": {
            "raw": "1500000000000000000",
            "decimals": 18,
            "numeric": "1.5",
            "float": 1.5,
        },
        "price": {"amount": "2500.123456789012345678", "currency": "USD"},
        "value": {"amount": "3750.185185183518518517", "currency": "USD"},
    }


def test_holdings_wire_unpriced_row_carries_explicit_nulls(fake_report):
    row = holdings_wire(fake_report)["holdings"][1]
    assert row["asset_id"] == USDC
    assert row["symbol"] is None
    assert "price" in row and row["price"] is None
    assert "value" in row and row["value"] is None
    assert row["quantity"]["raw"] == "9" * 78
    assert row["quantity"]["numeric"] == HUGE_NUMERIC


def test_holdings_wire_preserves_report_order(fake_report):
    reversed_report = _FakeReport(
        address=fake_report.address,
        chain_id=fake_report.chain_id,
        holdings=(UNPRICED, PRICED),
        total_value=fake_report.total_value,
        unpriced=fake_report.unpriced,
        as_of_ms=fake_report.as_of_ms,
    )
    assert [
        row["asset_id"] for row in holdings_wire(reversed_report)["holdings"]
    ] == [USDC, ETH]


def test_holdings_wire_never_emits_a_json_int_for_a_raw_amount(fake_report):
    assert _rule_2_violations(_roundtrip(holdings_wire(fake_report))) == []


def test_holdings_wire_accepts_a_real_holdings_report():
    """Duck typing must still fit the concrete portfolio.HoldingsReport."""
    from auradefi.portfolio.models import Holding, HoldingsReport

    report = HoldingsReport.assemble(
        "0xAbCd",
        "eip155:1",
        [
            Holding(ETH, "ETH", PRICED.quantity, PRICED.price, PRICED.value),
            Holding(USDC, None, UNPRICED.quantity, None, None),
        ],
        1_754_000_000_000,
    )
    body = holdings_wire(report)
    assert body["total_value"] == {
        "amount": "3750.185185183518518517",
        "currency": "USD",
    }
    assert body["unpriced"] == [USDC]
    assert [row["asset_id"] for row in body["holdings"]] == [ETH, USDC]


def test_holdings_wire_of_an_empty_report():
    body = holdings_wire(
        _FakeReport(
            address="0xAbCd",
            chain_id="eip155:1",
            holdings=(),
            total_value=Money(Decimal("0"), "USD"),
            unpriced=(),
            as_of_ms=1_754_000_000_000,
        )
    )
    assert body["holdings"] == []
    assert body["unpriced"] == []
    assert body["total_value"] == {"amount": "0", "currency": "USD"}


# --- purity, asserted mechanically ------------------------------------

BANNED_IMPORTS = ("fastapi", "starlette", "httpx", "auradefi.portfolio")


def _imported_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names if base)
    return names


def test_wire_imports_no_web_framework_no_http_client_no_portfolio():
    offenders = sorted(
        {
            name
            for name in _imported_names(WIRE_SOURCE)
            for banned in BANNED_IMPORTS
            if name == banned or name.startswith(f"{banned}.")
        }
    )
    assert not offenders, (
        "api/wire.py is PURE — it takes already-fetched domain objects and "
        f"returns plain dicts; banned imports found: {offenders}"
    )


def test_wire_imports_only_permitted_auradefi_domains():
    """money, ledger and chains only — the api row's pure subset."""
    domains = {
        name.split(".")[1]
        for name in _imported_names(WIRE_SOURCE)
        if name.startswith("auradefi.")
    }
    assert domains <= {"money", "ledger", "chains", "errors"}, (
        f"unexpected domain imports in api/wire.py: {sorted(domains)}"
    )


# --- Regression pins for the Phase 8 harsh-review findings -----------------
# Each of these failed before the fix; they exist so the failure cannot
# return silently. See docs/DECISIONS.md and the Phase 8 review notes.


def test_mapping_binding_honours_its_values_never_just_its_keys():
    """A Mapping's VALUES carry the verdict — keys alone invert a deny.

    Feeding this endpoint's own output shape back into Deps.capabilities
    is the most plausible host mistake there is, and reading it as a bare
    key collection reported all five True, manufacturing capabilities the
    deployment does not have (rule #10, SPEC §12 risk 6).
    """
    denied = dict.fromkeys(CAPABILITY_NAMES, False)
    payload = coverage_payload(ChainRegistry().chains(), {"eip155:1": denied}, 0)
    row = next(c for c in payload["chains"] if c["chain_id"] == "eip155:1")
    assert row["capabilities"] == denied

    mixed = {"balances": True, "transactions": True, "positions": False,
             "prices": True, "xpub": False}
    payload = coverage_payload(ChainRegistry().chains(), {"eip155:1": mixed}, 0)
    row = next(c for c in payload["chains"] if c["chain_id"] == "eip155:1")
    assert row["capabilities"] == mixed


def test_a_foreign_backends_string_kind_is_refused_not_projected_as_added(
    txn_confirmed,
):
    """A plain 'removed' string must never be guessed into ``added``.

    SyncEventKind is a StrEnum and SyncEvent is unvalidated, so a
    third-party LedgerPort backend rebuilding kind from a text column
    yields a string that is equal-but-not-identical. Projecting that
    deletion as an add would leave the client holding a transaction the
    ledger dropped — silently wrong numbers.
    """
    txn = txn_confirmed
    page = SyncPage(
        events=(SyncEvent(kind="removed", transaction=txn),),
        next_cursor="00000000000000000001",
        has_more=False,
    )
    envelope = sync_envelope(page)
    assert envelope["removed"] == [
        {"transaction_id": txn.id, "account_id": txn.account_id}
    ]
    assert envelope["added"] == []

    with pytest.raises(ValidationError):
        sync_envelope(
            SyncPage(
                events=(SyncEvent(kind="sideways", transaction=txn),),
                next_cursor="00000000000000000001",
                has_more=False,
            )
        )
