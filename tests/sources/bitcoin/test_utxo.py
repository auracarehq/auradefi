"""Contract tests for the pure Bitcoin UTXO models (SPEC §3.2, §10;
DECISIONS "Gap-limit scan": BTC decimals = 8, caip19 pinned).

Golden numbers are hand-derived, never computed by the code under test:
confirmed = funded_txo_sum - spent_txo_sum = 5000 - 1000 = 4000 sats;
the two pinned UTXO rows sum to 4000 confirmed and 4250 total; and
str(Quantity(4000, 8)) == "0.00004". The 10**77-scale totals below do
NOT survive a float roundtrip, so the exact int equalities mechanically
fail any implementation that sums money through float (SPEC rule #2).
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from auradefi.chains import bitcoin
from auradefi.errors import ValidationError
from auradefi.money.quantity import Quantity
from auradefi.sources.bitcoin import utxo as utxo_module
from auradefi.sources.bitcoin.utxo import (
    BTC_CAIP19,
    SATS_DECIMALS,
    AddressBalance,
    AddressStats,
    BitcoinScanResult,
    Utxo,
    confirmed_sats,
    total_sats,
)

TXID_A = "a1" * 32
TXID_B = "b2" * 32
PINNED_CAIP19 = "bip122:000000000019d6689c085ae165831e93/slip44:0"


def _golden_utxos() -> tuple[Utxo, Utxo]:
    """The two rows pinned in tests/cassettes/esplora_scan.json."""
    return (
        Utxo(txid=TXID_A, vout=0, value_sats=4000, confirmed=True),
        Utxo(txid=TXID_B, vout=1, value_sats=250, confirmed=False),
    )


class TestConstants:
    def test_sats_decimals_is_8(self):
        assert SATS_DECIMALS == 8

    def test_btc_caip19_is_the_pinned_literal(self):
        # Stability contract: a hardcoded string, not a call.
        assert BTC_CAIP19 == PINNED_CAIP19

    def test_btc_caip19_is_built_from_chain_constants(self):
        assert BTC_CAIP19 == f"{bitcoin.MAINNET}/slip44:{bitcoin.SLIP44}"


class TestUtxo:
    def test_happy_path_fields(self):
        row = Utxo(txid=TXID_A, vout=0, value_sats=4000, confirmed=True)
        assert row.txid == TXID_A
        assert row.vout == 0
        assert row.value_sats == 4000
        assert row.confirmed is True

    def test_boundaries_zero_and_huge_are_valid(self):
        assert Utxo(TXID_A, 0, 0, False).value_sats == 0
        assert Utxo(TXID_A, 0, 10**77, True).value_sats == 10**77

    @pytest.mark.parametrize(
        "override",
        [
            {"txid": ""},
            {"vout": -1},
            {"value_sats": -1},
            {"vout": True},  # bool rejected BEFORE int (house style)
            {"value_sats": True},  # bool rejected BEFORE int (house style)
            {"confirmed": 1},  # int is not bool
            {"confirmed": "yes"},
        ],
    )
    def test_invalid_field_raises_validation_error(self, override):
        base = {"txid": TXID_A, "vout": 0, "value_sats": 4000, "confirmed": True}
        with pytest.raises(ValidationError):
            Utxo(**{**base, **override})

    def test_frozen_with_slots(self):
        row = Utxo(TXID_A, 0, 4000, True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            row.value_sats = 5  # type: ignore[misc]
        assert not hasattr(row, "__dict__")


class TestAddressStats:
    def test_golden_confirmed_sats_is_funded_minus_spent(self):
        stats = AddressStats(5000, 1000, 2)
        assert stats.funded_txo_sum == 5000
        assert stats.spent_txo_sum == 1000
        assert stats.tx_count == 2
        assert stats.confirmed_sats == 4000  # 5000 - 1000, hand-derived

    def test_swept_address_confirms_to_zero(self):
        assert AddressStats(700, 700, 3).confirmed_sats == 0

    def test_all_zero_is_valid(self):
        assert AddressStats(0, 0, 0).confirmed_sats == 0

    @pytest.mark.parametrize(
        "args",
        [
            (1, 2, 1),  # spent > funded — acceptance-pinned
            (-1, 0, 0),
            (0, -1, 0),
            (0, 0, -1),
            (True, 0, 0),  # bool rejected BEFORE int
            (0, False, 0),  # bool rejected BEFORE int (False == 0 numerically)
            (0, 0, True),
        ],
    )
    def test_invalid_raises_validation_error(self, args):
        with pytest.raises(ValidationError):
            AddressStats(*args)

    def test_frozen_with_slots(self):
        stats = AddressStats(5000, 1000, 2)
        with pytest.raises(dataclasses.FrozenInstanceError):
            stats.tx_count = 3  # type: ignore[misc]
        assert not hasattr(stats, "__dict__")


class TestAddressBalance:
    def test_happy_path_fields(self):
        balance = AddressBalance("tb0x0", 0, 0, 4000, 2)
        assert balance.address == "tb0x0"
        assert balance.chain == 0
        assert balance.index == 0
        assert balance.balance_sats == 4000
        assert balance.tx_count == 2

    def test_change_chain_and_swept_zero_balance_are_valid(self):
        balance = AddressBalance("tb1x7", 1, 7, 0, 3)
        assert balance.chain == 1
        assert balance.balance_sats == 0

    @pytest.mark.parametrize(
        "args",
        [
            ("", 0, 0, 4000, 2),  # empty address
            ("tb0x0", 2, 0, 4000, 2),  # chain must be 0 or 1 — acceptance
            ("tb0x0", -1, 0, 4000, 2),
            ("tb0x0", True, 0, 4000, 2),  # bool rejected BEFORE the {0,1} check
            ("tb0x0", 0, -1, 4000, 2),
            ("tb0x0", 0, False, 4000, 2),  # bool rejected BEFORE int
            ("tb0x0", 0, 0, -1, 2),
            ("tb0x0", 0, 0, True, 2),
            ("tb0x0", 0, 0, 4000, -1),
            ("tb0x0", 0, 0, 4000, True),
        ],
    )
    def test_invalid_raises_validation_error(self, args):
        with pytest.raises(ValidationError):
            AddressBalance(*args)

    def test_frozen_with_slots(self):
        balance = AddressBalance("tb0x0", 0, 0, 4000, 2)
        with pytest.raises(dataclasses.FrozenInstanceError):
            balance.balance_sats = 1  # type: ignore[misc]
        assert not hasattr(balance, "__dict__")


class TestBitcoinScanResult:
    def test_empty_result_totals_and_caip19(self):
        result = BitcoinScanResult(addresses=())
        assert result.addresses == ()
        assert result.total_sats == 0
        assert result.total == Quantity(0, 8)  # acceptance-pinned
        assert result.caip19 == PINNED_CAIP19

    def test_single_address_golden_total(self):
        result = BitcoinScanResult(
            addresses=(AddressBalance("tb0x0", 0, 0, 4000, 2),)
        )
        assert result.total_sats == 4000
        assert result.total == Quantity(4000, 8)
        assert str(result.total) == "0.00004"  # exact BTC string, hand-derived

    def test_total_is_exact_at_1e77_scale(self):
        # 10**77 + 1 does not survive float; this equality kills float sums.
        result = BitcoinScanResult(
            addresses=(
                AddressBalance("tb0x0", 0, 0, 10**77, 1),
                AddressBalance("tb1x0", 1, 0, 1, 1),
            )
        )
        assert result.total_sats == 10**77 + 1
        assert result.total == Quantity(10**77 + 1, 8)

    def test_frozen_with_slots(self):
        result = BitcoinScanResult(addresses=())
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.addresses = ()  # type: ignore[misc]
        assert not hasattr(result, "__dict__")


class TestHelpers:
    def test_empty_iterables_sum_to_zero(self):
        assert confirmed_sats([]) == 0
        assert total_sats([]) == 0

    def test_golden_rows(self):
        rows = _golden_utxos()
        assert confirmed_sats(rows) == 4000  # unconfirmed 250 excluded
        assert total_sats(rows) == 4250  # 4000 + 250

    def test_only_unconfirmed_confirms_to_zero(self):
        rows = [Utxo(TXID_B, 1, 250, False)]
        assert confirmed_sats(rows) == 0
        assert total_sats(rows) == 250

    def test_exact_at_1e77_scale(self):
        rows = [Utxo(TXID_A, 0, 10**77, True), Utxo(TXID_B, 1, 1, True)]
        assert confirmed_sats(rows) == 10**77 + 1
        assert total_sats(rows) == 10**77 + 1


def test_module_is_pure_no_http_client_import():
    """utxo.py is PURE: no httpx (or any IO client), only allowed deps."""
    tree = ast.parse(Path(utxo_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module or "")
    tops = {name.split(".")[0] for name in imported}
    assert tops.isdisjoint(
        {"httpx", "requests", "urllib3", "aiohttp", "socket", "urllib"}
    ), f"utxo.py must be pure, found: {sorted(tops)}"
    internal = {
        name.split(".")[1] for name in imported if name.startswith("auradefi.")
    }
    assert internal <= {"errors", "money", "chains"}, (
        f"utxo.py may only use errors/money/chains, found: {sorted(internal)}"
    )
