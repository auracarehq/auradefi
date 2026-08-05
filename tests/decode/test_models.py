"""Contract tests for auradefi.decode.models (SPEC §4.4; rules #1 #2 #4 #7).

The transaction-id literals below were derived INDEPENDENTLY of the code
under test, via ``python3 -c`` over the algorithm pinned in
docs/internal/DECISIONS.md:

    "txn_" + sha256(f"{chain_id}|{tx_hash}|{account_id}".encode()).hexdigest()[:16]

A stability contract is a hardcoded string, not a call to the function
under test. The decode↔ledger cross-pinning of these vectors lives in
tests/ledger/test_bridge.py (DECISIONS.md "Duplication waiver").
"""

from __future__ import annotations

import dataclasses
import inspect
from dataclasses import FrozenInstanceError

import pytest

from auradefi.decode.models import (
    Act,
    BorneBy,
    DataQuality,
    Direction,
    Fee,
    MetaType,
    Part,
    Transaction,
    TxStatus,
    TxSubtype,
    TxType,
    act_id_for,
    derive_tx_type,
    transaction_id,
)
from auradefi.errors import ValidationError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity

# Derived independently (see module docstring); NEVER regenerate from the
# implementation.
GOLDEN_ID_MAINNET = "txn_8960436486a11960"  # eip155:1 | 0xabc | acct_1
GOLDEN_ID_ACCT_2 = "txn_96e39b11221dd121"  # eip155:1 | 0xabc | acct_2
GOLDEN_ID_CHAIN_137 = "txn_29df63af5ae2a213"  # eip155:137 | 0xabc | acct_1
GOLDEN_ID_HASH_DEF = "txn_728c1582b3e16304"  # eip155:1 | 0xdef | acct_1

MS = 1_754_000_000_000  # ms-epoch, matches the repo's frozen clock era
ETH = "eip155:1/slip44:60"
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def make_part(**overrides) -> Part:
    fields = {
        "act_id": "act_0",
        "direction": Direction.IN,
        "asset_id": ETH,
        "quantity": Quantity(10**18, 18),
        "value": None,
        "price": None,
        "from_address": "0xc0ffee",
        "to_address": "0xaccount",
    }
    fields.update(overrides)
    return Part(**fields)


def make_fee(**overrides) -> Fee:
    fields = {
        "asset_id": ETH,
        "quantity": Quantity(210_000_000_000_000, 18),
        "value": None,
        "act_id": "act_0",
        "borne_by": BorneBy.SELF,
    }
    fields.update(overrides)
    return Fee(**fields)


def make_data_quality(**overrides) -> DataQuality:
    fields = {
        "incomplete": (),
        "confidence": 1.0,
        "decoder_version": 1,
        "sources": ("etherscan",),
    }
    fields.update(overrides)
    return DataQuality(**fields)


def make_txn(**overrides) -> Transaction:
    """Local factory (duplicated per test module: tests/conftest.py and
    tests/ledger/conftest.py are outside this order's ownership)."""
    fields = {
        "id": GOLDEN_ID_MAINNET,
        "chain_id": "eip155:1",
        "tx_hash": "0xabc",
        "account_id": "acct_1",
        "status": TxStatus.CONFIRMED,
        "block_number": 19_000_000,
        "initiated_at": MS,
        "confirmed_at": MS,
        "subtype": TxSubtype.TRANSFER,
        "parts": (make_part(),),
        "fees": (make_fee(),),
        "acts": (Act("act_0", TxSubtype.TRANSFER),),
        "protocol": None,
        "decoder_version": 1,
        "data_quality": make_data_quality(),
    }
    fields.update(overrides)
    return Transaction(**fields)


