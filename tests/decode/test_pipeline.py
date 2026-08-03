"""Contract tests for auradefi.decode.pipeline (SPEC §4.5; rules #4, #7).

Golden transaction-id literals were derived INDEPENDENTLY of the code
under test, via ``python3 -c`` over the algorithm pinned in
docs/DECISIONS.md:

    "txn_" + sha256(f"{chain_id}|{tx_hash}|{account_id}".encode()).hexdigest()[:16]

    eip155:1 | 0x + 'aa'*32 | acct_1  ->  txn_f7e3f7aba9d6775a
    eip155:1 | 0x + 'bb'*32 | acct_1  ->  txn_e5e727672fb4ada6
    eip155:1 | 0x + 'cc'*32 | acct_1  ->  txn_557113c18fb02870
    eip155:1 | 0x + 'dd'*32 | acct_1  ->  txn_a30f49051566e03d

Fee quantities are gas_used * gas_price_wei computed by hand (21000 *
10**10 = 210000000000000, etc.). A stability contract is a hardcoded
literal, not a call to the function under test.
"""

from __future__ import annotations

import inspect

import pytest

from auradefi.chains.families import ChainFamily
from auradefi.chains.registry import Chain, ChainRegistry
from auradefi.decode.models import (
    Act,
    BorneBy,
    DataQuality,
    Direction,
    Fee,
    Part,
    TxStatus,
    TxSubtype,
    TxType,
)
from auradefi.decode.pipeline import DECODER_VERSION, decode_account
from auradefi.errors import DecodeError, UnknownChainError
from auradefi.money.quantity import Quantity
from auradefi.sources.evm.txlist import NormalTxRecord, TokenTxRecord

CHAIN = "eip155:1"
ACCT_ID = "acct_1"
ACCOUNT = "0x" + "11" * 20
FROM_99 = "0x" + "99" * 20
TO_33 = "0x" + "33" * 20
CP_44 = "0x" + "44" * 20
TO_55 = "0x" + "55" * 20
STRANGER = "0x" + "88" * 20

USDC_CONTRACT = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
ETH = "eip155:1/slip44:60"
USDC = f"eip155:1/erc20:{USDC_CONTRACT}"

HASH_A = "0x" + "aa" * 32
HASH_B = "0x" + "bb" * 32
HASH_C = "0x" + "cc" * 32
HASH_D = "0x" + "dd" * 32

# Derived independently (see module docstring); NEVER regenerate from the
# implementation.
TXN_A = "txn_f7e3f7aba9d6775a"
TXN_B = "txn_e5e727672fb4ada6"
TXN_C = "txn_557113c18fb02870"
TXN_D = "txn_a30f49051566e03d"

GAS_PRICE = 10**10


def normal_row(**overrides) -> NormalTxRecord:
    """TX A defaults: inbound 1 ETH from 0x99..99, block 100."""
    fields = {
        "tx_hash": HASH_A,
        "block_number": 100,
        "time_stamp": 1_700_000_000,
        "from_address": FROM_99,
        "to_address": ACCOUNT,
        "value_wei": 10**18,
        "gas_used": 21_000,
        "gas_price_wei": GAS_PRICE,
        "is_error": False,
    }
    fields.update(overrides)
    return NormalTxRecord(**fields)


def token_row(**overrides) -> TokenTxRecord:
    """TX B tokentx defaults: 25 USDC out to 0x33..33, block 101."""
    fields = {
        "tx_hash": HASH_B,
        "block_number": 101,
        "time_stamp": 1_700_000_100,
        "from_address": ACCOUNT,
        "to_address": TO_33,
        "contract_address": USDC_CONTRACT,
        "value_raw": 25_000_000,
        "token_decimal": 6,
        "token_symbol": "USDC",
        "gas_used": 50_000,
        "gas_price_wei": GAS_PRICE,
    }
    fields.update(overrides)
    return TokenTxRecord(**fields)


