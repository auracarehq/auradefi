"""Contract tests for Etherscan V2 txlist/tokentx typed raw records.

Parse only — the sources half of SPEC §3.3 ("raw chain bytes -> typed
records"). Golden literals are hardcoded and derived by hand:
``int("1000000000000000000") == 10**18``, ``int("10000000000") ==
10**10``, ``int("9"*78) == 10**78 - 1``. The 78-nines amount does NOT
survive an ``int(float(...))`` roundtrip, so its exact equality
mechanically fails any implementation that parses amounts through float
(SPEC rules #1/#2). ``timeStamp`` stays in SECONDS as delivered — the
decoder owns the ×1000 to ms (DECISIONS: decode timestamps).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

from auradefi.errors import SourceError
from auradefi.sources.evm.txlist import (
    NormalTxRecord,
    TokenTxRecord,
    parse_normal_row,
    parse_tokentx_row,
)

TX_HASH_UPPER = "0x" + "AA" * 32
TX_HASH_LOWER = "0x" + "aa" * 32
FROM = "0x9999999999999999999999999999999999999999"
TO = "0x1111111111111111111111111111111111111111"
MIXED_ADDR = "0xD8Da6BF26964aF9D7eEd9e03E53415D37aA96045"
MIXED_ADDR_LOWER = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"
USDC_MIXED = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_LOWER = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"

NORMAL_KEYS = (
    "hash", "blockNumber", "timeStamp", "from", "to",
    "value", "gasUsed", "gasPrice", "isError",
)
TOKENTX_KEYS = (
    "hash", "blockNumber", "timeStamp", "from", "to", "contractAddress",
    "value", "tokenDecimal", "tokenSymbol", "gasUsed", "gasPrice",
)
NORMAL_NUMERIC_KEYS = ("blockNumber", "timeStamp", "value", "gasUsed", "gasPrice")
TOKENTX_NUMERIC_KEYS = NORMAL_NUMERIC_KEYS + ("tokenDecimal",)


def normal_row(omit: str | None = None, patch: dict | None = None) -> dict:
    row = {
        "hash": TX_HASH_UPPER,
        "blockNumber": "100",
        "timeStamp": "1700000000",
        "from": FROM,
        "to": TO,
        "value": "1000000000000000000",
        "gasUsed": "21000",
        "gasPrice": "10000000000",
        "isError": "0",
    }
    if patch:
        row.update(patch)
    if omit is not None:
        del row[omit]
    return row


def tokentx_row(omit: str | None = None, patch: dict | None = None) -> dict:
    row = {
        "hash": TX_HASH_UPPER,
        "blockNumber": "18000000",
        "timeStamp": "1700000123",
        "from": MIXED_ADDR,
        "to": TO,
        "contractAddress": USDC_MIXED,
        "value": "25000000",
        "tokenDecimal": "6",
        "tokenSymbol": "USDC",
        "gasUsed": "65000",
        "gasPrice": "30000000000",
    }
    if patch:
        row.update(patch)
    if omit is not None:
        del row[omit]
    return row


NORMAL_GOLDEN = NormalTxRecord(
    tx_hash=TX_HASH_LOWER,
    block_number=100,
    time_stamp=1700000000,
    from_address=FROM,
    to_address=TO,
    value_wei=1000000000000000000,
    gas_used=21000,
    gas_price_wei=10000000000,
    is_error=False,
)

TOKENTX_GOLDEN = TokenTxRecord(
    tx_hash=TX_HASH_LOWER,
    block_number=18000000,
    time_stamp=1700000123,
    from_address=MIXED_ADDR_LOWER,
    to_address=TO,
    contract_address=USDC_LOWER,
    value_raw=25000000,
    token_decimal=6,
    token_symbol="USDC",
    gas_used=65000,
    gas_price_wei=30000000000,
)


# --- happy path ---------------------------------------------------------


def test_parse_normal_row_golden():
    record = parse_normal_row(normal_row())
    assert record == NORMAL_GOLDEN
    assert record.value_wei == 10**18
    assert record.gas_used == 21000
    assert record.gas_price_wei == 10**10
    assert record.is_error is False
    assert record.tx_hash == "0x" + "aa" * 32


def test_parse_normal_row_keeps_timestamp_in_seconds():
    record = parse_normal_row(normal_row())
    assert record.time_stamp == 1700000000  # NOT ms — decode owns ×1000
    assert record.block_number == 100


def test_parse_normal_row_lowercases_hash_and_addresses():
    row = normal_row(patch={"from": MIXED_ADDR, "to": USDC_MIXED})
    record = parse_normal_row(row)
    assert record.tx_hash == TX_HASH_LOWER
    assert record.from_address == MIXED_ADDR_LOWER
    assert record.to_address == USDC_LOWER


def test_parse_tokentx_row_golden():
    record = parse_tokentx_row(tokentx_row())
    assert record == TOKENTX_GOLDEN
    assert record.value_raw == 25000000
    assert record.token_decimal == 6
    assert record.token_symbol == "USDC"
    assert record.contract_address == USDC_LOWER


def test_unknown_extra_keys_are_ignored():
    extras = {"nonce": "7", "confirmations": "12", "methodId": "0x", "input": "0x"}
    assert parse_normal_row(normal_row(patch=extras)) == NORMAL_GOLDEN
    assert parse_tokentx_row(tokentx_row(patch=extras)) == TOKENTX_GOLDEN


# --- boundaries ---------------------------------------------------------


def test_huge_amount_exact_78_digit_int_never_through_float():
    nines = "9" * 78
    record = parse_normal_row(normal_row(patch={"value": nines}))
    assert record.value_wei == 10**78 - 1  # int(float(...)) cannot round-trip
    token = parse_tokentx_row(tokentx_row(patch={"value": nines}))
    assert token.value_raw == 10**78 - 1


def test_zero_values_parse_to_zero():
    record = parse_normal_row(
        normal_row(patch={"value": "0", "blockNumber": "0", "gasUsed": "0"})
    )
    assert record.value_wei == 0
    assert record.block_number == 0
    assert record.gas_used == 0


def test_to_address_empty_round_trips_as_empty():
    record = parse_normal_row(normal_row(patch={"to": ""}))
    assert record.to_address == ""
    token = parse_tokentx_row(tokentx_row(patch={"to": ""}))
    assert token.to_address == ""


# --- isError ------------------------------------------------------------


def test_is_error_one_is_true():
    record = parse_normal_row(normal_row(patch={"isError": "1"}))
    assert record.is_error is True


@pytest.mark.parametrize("bad", ["2", "", "00", "01", "10", "true", "False", 0, 1, None])
def test_is_error_anything_but_exact_0_or_1_raises(bad):
    with pytest.raises(SourceError):
        parse_normal_row(normal_row(patch={"isError": bad}))


def test_missing_is_error_raises_source_error_naming_the_key():
    with pytest.raises(SourceError, match="isError"):
        parse_normal_row(normal_row(omit="isError"))


# --- rule #2: string-typed numerics only --------------------------------


def test_value_as_python_int_raises_source_error_naming_value():
    with pytest.raises(SourceError, match="value"):
        parse_normal_row(normal_row(patch={"value": 1000000000000000000}))
    with pytest.raises(SourceError, match="value"):
        parse_tokentx_row(tokentx_row(patch={"value": 25000000}))


@pytest.mark.parametrize("key", NORMAL_NUMERIC_KEYS)
def test_normal_numeric_field_as_json_number_raises(key):
    with pytest.raises(SourceError, match=key):
        parse_normal_row(normal_row(patch={key: 100}))


@pytest.mark.parametrize("key", TOKENTX_NUMERIC_KEYS)
def test_tokentx_numeric_field_as_json_number_raises(key):
    with pytest.raises(SourceError, match=key):
        parse_tokentx_row(tokentx_row(patch={key: 6}))


@pytest.mark.parametrize(
    "bad", ["0x10", "1_0", " 10 ", "+1", "-5", "1.5", "1e3", "", "ten"]
)
def test_malformed_number_raises_source_error_naming_the_key(bad):
    with pytest.raises(SourceError, match="blockNumber"):
        parse_normal_row(normal_row(patch={"blockNumber": bad}))
    with pytest.raises(SourceError, match="tokenDecimal"):
        parse_tokentx_row(tokentx_row(patch={"tokenDecimal": bad}))


# --- missing keys and non-string fields ---------------------------------


@pytest.mark.parametrize("key", NORMAL_KEYS)
def test_parse_normal_row_missing_key_raises_naming_it(key):
    with pytest.raises(SourceError, match=key):
        parse_normal_row(normal_row(omit=key))


@pytest.mark.parametrize("key", TOKENTX_KEYS)
def test_parse_tokentx_row_missing_key_raises_naming_it(key):
    with pytest.raises(SourceError, match=key):
        parse_tokentx_row(tokentx_row(omit=key))


@pytest.mark.parametrize("key,bad", [("hash", 123), ("from", None), ("to", 5)])
def test_normal_non_string_text_field_raises_naming_it(key, bad):
    with pytest.raises(SourceError, match=key):
        parse_normal_row(normal_row(patch={key: bad}))


@pytest.mark.parametrize(
    "key,bad", [("contractAddress", None), ("tokenSymbol", 42), ("hash", b"0xaa")]
)
def test_tokentx_non_string_text_field_raises_naming_it(key, bad):
    with pytest.raises(SourceError, match=key):
        parse_tokentx_row(tokentx_row(patch={key: bad}))


# --- input never mutated -------------------------------------------------


def test_input_dict_unchanged_after_success():
    row = normal_row()
    snapshot = dict(row)
    parse_normal_row(row)
    assert row == snapshot
    token = tokentx_row()
    token_snapshot = dict(token)
    parse_tokentx_row(token)
    assert token == token_snapshot


def test_input_dict_unchanged_after_failure():
    row = normal_row(omit="gasPrice")
    snapshot = dict(row)
    with pytest.raises(SourceError, match="gasPrice"):
        parse_normal_row(row)
    assert row == snapshot
    token = tokentx_row(patch={"value": 25000000})
    token_snapshot = dict(token)
    with pytest.raises(SourceError):
        parse_tokentx_row(token)
    assert token == token_snapshot


# --- record shape: frozen, slots, hashable ------------------------------


def test_records_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        NORMAL_GOLDEN.value_wei = 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        TOKENTX_GOLDEN.value_raw = 0


def test_records_are_slotted_no_dict():
    assert not hasattr(NORMAL_GOLDEN, "__dict__")
    assert not hasattr(TOKENTX_GOLDEN, "__dict__")


def test_records_are_hashable_and_equal_by_value():
    twin = dataclasses.replace(NORMAL_GOLDEN)
    assert twin == NORMAL_GOLDEN
    assert hash(twin) == hash(NORMAL_GOLDEN)
    assert NORMAL_GOLDEN in {twin}
    assert TOKENTX_GOLDEN in {dataclasses.replace(TOKENTX_GOLDEN)}


# --- module purity: stdlib only, layer contract -------------------------


def _imported_names() -> set[str]:
    import auradefi.sources.evm.txlist as mod

    source = Path(inspect.getsourcefile(mod)).read_text(encoding="utf-8")
    package = ["auradefi", "sources", "evm"]
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join(anchor + ([node.module] if node.module else []))
            if base:
                names.add(base)
    return names


def test_module_never_imports_http_clients_or_sockets():
    tops = {name.split(".")[0] for name in _imported_names()}
    banned = {"httpx", "requests", "urllib3", "aiohttp", "socket"}
    assert not tops & banned, f"txlist.py must be fetch-free: {sorted(tops & banned)}"
    assert "urllib.request" not in _imported_names()


def test_module_never_imports_decode_or_higher_layers():
    banned_domains = {"decode", "positions", "prices", "portfolio", "ledger", "api"}
    offenders = {
        name
        for name in _imported_names()
        if name.split(".")[0] == "auradefi"
        and len(name.split(".")) > 1
        and name.split(".")[1] in banned_domains
    }
    assert not offenders, f"sources may import only money/chains/assets: {offenders}"