class TestEnums:
    def test_tx_status_members_and_wire_values(self):
        assert [m.value for m in TxStatus] == [
            "pending", "confirmed", "failed", "reverted", "replaced", "dropped",
        ]

    def test_tx_type_members_and_wire_values(self):
        assert [m.value for m in TxType] == [
            "send", "receive", "trade", "self", "interaction",
        ]

    def test_tx_subtype_phase3_subset(self):
        assert [m.value for m in TxSubtype] == [
            "transfer", "swap", "approve", "fee", "unknown",
        ]

    def test_meta_type_members_verbatim_spec_4_3(self):
        assert [m.value for m in MetaType] == [
            "wallet", "supplied", "borrowed", "claimable", "vesting",
            "locked", "nft",
        ]

    def test_borne_by_members(self):
        assert [m.value for m in BorneBy] == ["self", "counterparty"]

    def test_direction_members_and_wire_values(self):
        assert [m.value for m in Direction] == ["in", "out", "self"]

    def test_enums_are_str_and_str_of_member_is_the_value(self):
        assert str(Direction.IN) == "in"
        assert str(Direction.OUT) == "out"
        assert str(Direction.SELF) == "self"
        assert str(TxStatus.FAILED) == "failed"
        assert str(BorneBy.COUNTERPARTY) == "counterparty"
        assert Direction.IN == "in"
        assert isinstance(Direction.IN, str)
        assert isinstance(TxType.TRADE, str)


class TestTransactionId:
    def test_pinned_golden_vector(self):
        assert transaction_id("eip155:1", "0xabc", "acct_1") == GOLDEN_ID_MAINNET

    def test_every_component_is_identity_bearing(self):
        assert transaction_id("eip155:1", "0xabc", "acct_2") == GOLDEN_ID_ACCT_2
        assert transaction_id("eip155:137", "0xabc", "acct_1") == GOLDEN_ID_CHAIN_137
        assert transaction_id("eip155:1", "0xdef", "acct_1") == GOLDEN_ID_HASH_DEF
        ids = {
            GOLDEN_ID_MAINNET,
            GOLDEN_ID_ACCT_2,
            GOLDEN_ID_CHAIN_137,
            GOLDEN_ID_HASH_DEF,
        }
        assert len(ids) == 4

    def test_shape_is_txn_plus_16_hex_chars(self):
        txn_id = transaction_id("eip155:1", "0xabc", "acct_1")
        assert txn_id.startswith("txn_")
        suffix = txn_id.removeprefix("txn_")
        assert len(suffix) == 16
        assert set(suffix) <= set("0123456789abcdef")


class TestActIdFor:
    def test_zero(self):
        assert act_id_for(0) == "act_0"

    def test_positional(self):
        assert act_id_for(1) == "act_1"
        assert act_id_for(12) == "act_12"


class TestDeriveTxType:
    def test_empty_is_interaction(self):
        assert derive_tx_type(()) is TxType.INTERACTION

    def test_all_in_is_receive(self):
        assert derive_tx_type((make_part(direction=Direction.IN),)) is TxType.RECEIVE
        both_in = (
            make_part(direction=Direction.IN),
            make_part(direction=Direction.IN, asset_id=USDC),
        )
        assert derive_tx_type(both_in) is TxType.RECEIVE

    def test_all_out_is_send(self):
        both_out = (
            make_part(direction=Direction.OUT),
            make_part(direction=Direction.OUT, asset_id=USDC),
        )
        assert derive_tx_type(both_out) is TxType.SEND

    def test_all_self_is_self(self):
        assert derive_tx_type((make_part(direction=Direction.SELF),)) is TxType.SELF

    def test_any_mixture_is_trade(self):
        assert derive_tx_type(
            (make_part(direction=Direction.IN), make_part(direction=Direction.OUT))
        ) is TxType.TRADE
        assert derive_tx_type(
            (make_part(direction=Direction.IN), make_part(direction=Direction.SELF))
        ) is TxType.TRADE
        assert derive_tx_type(
            (make_part(direction=Direction.OUT), make_part(direction=Direction.SELF))
        ) is TxType.TRADE


class TestTypeIsADerivedProperty:
    def test_type_is_not_a_stored_field(self):
        assert "type" not in {f.name for f in dataclasses.fields(Transaction)}

    def test_type_is_a_property(self):
        assert isinstance(inspect.getattr_static(Transaction, "type"), property)

    def test_type_follows_the_parts_bag(self):
        assert make_txn().type is TxType.RECEIVE
        swap = make_txn(
            parts=(
                make_part(direction=Direction.OUT),
                make_part(direction=Direction.IN, asset_id=USDC),
            )
        )
        assert swap.type is TxType.TRADE

    def test_fee_only_transaction_is_interaction(self):
        # Fees are siblings, structurally excluded from type derivation.
        failed = make_txn(status=TxStatus.FAILED, parts=())
        assert failed.type is TxType.INTERACTION