def fixture_normal() -> list[NormalTxRecord]:
    return [
        normal_row(),  # A: native receive
        normal_row(  # B: zero-value call to the USDC contract
            tx_hash=HASH_B, block_number=101, time_stamp=1_700_000_100,
            from_address=ACCOUNT, to_address=USDC_CONTRACT, value_wei=0,
            gas_used=50_000,
        ),
        normal_row(  # C: 1 ETH out (swap leg)
            tx_hash=HASH_C, block_number=102, time_stamp=1_700_000_200,
            from_address=ACCOUNT, to_address=CP_44, value_wei=10**18,
            gas_used=120_000,
        ),
        normal_row(  # D: failed send
            tx_hash=HASH_D, block_number=103, time_stamp=1_700_000_300,
            from_address=ACCOUNT, to_address=TO_55, value_wei=5 * 10**17,
            gas_used=21_000, is_error=True,
        ),
    ]


def fixture_tokens() -> list[TokenTxRecord]:
    return [
        token_row(),  # B: 25 USDC out
        token_row(  # C: 3000 USDC in (swap leg)
            tx_hash=HASH_C, block_number=102, time_stamp=1_700_000_200,
            from_address=CP_44, to_address=ACCOUNT, value_raw=3_000_000_000,
            gas_used=120_000,
        ),
    ]


def decode_fixture():
    return decode_account(CHAIN, ACCT_ID, ACCOUNT, fixture_normal(), fixture_tokens())


class TestModuleContract:
    def test_decoder_version_is_pinned_to_1(self):
        assert DECODER_VERSION == 1
        assert type(DECODER_VERSION) is int

    def test_decode_account_signature_is_the_published_api(self):
        params = inspect.signature(decode_account).parameters
        assert list(params) == [
            "chain_id", "account_id", "address", "normal", "tokens", "registry",
        ]
        assert params["registry"].default is None


class TestTxANativeReceive:
    def test_identity_status_and_timestamps(self):
        txn = decode_fixture()[0]
        assert txn.id == TXN_A
        assert txn.chain_id == CHAIN
        assert txn.tx_hash == HASH_A
        assert txn.account_id == ACCT_ID
        assert txn.status is TxStatus.CONFIRMED
        assert txn.block_number == 100
        assert txn.initiated_at == 1_700_000_000_000
        assert txn.confirmed_at == 1_700_000_000_000

    def test_parts_exactly_one_native_in(self):
        txn = decode_fixture()[0]
        assert txn.parts == (
            Part(
                act_id="act_0",
                direction=Direction.IN,
                asset_id=ETH,
                quantity=Quantity(10**18, 18),
                value=None,
                price=None,
                from_address=FROM_99,
                to_address=ACCOUNT,
                meta_type=None,
                other_parties=(),
            ),
        )

    def test_type_receive_subtype_transfer(self):
        txn = decode_fixture()[0]
        assert txn.type is TxType.RECEIVE
        assert txn.subtype is TxSubtype.TRANSFER
        assert txn.acts == (Act("act_0", TxSubtype.TRANSFER, None),)

    def test_fee_borne_by_counterparty(self):
        txn = decode_fixture()[0]
        assert txn.fees == (
            Fee(
                asset_id=ETH,
                quantity=Quantity(210_000_000_000_000, 18),
                value=None,
                act_id="act_0",
                borne_by=BorneBy.COUNTERPARTY,
            ),
        )


class TestTxBErc20Send:
    def test_zero_native_value_emits_no_native_part(self):
        txn = decode_fixture()[1]
        assert txn.id == TXN_B
        assert txn.parts == (
            Part(
                act_id="act_0",
                direction=Direction.OUT,
                asset_id=USDC,
                quantity=Quantity(25_000_000, 6),
                value=None,
                price=None,
                from_address=ACCOUNT,
                to_address=TO_33,
                meta_type=None,
                other_parties=(),
            ),
        )

    def test_type_send_and_fee_borne_by_self(self):
        txn = decode_fixture()[1]
        assert txn.type is TxType.SEND
        assert txn.subtype is TxSubtype.TRANSFER
        assert txn.fees == (
            Fee(
                asset_id=ETH,
                quantity=Quantity(500_000_000_000_000, 18),
                value=None,
                act_id="act_0",
                borne_by=BorneBy.SELF,
            ),
        )


