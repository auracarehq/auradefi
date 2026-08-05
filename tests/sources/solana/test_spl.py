"""Contract tests for pure SPL parsing (SPEC §3.2, §3.3, §4.1 warning;
DECISIONS "Solana ScaledUiAmount detection").

Every golden below is hand-derived, never computed by the code under test:

  * ``str(Quantity(250000000, 6)) == "250"``  -> USDC is unscaled.
  * ``str(Quantity(1000000000, 9)) == "1"`` while the RPC says ``"2"`` ->
    the ScaledUiAmount identity break, caught by string comparison ALONE
    (no ``extensions`` list, no ``uiAmount`` float).
  * ``str(Quantity(1000000000, 6)) == "1000"`` -> 250 + 750 USDC.
  * ``str(Quantity(3500000000, 9)) == "3.5"`` -> 3.5 SOL from lamports.
  * ``Decimal("0.00000005") * 2`` prints ``"1.0E-7"`` via ``str`` but
    ``"0.00000010"`` via ``format(_, "f")``; the pinned answer is
    ``"0.0000001"``. A ``str()``-based sum fails this test.
  * ``Decimal("100") + Decimal("150") == 250`` must stay ``"250"``: a
    naive ``rstrip("0")`` yields ``"25"`` and is caught here.

The 10**77-scale equalities do NOT survive a float roundtrip, so they
mechanically fail any implementation that goes through ``uiAmount``
(SPEC rules #1/#2).
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import sys
from pathlib import Path

import pytest

from auradefi.chains import solana as solana_chain
from auradefi.errors import SourceError, ValidationError
from auradefi.money.quantity import Quantity
from auradefi.sources.solana import spl as spl_module
from auradefi.sources.solana.spl import (
    NATIVE_CAIP19,
    NATIVE_DECIMALS,
    MintBalance,
    SolanaBalance,
    TokenAccountRecord,
    aggregate_by_mint,
    build_balances,
    parse_token_accounts,
    token_caip19,
)

SPL_PATH = Path(spl_module.__file__)

PINNED_NATIVE_CAIP19 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/slip44:501"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
T22_MINT = "ScaLedUiAmountMint11111111111111111111111111"
OWNER = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
USDC_PUBKEY = "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T"
T22_PUBKEY = "TokenAccountPubkey1111111111111111111111111"


def _row(
    *,
    pubkey: str,
    mint: str,
    program: str,
    amount: str,
    decimals: int,
    ui_amount_string: str,
    ui_amount: object = 0.0,
    owner: str = OWNER,
) -> dict:
    """One ``getTokenAccountsByOwner`` jsonParsed ``result.value`` row."""
    return {
        "pubkey": pubkey,
        "account": {
            "data": {
                "program": program,
                "parsed": {
                    "type": "account",
                    "info": {
                        "isNative": False,
                        "mint": mint,
                        "owner": owner,
                        "state": "initialized",
                        "tokenAmount": {
                            "amount": amount,
                            "decimals": decimals,
                            "uiAmount": ui_amount,
                            "uiAmountString": ui_amount_string,
                        },
                    },
                },
                "space": 165,
            },
            "executable": False,
            "lamports": 2039280,
            "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "rentEpoch": 18446744073709551615,
        },
    }


def _usdc_row() -> dict:
    """250 USDC, spl-token, a normal mint where raw/10**d holds."""
    return _row(
        pubkey=USDC_PUBKEY,
        mint=USDC_MINT,
        program="spl-token",
        amount="250000000",
        decimals=6,
        ui_amount_string="250",
        ui_amount=250.0,
    )


def _t22_row() -> dict:
    """Token-2022 ScaledUiAmount: raw says 1, the RPC displays 2."""
    return _row(
        pubkey=T22_PUBKEY,
        mint=T22_MINT,
        program="spl-token-2022",
        amount="1000000000",
        decimals=9,
        ui_amount_string="2",
        ui_amount=2.0,
    )


def _info(row: dict) -> dict:
    return row["account"]["data"]["parsed"]["info"]


def _token_amount(row: dict) -> dict:
    return _info(row)["tokenAmount"]


def _record(
    mint: str = USDC_MINT,
    raw: int = 250000000,
    decimals: int = 6,
    ui: str = "250",
    scaled: bool = False,
    pubkey: str = USDC_PUBKEY,
    program: str = "spl-token",
) -> TokenAccountRecord:
    return TokenAccountRecord(
        pubkey=pubkey,
        mint=mint,
        owner=OWNER,
        program=program,
        quantity=Quantity(raw, decimals),
        ui_amount_string=ui,
        scaled_ui=scaled,
    )


class TestPinnedConstants:
    def test_native_decimals_is_9(self):
        assert NATIVE_DECIMALS == 9

    def test_native_caip19_is_the_pinned_literal(self):
        # Stability contract: a hardcoded string, not a call.
        assert NATIVE_CAIP19 == PINNED_NATIVE_CAIP19

    def test_native_caip19_is_built_from_chain_constants(self):
        assert NATIVE_CAIP19 == f"{solana_chain.MAINNET}/slip44:{solana_chain.SLIP44}"

    def test_token_caip19_preserves_base58_case(self):
        assert token_caip19(T22_MINT) == (
            "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/token:"
            "ScaLedUiAmountMint11111111111111111111111111"
        )


class TestPurity:
    """Mechanical proof of the layer and the 'never read uiAmount' pin."""

    @staticmethod
    def _imports() -> list[str]:
        tree = ast.parse(SPL_PATH.read_text(encoding="utf-8"))
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names.append(node.module or "")
        return names

    def test_imports_no_http_client(self):
        roots = {name.split(".")[0] for name in self._imports()}
        assert not roots & {"httpx", "requests", "urllib3", "aiohttp"}

    def test_imports_only_stdlib_and_auradefi(self):
        offenders = sorted(
            name
            for name in self._imports()
            if name.split(".")[0] not in sys.stdlib_module_names
            and name.split(".")[0] != "auradefi"
        )
        assert not offenders, f"spl.py must stay pure: {offenders}"

    def test_imports_only_the_three_allowed_auradefi_modules(self):
        allowed = {
            "auradefi.money.quantity",
            "auradefi.chains.solana",
            "auradefi.errors",
        }
        used = {name for name in self._imports() if name.startswith("auradefi")}
        assert used <= allowed, f"unexpected internal imports: {sorted(used - allowed)}"

    def test_the_uiamount_float_key_is_never_named_in_the_source(self):
        # "uiAmountString" is fine; the bare "uiAmount" key must not appear.
        tree = ast.parse(SPL_PATH.read_text(encoding="utf-8"))
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert "uiAmount" not in literals


class TestDataclassShapes:
    def test_token_account_record_is_frozen_with_slots(self):
        record = _record()
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.scaled_ui = True  # type: ignore[misc]
        assert not hasattr(record, "__dict__")

    def test_mint_balance_is_frozen_with_slots(self):
        balance = MintBalance(USDC_MINT, Quantity(1, 6), "0.000001", False)
        with pytest.raises(dataclasses.FrozenInstanceError):
            balance.mint = T22_MINT  # type: ignore[misc]
        assert not hasattr(balance, "__dict__")

    def test_solana_balance_is_frozen_with_slots(self):
        balance = SolanaBalance(NATIVE_CAIP19, Quantity(1, 9), None, "1e-9", False)
        with pytest.raises(dataclasses.FrozenInstanceError):
            balance.mint = USDC_MINT  # type: ignore[misc]
        assert not hasattr(balance, "__dict__")


class TestParseTokenAccounts:
    def test_usdc_row_golden(self):
        (record,) = parse_token_accounts([_usdc_row()])
        assert record.pubkey == USDC_PUBKEY
        assert record.mint == USDC_MINT
        assert record.owner == OWNER
        assert record.program == "spl-token"
        assert record.quantity == Quantity(250000000, 6)
        assert record.ui_amount_string == "250"
        assert record.scaled_ui is False
        # The identity holds here: hand-derived, not computed by the code.
        assert str(record.quantity) == "250"

    def test_token_2022_scaled_ui_amount_breaks_the_identity(self):
        (record,) = parse_token_accounts([_t22_row()])
        assert record.program == "spl-token-2022"
        assert record.quantity == Quantity(1000000000, 9)
        assert record.ui_amount_string == "2"
        assert record.scaled_ui is True
        # raw/10**decimals says 1; the RPC displays 2. String comparison
        # alone finds it (DECISIONS pin), no extensions list needed.
        assert str(record.quantity) == "1"

    def test_rows_are_parsed_in_order(self):
        records = parse_token_accounts([_t22_row(), _usdc_row()])
        assert [r.mint for r in records] == [T22_MINT, USDC_MINT]

    def test_empty_rows_give_no_records(self):
        assert parse_token_accounts([]) == []

    def test_zero_amount_account_parses_unscaled(self):
        row = _row(
            pubkey=USDC_PUBKEY,
            mint=USDC_MINT,
            program="spl-token",
            amount="0",
            decimals=6,
            ui_amount_string="0",
        )
        (record,) = parse_token_accounts([row])
        assert record.quantity == Quantity(0, 6)
        assert record.ui_amount_string == "0"
        assert record.scaled_ui is False

    def test_zero_decimals_token_parses(self):
        row = _row(
            pubkey=USDC_PUBKEY,
            mint=T22_MINT,
            program="spl-token",
            amount="7",
            decimals=0,
            ui_amount_string="7",
        )
        (record,) = parse_token_accounts([row])
        assert record.quantity == Quantity(7, 0)
        assert record.scaled_ui is False

    def test_amount_is_exact_at_1e77_scale(self):
        # 10**77 + 1 does not survive a float roundtrip.
        huge = 10**77 + 1
        row = _row(
            pubkey=USDC_PUBKEY,
            mint=USDC_MINT,
            program="spl-token",
            amount=str(huge),
            decimals=0,
            ui_amount_string=str(huge),
            ui_amount=1e77,
        )
        (record,) = parse_token_accounts([row])
        assert record.quantity.raw == huge
        assert record.quantity.raw != 10**77
        assert record.scaled_ui is False


class TestUiAmountFloatIsNeverRead:
    """The float member may be absent, wrong or garbage: same result."""

    @staticmethod
    def _baseline() -> TokenAccountRecord:
        return parse_token_accounts([_usdc_row()])[0]

    def test_deleted_ui_amount_parses_identically(self):
        row = _usdc_row()
        del _token_amount(row)["uiAmount"]
        assert "uiAmount" not in _token_amount(row)
        (record,) = parse_token_accounts([row])
        assert record == self._baseline()

    @pytest.mark.parametrize(
        "value", [None, "not-a-number", 999.999, -1.0, float("nan"), {}]
    )
    def test_nonsense_ui_amount_parses_identically(self, value):
        row = _usdc_row()
        _token_amount(row)["uiAmount"] = value
        (record,) = parse_token_accounts([row])
        assert record == self._baseline()

    def test_deleted_extensions_list_does_not_affect_detection(self):
        # Older RPCs omit `extensions` entirely; detection must not use it.
        row = _t22_row()
        assert "extensions" not in _info(row)
        (record,) = parse_token_accounts([row])
        assert record.scaled_ui is True


def _mutate_amount(value: object):
    def apply(row: dict) -> None:
        _token_amount(row)["amount"] = value

    return apply


def _delete(container, key: str):
    def apply(row: dict) -> None:
        del container(row)[key]

    return apply


def _set(container, key: str, value: object):
    def apply(row: dict) -> None:
        container(row)[key] = value

    return apply


def _parsed(row: dict) -> dict:
    return row["account"]["data"]["parsed"]


def _data(row: dict) -> dict:
    return row["account"]["data"]


MALFORMED = {
    # amount must be an unsigned base-10 digit STRING (rules #1/#2).
    "amount_underscore": _mutate_amount("1_0"),
    "amount_whitespace": _mutate_amount(" 10 "),
    "amount_plus_sign": _mutate_amount("+1"),
    "amount_negative": _mutate_amount("-1"),
    "amount_empty": _mutate_amount(""),
    "amount_json_int": _mutate_amount(10),
    "amount_float": _mutate_amount(250.0),
    "amount_hex": _mutate_amount("0x10"),
    "amount_exponent": _mutate_amount("1e3"),
    "amount_missing": _delete(_token_amount, "amount"),
    # decimals must be a non-bool int >= 0.
    "decimals_bool": _set(_token_amount, "decimals", True),
    "decimals_str": _set(_token_amount, "decimals", "6"),
    "decimals_negative": _set(_token_amount, "decimals", -1),
    "decimals_float": _set(_token_amount, "decimals", 6.0),
    "decimals_missing": _delete(_token_amount, "decimals"),
    # uiAmountString must be a str.
    "ui_amount_string_int": _set(_token_amount, "uiAmountString", 250),
    "ui_amount_string_none": _set(_token_amount, "uiAmountString", None),
    "ui_amount_string_missing": _delete(_token_amount, "uiAmountString"),
    # tokenAmount itself.
    "token_amount_missing": _delete(_info, "tokenAmount"),
    "token_amount_not_a_dict": _set(_info, "tokenAmount", "250"),
    # mint / owner.
    "mint_missing": _delete(_info, "mint"),
    "mint_empty": _set(_info, "mint", ""),
    "mint_not_a_str": _set(_info, "mint", 123),
    "owner_missing": _delete(_info, "owner"),
    "owner_not_a_str": _set(_info, "owner", None),
    "info_not_a_dict": _set(_parsed, "info", "nope"),
    # parsed.type must be exactly "account".
    "type_is_mint": _set(_parsed, "type", "mint"),
    "type_missing": _delete(_parsed, "type"),
    "type_not_a_str": _set(_parsed, "type", 1),
    "parsed_not_a_dict": _set(_data, "parsed", []),
    "parsed_missing": _delete(_data, "parsed"),
    # program / pubkey / envelope.
    "program_missing": _delete(_data, "program"),
    "program_not_a_str": _set(_data, "program", 2022),
    "data_not_a_dict": _set(lambda row: row["account"], "data", "base64blob"),
    "data_missing": _delete(lambda row: row["account"], "data"),
    "account_missing": _delete(lambda row: row, "account"),
    "account_not_a_dict": _set(lambda row: row, "account", None),
    "pubkey_missing": _delete(lambda row: row, "pubkey"),
    "pubkey_not_a_str": _set(lambda row: row, "pubkey", 7),
}


class TestParseTokenAccountsRejects:
    @pytest.mark.parametrize("name", sorted(MALFORMED))
    def test_malformed_row_raises_source_error(self, name):
        row = copy.deepcopy(_usdc_row())
        MALFORMED[name](row)
        with pytest.raises(SourceError):
            parse_token_accounts([row])

    @pytest.mark.parametrize("row", ["nope", None, 42, [], ()])
    def test_row_that_is_not_a_dict_raises_source_error(self, row):
        with pytest.raises(SourceError):
            parse_token_accounts([row])

    def test_a_bad_row_after_a_good_one_still_raises(self):
        bad = _usdc_row()
        _token_amount(bad)["amount"] = "1_0"
        with pytest.raises(SourceError):
            parse_token_accounts([_t22_row(), bad])


class TestAggregateByMint:
    def test_two_usdc_accounts_sum_to_one_mint_balance(self):
        records = [
            _record(raw=250000000),
            _record(raw=750000000, pubkey=T22_PUBKEY),
        ]
        (balance,) = aggregate_by_mint(records)
        assert balance == MintBalance(
            mint=USDC_MINT,
            quantity=Quantity(1000000000, 6),  # 250000000 + 750000000
            ui_amount_string="1000",  # str(Quantity(1000000000, 6))
            scaled_ui=False,
        )

    def test_empty_input_gives_no_balances(self):
        assert aggregate_by_mint([]) == []

    def test_single_account_passes_through(self):
        (balance,) = aggregate_by_mint([_record()])
        assert balance == MintBalance(USDC_MINT, Quantity(250000000, 6), "250", False)

    def test_decimals_mismatch_within_a_mint_raises_naming_the_mint(self):
        records = [
            _record(raw=250000000, decimals=6),
            _record(raw=1, decimals=5, ui="0.00001"),
        ]
        with pytest.raises(SourceError, match=USDC_MINT):
            aggregate_by_mint(records)

    def test_same_decimals_across_different_mints_is_fine(self):
        records = [
            _record(mint=USDC_MINT, raw=1, decimals=6, ui="0.000001"),
            _record(mint=T22_MINT, raw=1, decimals=6, ui="0.000001"),
        ]
        assert len(aggregate_by_mint(records)) == 2

    def test_output_is_sorted_ascending_by_mint(self):
        records = [
            _record(mint=T22_MINT, raw=1000000000, decimals=9, ui="2", scaled=True),
            _record(mint=USDC_MINT),
        ]
        # "E" (0x45) sorts before "S" (0x53): base58, case-sensitive.
        assert [b.mint for b in aggregate_by_mint(records)] == [USDC_MINT, T22_MINT]

    def test_interleaved_accounts_group_by_exact_mint(self):
        records = [
            _record(mint=USDC_MINT, raw=1000000),
            _record(mint=T22_MINT, raw=1000000000, decimals=9, ui="2", scaled=True),
            _record(mint=USDC_MINT, raw=2000000),
        ]
        usdc, t22 = aggregate_by_mint(records)
        assert usdc.quantity == Quantity(3000000, 6)
        assert t22.quantity == Quantity(1000000000, 9)

    def test_sum_is_exact_at_1e77_scale(self):
        records = [
            _record(raw=10**77, ui=str(Quantity(10**77, 6))),
            _record(raw=1, ui="0.000001"),
        ]
        (balance,) = aggregate_by_mint(records)
        assert balance.quantity == Quantity(10**77 + 1, 6)


class TestAggregateScaledUiAmount:
    def test_a_single_scaled_constituent_passes_through_verbatim(self):
        records = [_record(mint=T22_MINT, raw=1000000000, decimals=9, ui="2", scaled=True)]
        (balance,) = aggregate_by_mint(records)
        assert balance.ui_amount_string == "2"  # NOT str(Quantity(1e9, 9)) == "1"
        assert balance.scaled_ui is True
        assert balance.quantity == Quantity(1000000000, 9)

    def test_scaled_plus_unscaled_sums_the_displayed_strings(self):
        # Decimal("1.5") + Decimal("0.50") == 2.00 -> "2" after the strip.
        records = [
            _record(mint=T22_MINT, raw=150, decimals=2, ui="1.5", scaled=False),
            _record(mint=T22_MINT, raw=999, decimals=2, ui="0.50", scaled=True),
        ]
        (balance,) = aggregate_by_mint(records)
        assert balance.quantity == Quantity(1149, 2)  # raw arithmetic, untouched
        assert balance.scaled_ui is True  # any(constituent)
        assert balance.ui_amount_string == "2"

    def test_scaled_sum_never_emits_scientific_notation(self):
        # str(Decimal("0.00000005") * 2) == "1.0E-7". Format(_, "f") is
        # pinned precisely so this cannot leak to a display string.
        records = [
            _record(mint=T22_MINT, raw=1, decimals=9, ui="0.00000005", scaled=True),
            _record(mint=T22_MINT, raw=1, decimals=9, ui="0.00000005", scaled=True),
        ]
        (balance,) = aggregate_by_mint(records)
        assert balance.ui_amount_string == "0.0000001"
        assert "E" not in balance.ui_amount_string
        assert "e" not in balance.ui_amount_string

    def test_integer_trailing_zeros_are_not_stripped(self):
        # Decimal("100") + Decimal("150") == 250; a naive rstrip("0")
        # would return "25".
        records = [
            _record(mint=T22_MINT, raw=1, decimals=0, ui="100", scaled=True),
            _record(mint=T22_MINT, raw=1, decimals=0, ui="150", scaled=True),
        ]
        (balance,) = aggregate_by_mint(records)
        assert balance.ui_amount_string == "250"

    def test_unscaled_group_uses_the_quantity_string(self):
        records = [_record(raw=1500000, ui="1.5"), _record(raw=500000, ui="0.5")]
        (balance,) = aggregate_by_mint(records)
        assert balance.scaled_ui is False
        assert balance.ui_amount_string == "2"  # str(Quantity(2000000, 6))


def _usdc_mint_balance(raw: int = 1000000000) -> MintBalance:
    return MintBalance(USDC_MINT, Quantity(raw, 6), str(Quantity(raw, 6)), False)


def _t22_mint_balance() -> MintBalance:
    return MintBalance(T22_MINT, Quantity(1000000000, 9), "2", True)


class TestBuildBalances:
    def test_golden_three_records_native_first(self):
        balances = build_balances(3500000000, [_usdc_mint_balance(), _t22_mint_balance()])
        assert len(balances) == 3
        native, usdc, t22 = balances

        assert native == SolanaBalance(
            caip19="solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/slip44:501",
            quantity=Quantity(3500000000, 9),  # 3.5 SOL in lamports
            mint=None,
            ui_amount_string="3.5",  # str(Quantity(3500000000, 9))
            scaled_ui=False,
        )
        assert usdc == SolanaBalance(
            caip19=(
                "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/token:"
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            ),
            quantity=Quantity(1000000000, 6),
            mint=USDC_MINT,
            ui_amount_string="1000",
            scaled_ui=False,
        )
        assert t22 == SolanaBalance(
            caip19=(
                "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/token:"
                "ScaLedUiAmountMint11111111111111111111111111"
            ),
            quantity=Quantity(1000000000, 9),
            mint=T22_MINT,
            ui_amount_string="2",
            scaled_ui=True,
        )

    def test_token_caip19_never_lowercases_the_mint(self):
        (balance,) = build_balances(0, [_t22_mint_balance()])
        assert balance.caip19.endswith(f"/token:{T22_MINT}")
        assert balance.caip19 != balance.caip19.lower()

    def test_mint_order_is_preserved_not_resorted(self):
        balances = build_balances(0, [_t22_mint_balance(), _usdc_mint_balance()])
        assert [b.mint for b in balances] == [T22_MINT, USDC_MINT]

    def test_zero_lamports_omits_the_native_record(self):
        balances = build_balances(0, [_usdc_mint_balance()])
        assert [b.mint for b in balances] == [USDC_MINT]

    def test_one_lamport_still_emits_native(self):
        (native,) = build_balances(1, [])
        assert native.quantity == Quantity(1, 9)
        assert native.ui_amount_string == "0.000000001"
        assert native.mint is None
        assert native.scaled_ui is False

    def test_zero_raw_mint_balance_is_omitted(self):
        zero = MintBalance(USDC_MINT, Quantity(0, 6), "0", False)
        assert build_balances(0, [zero]) == []
        (native,) = build_balances(3500000000, [zero])
        assert native.mint is None

    def test_nothing_at_all_gives_an_empty_list(self):
        assert build_balances(0, []) == []

    def test_lamports_are_exact_at_1e77_scale(self):
        (native,) = build_balances(10**77 + 1, [])
        assert native.quantity == Quantity(10**77 + 1, 9)

    @pytest.mark.parametrize(
        "lamports", [True, False, -1, -3500000000, 1.0, "1", None, Quantity(1, 9)]
    )
    def test_invalid_lamports_raises_validation_error(self, lamports):
        with pytest.raises(ValidationError):
            build_balances(lamports, [])


class TestEndToEndFromRows:
    def test_rows_to_balances_matches_the_phase_gate_shape(self):
        records = parse_token_accounts([_usdc_row(), _t22_row()])
        balances = build_balances(3500000000, aggregate_by_mint(records))
        assert [b.caip19 for b in balances] == [
            "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/slip44:501",
            "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/token:"
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/token:"
            "ScaLedUiAmountMint11111111111111111111111111",
        ]
        assert [b.ui_amount_string for b in balances] == ["3.5", "250", "2"]
        assert [b.scaled_ui for b in balances] == [False, False, True]