class TestActBackReferences:
    def test_dangling_part_act_id_raises(self):
        with pytest.raises(ValidationError):
            make_txn(parts=(make_part(act_id="act_9"),))

    def test_dangling_fee_act_id_raises(self):
        with pytest.raises(ValidationError):
            make_txn(fees=(make_fee(act_id="act_9"),))

    def test_none_act_id_is_accepted(self):
        txn = make_txn(
            parts=(make_part(act_id=None),),
            fees=(make_fee(act_id=None),),
            acts=(),
        )
        assert txn.parts[0].act_id is None
        assert txn.fees[0].act_id is None

    def test_valid_back_references_are_accepted(self):
        txn = make_txn()
        assert txn.parts[0].act_id == "act_0"
        assert txn.acts[0].act_id == "act_0"


class TestDecoderVersionAgreement:
    def test_mismatch_raises(self):
        with pytest.raises(ValidationError):
            make_txn(
                decoder_version=1,
                data_quality=make_data_quality(decoder_version=2),
            )

    def test_matching_versions_are_accepted(self):
        txn = make_txn()
        assert txn.decoder_version == txn.data_quality.decoder_version == 1


class TestImmutability:
    def test_all_model_classes_are_frozen_slots_dataclasses(self):
        for cls in (Part, Fee, Act, DataQuality, Transaction):
            assert dataclasses.is_dataclass(cls)
            assert cls.__dataclass_params__.frozen is True
            assert "__slots__" in cls.__dict__

    def test_part_is_frozen(self):
        part = make_part()
        with pytest.raises(FrozenInstanceError):
            part.asset_id = USDC

    def test_fee_is_frozen(self):
        fee = make_fee()
        with pytest.raises(FrozenInstanceError):
            fee.borne_by = BorneBy.COUNTERPARTY

    def test_act_is_frozen(self):
        act = Act("act_0", TxSubtype.SWAP)
        with pytest.raises(FrozenInstanceError):
            act.subtype = TxSubtype.TRANSFER

    def test_data_quality_is_frozen(self):
        quality = make_data_quality()
        with pytest.raises(FrozenInstanceError):
            quality.confidence = 0.5

    def test_transaction_is_frozen(self):
        txn = make_txn()
        with pytest.raises(FrozenInstanceError):
            txn.status = TxStatus.DROPPED


class TestShapeAndDefaults:
    def test_part_defaults(self):
        part = make_part()
        assert part.meta_type is None
        assert part.other_parties == ()

    def test_part_asset_id_is_caip19_never_ast(self):
        part = make_part()
        assert "/" in part.asset_id
        assert not part.asset_id.startswith("ast_")

    def test_part_carries_exact_quantity_and_optional_money(self):
        priced = make_part(
            value=Money(Quantity(10**18, 18).as_decimal(), "USD"),
            price=None,
        )
        assert priced.quantity == Quantity(10**18, 18)
        assert priced.value == Money(Quantity(10**18, 18).as_decimal(), "USD")

    def test_act_protocol_defaults_to_none(self):
        assert Act("act_0", TxSubtype.SWAP).protocol is None

    def test_transaction_collections_are_tuples(self):
        txn = make_txn()
        assert isinstance(txn.parts, tuple)
        assert isinstance(txn.fees, tuple)
        assert isinstance(txn.acts, tuple)

    def test_timestamps_are_ms_epoch_ints(self):
        txn = make_txn()
        assert isinstance(txn.initiated_at, int)
        assert isinstance(txn.confirmed_at, int)
        assert txn.initiated_at == MS

    def test_pending_transaction_nullables(self):
        pending = make_txn(
            status=TxStatus.PENDING, block_number=None, confirmed_at=None
        )
        assert pending.block_number is None
        assert pending.confirmed_at is None

    def test_huge_quantity_survives_exactly(self):
        colossal = make_txn(parts=(make_part(quantity=Quantity(10**77, 18)),))
        assert colossal.parts[0].quantity.raw == 10**77