class TestTxCSwap:
    def test_parts_native_out_before_token_in(self):
        txn = decode_fixture()[2]
        assert txn.id == TXN_C
        assert [
            (p.direction, p.asset_id, p.quantity) for p in txn.parts
        ] == [
            (Direction.OUT, ETH, Quantity(10**18, 18)),
            (Direction.IN, USDC, Quantity(3_000_000_000, 6)),
        ]
        assert all(p.act_id == "act_0" for p in txn.parts)

    def test_type_trade_subtype_swap_single_act(self):
        txn = decode_fixture()[2]
        assert txn.type is TxType.TRADE
        assert txn.subtype is TxSubtype.SWAP
        assert txn.acts == (Act("act_0", TxSubtype.SWAP, None),)

    def test_fee_amount(self):
        txn = decode_fixture()[2]
        assert txn.fees == (
            Fee(
                asset_id=ETH,
                quantity=Quantity(1_200_000_000_000_000, 18),
                value=None,
                act_id="act_0",
                borne_by=BorneBy.SELF,
            ),
        )


class TestTxDFailed:
    def test_failed_zero_parts_fee_survives(self):
        txn = decode_fixture()[3]
        assert txn.id == TXN_D
        assert txn.status is TxStatus.FAILED
        assert txn.parts == ()
        assert txn.type is TxType.INTERACTION
        assert txn.subtype is TxSubtype.UNKNOWN
        assert txn.acts == (Act("act_0", TxSubtype.UNKNOWN, None),)
        assert txn.fees == (
            Fee(
                asset_id=ETH,
                quantity=Quantity(210_000_000_000_000, 18),
                value=None,
                act_id="act_0",
                borne_by=BorneBy.SELF,
            ),
        )

    def test_tokentx_rows_of_a_failed_hash_are_dropped(self):
        tokens = fixture_tokens() + [
            token_row(
                tx_hash=HASH_D, block_number=103, time_stamp=1_700_000_300,
                from_address=ACCOUNT, to_address=TO_55, value_raw=7,
                gas_used=21_000,
            )
        ]
        txn = decode_account(CHAIN, ACCT_ID, ACCOUNT, fixture_normal(), tokens)[3]
        assert txn.parts == ()
        assert txn.status is TxStatus.FAILED
        assert len(txn.fees) == 1


class TestWholeOutput:
    def test_sorted_by_block_then_hash(self):
        assert [t.id for t in decode_fixture()] == [TXN_A, TXN_B, TXN_C, TXN_D]

    def test_scrambled_input_still_sorted(self):
        normal = list(reversed(fixture_normal()))
        tokens = list(reversed(fixture_tokens()))
        out = decode_account(CHAIN, ACCT_ID, ACCOUNT, normal, tokens)
        assert [t.id for t in out] == [TXN_A, TXN_B, TXN_C, TXN_D]

    def test_same_block_ties_break_on_tx_hash_ascending(self):
        hash_ff = "0x" + "ff" * 32
        hash_ee = "0x" + "ee" * 32
        normal = [
            normal_row(tx_hash=hash_ff),
            normal_row(tx_hash=hash_ee),
        ]
        out = decode_account(CHAIN, ACCT_ID, ACCOUNT, normal, [])
        assert [t.tx_hash for t in out] == [hash_ee, hash_ff]

    def test_exactly_one_transaction_per_hash(self):
        out = decode_fixture()
        assert len(out) == 4
        assert len({t.tx_hash for t in out}) == 4

    def test_decoder_version_and_data_quality_on_every_output(self):
        for txn in decode_fixture():
            assert txn.decoder_version == 1
            assert txn.data_quality == DataQuality(
                ("fiat_value",), 1.0, 1, ("etherscan",)
            )
            assert txn.data_quality.decoder_version == txn.decoder_version
            assert txn.protocol is None

    def test_enrichment_deferred_on_every_part(self):
        for txn in decode_fixture():
            for part in txn.parts:
                assert part.value is None
                assert part.price is None
                assert part.meta_type is None
                assert part.other_parties == ()
            for fee in txn.fees:
                assert fee.value is None

    def test_empty_input_decodes_to_empty_tuple(self):
        assert decode_account(CHAIN, ACCT_ID, ACCOUNT, [], []) == ()


class TestErrors:
    def test_conflicting_block_numbers_for_one_hash(self):
        tokens = [token_row(block_number=999)]  # normal B says 101
        with pytest.raises(DecodeError):
            decode_account(CHAIN, ACCT_ID, ACCOUNT, fixture_normal(), tokens)

    def test_conflicting_timestamps_for_one_hash(self):
        tokens = [token_row(time_stamp=1_700_999_999)]  # normal B says 1_700_000_100
        with pytest.raises(DecodeError):
            decode_account(CHAIN, ACCT_ID, ACCOUNT, fixture_normal(), tokens)

    def test_normal_record_touching_neither_side(self):
        rogue = normal_row(
            tx_hash="0x" + "ab" * 32, from_address=FROM_99, to_address=STRANGER
        )
        with pytest.raises(DecodeError):
            decode_account(CHAIN, ACCT_ID, ACCOUNT, [rogue], [])

    def test_token_record_touching_neither_side(self):
        rogue = token_row(from_address=FROM_99, to_address=STRANGER)
        with pytest.raises(DecodeError):
            decode_account(CHAIN, ACCT_ID, ACCOUNT, [], [rogue])

    def test_unknown_chain_propagates(self):
        with pytest.raises(UnknownChainError):
            decode_account("eip155:424242", ACCT_ID, ACCOUNT, [normal_row()], [])


class TestEntryNormalization:
    def test_address_is_lowercased_on_entry(self):
        account = "0x" + "ab" * 20  # records carry the lowercase form
        normal = [normal_row(to_address=account)]
        out = decode_account(CHAIN, ACCT_ID, "0x" + "AB" * 20, normal, [])
        assert len(out) == 1
        assert out[0].parts[0].direction is Direction.IN

    def test_registry_argument_supplies_the_native_asset(self):
        registry = ChainRegistry()
        registry.register(
            Chain(
                caip2="eip155:31337",
                family=ChainFamily.EVM,
                name="Localnet",
                native_caip19="eip155:31337/slip44:60",
                native_symbol="ETH",
                native_decimals=18,
            )
        )
        out = decode_account(
            "eip155:31337", ACCT_ID, ACCOUNT, [normal_row()], [], registry=registry
        )
        assert out[0].parts[0].asset_id == "eip155:31337/slip44:60"
        assert out[0].fees[0].asset_id == "eip155:31337/slip44:60"


class TestGasRowAndDirections:
    def test_token_only_hash_uses_first_tokentx_as_gas_row(self):
        airdrop = token_row(
            tx_hash="0x" + "cd" * 32, from_address=FROM_99, to_address=ACCOUNT,
            value_raw=42, gas_used=90_000,
        )
        out = decode_account(CHAIN, ACCT_ID, ACCOUNT, [], [airdrop])
        (txn,) = out
        assert txn.parts[0].direction is Direction.IN
        assert txn.fees == (
            Fee(
                asset_id=ETH,
                quantity=Quantity(900_000_000_000_000, 18),
                value=None,
                act_id="act_0",
                borne_by=BorneBy.COUNTERPARTY,
            ),
        )

    def test_token_only_outgoing_fee_borne_by_self(self):
        (txn,) = decode_account(CHAIN, ACCT_ID, ACCOUNT, [], [token_row()])
        assert txn.fees[0].borne_by is BorneBy.SELF

    def test_self_transfer_direction_and_type(self):
        row = normal_row(from_address=ACCOUNT, to_address=ACCOUNT)
        (txn,) = decode_account(CHAIN, ACCT_ID, ACCOUNT, [row], [])
        assert txn.parts[0].direction is Direction.SELF
        assert txn.type is TxType.SELF
        assert txn.subtype is TxSubtype.TRANSFER

    def test_huge_token_amount_survives_exactly(self):
        whale = token_row(value_raw=10**77, token_decimal=18)
        (txn,) = decode_account(CHAIN, ACCT_ID, ACCOUNT, [], [whale])
        assert txn.parts[0].quantity == Quantity(10**77, 18)
