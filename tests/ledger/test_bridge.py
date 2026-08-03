"""Contract tests for auradefi.ledger.bridge (SPEC §6.4) and the
DECISIONS.md "Duplication waiver" cross-pins.

decode.models.transaction_id and decode.models.Direction are deliberate
value-identical duplicates of ledger.models — the layer contract forbids
decode→ledger imports. This module pins BOTH sides to the same hardcoded
golden bytes, derived INDEPENDENTLY via ``python3 -c`` over the pinned
algorithm:

    "txn_" + sha256(f"{chain_id}|{tx_hash}|{account_id}".encode()).hexdigest()[:16]

so drift between the duplicates is a red test, not a debate.
"""

from __future__ import annotations

import pytest

from auradefi.decode.models import (
    Act,
    BorneBy,
    DataQuality,
    Fee,
    Part,
    Transaction,
    TxStatus,
    TxSubtype,
)
from auradefi.decode.models import Direction as DecodeDirection
from auradefi.decode.models import transaction_id as decode_transaction_id
from auradefi.ledger.bridge import to_ledger_transaction
from auradefi.ledger.models import (
    Direction as LedgerDirection,
    LedgerTransaction,
)
from auradefi.ledger.models import transaction_id as ledger_transaction_id
from auradefi.money.quantity import Quantity

# Derived independently (see module docstring); NEVER regenerate from
# either implementation.
GOLDEN_ID_MAINNET = "txn_8960436486a11960"  # eip155:1 | 0xabc | acct_1
GOLDEN_ID_CHAIN_137 = "txn_29df63af5ae2a213"  # eip155:137 | 0xabc | acct_1

MS = 1_754_000_000_000  # ms-epoch, matches the repo's frozen clock era
ETH = "eip155:1/slip44:60"
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def make_part(**overrides) -> Part:
    fields = {
        "act_id": "act_0",
        "direction": DecodeDirection.IN,
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
        "borne_by": BorneBy.COUNTERPARTY,
    }
    fields.update(overrides)
    return Fee(**fields)


def make_rich(**overrides) -> Transaction:
    """One inbound 1-ETH part plus a counterparty gas fee on act_0."""
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
        "data_quality": DataQuality(
            incomplete=(),
            confidence=1.0,
            decoder_version=1,
            sources=("etherscan",),
        ),
    }
    fields.update(overrides)
    return Transaction(**fields)


class TestDuplicationWaiverGoldenVectors:
    """DECISIONS.md: both transaction_id duplicates pinned to the same bytes."""

    def test_decode_id_matches_the_phase0_golden_vector(self):
        assert decode_transaction_id("eip155:1", "0xabc", "acct_1") == GOLDEN_ID_MAINNET

    def test_both_implementations_agree_on_the_mainnet_vector(self):
        vector = ("eip155:1", "0xabc", "acct_1")
        assert decode_transaction_id(*vector) == ledger_transaction_id(*vector)
        assert decode_transaction_id(*vector) == GOLDEN_ID_MAINNET

    def test_both_implementations_agree_on_the_polygon_vector(self):
        vector = ("eip155:137", "0xabc", "acct_1")
        assert decode_transaction_id(*vector) == ledger_transaction_id(*vector)
        assert decode_transaction_id(*vector) == GOLDEN_ID_CHAIN_137

    def test_direction_duplicate_is_value_identical(self):
        assert [m.value for m in DecodeDirection] == [
            m.value for m in LedgerDirection
        ]
        assert [m.name for m in DecodeDirection] == [
            m.name for m in LedgerDirection
        ]
        assert [m.value for m in DecodeDirection] == ["in", "out", "self"]


class TestVerbatimCarry:
    def test_identity_and_timing_fields_carry_verbatim(self):
        ledger_txn = to_ledger_transaction(make_rich())
        assert ledger_txn.id == GOLDEN_ID_MAINNET
        assert ledger_txn.chain_id == "eip155:1"
        assert ledger_txn.tx_hash == "0xabc"
        assert ledger_txn.account_id == "acct_1"
        assert ledger_txn.block_number == 19_000_000
        assert ledger_txn.initiated_at == MS
        assert ledger_txn.confirmed_at == MS

    def test_pending_nullables_carry_verbatim(self):
        pending = make_rich(
            status=TxStatus.PENDING, block_number=None, confirmed_at=None
        )
        ledger_txn = to_ledger_transaction(pending)
        assert ledger_txn.block_number is None
        assert ledger_txn.confirmed_at is None

    def test_result_is_a_ledger_transaction_with_bookkeeping_defaults(self):
        ledger_txn = to_ledger_transaction(make_rich())
        assert isinstance(ledger_txn, LedgerTransaction)
        assert ledger_txn.removed is False
        assert ledger_txn.last_modified_seq == 0


class TestPartsBecomeEntries:
    def test_one_part_one_counterparty_fee_yields_exactly_one_entry(self):
        ledger_txn = to_ledger_transaction(make_rich())
        assert len(ledger_txn.entries) == 1
        entry = ledger_txn.entries[0]
        assert entry.asset_id == ETH
        assert entry.quantity == Quantity(10**18, 18)
        assert entry.direction is LedgerDirection.IN
        assert type(entry.direction) is LedgerDirection

    def test_entries_preserve_parts_order(self):
        swap = make_rich(
            subtype=TxSubtype.SWAP,
            parts=(
                make_part(direction=DecodeDirection.OUT),
                make_part(
                    direction=DecodeDirection.IN,
                    asset_id=USDC,
                    quantity=Quantity(3_500_000_000, 6),
                ),
            ),
            acts=(Act("act_0", TxSubtype.SWAP),),
        )
        entries = to_ledger_transaction(swap).entries
        assert len(entries) == 2
        assert entries[0].asset_id == ETH
        assert entries[0].direction is LedgerDirection.OUT
        assert entries[1].asset_id == USDC
        assert entries[1].quantity == Quantity(3_500_000_000, 6)
        assert entries[1].direction is LedgerDirection.IN

    def test_self_direction_maps_by_value(self):
        rich = make_rich(parts=(make_part(direction=DecodeDirection.SELF),))
        entries = to_ledger_transaction(rich).entries
        assert entries[0].direction is LedgerDirection.SELF


class TestFeesNeverBecomeEntries:
    def test_failed_transaction_with_only_a_fee_bridges_to_zero_entries(self):
        failed = make_rich(status=TxStatus.FAILED, parts=())
        ledger_txn = to_ledger_transaction(failed)
        assert ledger_txn.entries == ()

    def test_self_borne_fees_produce_no_entries_either(self):
        rich = make_rich(
            parts=(),
            fees=(
                make_fee(borne_by=BorneBy.SELF),
                make_fee(
                    borne_by=BorneBy.SELF,
                    asset_id=USDC,
                    quantity=Quantity(1_000_000, 6),
                ),
            ),
        )
        assert to_ledger_transaction(rich).entries == ()

    def test_fee_asset_never_leaks_into_a_parts_entry(self):
        swap_fee = make_rich(
            fees=(make_fee(asset_id=USDC, quantity=Quantity(1_000_000, 6)),)
        )
        entries = to_ledger_transaction(swap_fee).entries
        assert len(entries) == 1
        assert entries[0].asset_id == ETH


class TestPurity:
    def test_deterministic_over_equal_calls(self):
        rich = make_rich()
        assert to_ledger_transaction(rich) == to_ledger_transaction(rich)

    def test_equal_inputs_give_equal_outputs(self):
        assert to_ledger_transaction(make_rich()) == to_ledger_transaction(
            make_rich()
        )

    def test_input_is_never_mutated(self):
        rich = make_rich()
        twin = make_rich()
        to_ledger_transaction(rich)
        assert rich == twin
        assert rich.parts == twin.parts
        assert rich.fees == twin.fees


class TestBridgeRaisesNothingOfItsOwn:
    def test_zero_part_interaction_bridges_cleanly(self):
        interaction = make_rich(
            parts=(),
            fees=(),
            acts=(),
            subtype=TxSubtype.UNKNOWN,
        )
        ledger_txn = to_ledger_transaction(interaction)
        assert ledger_txn.entries == ()
        assert ledger_txn.id == GOLDEN_ID_MAINNET

    def test_huge_quantities_survive_the_projection_exactly(self):
        colossal = make_rich(parts=(make_part(quantity=Quantity(10**77, 18)),))
        entries = to_ledger_transaction(colossal).entries
        assert entries[0].quantity == Quantity(10**77, 18)
        assert entries[0].quantity.raw == 10**77


@pytest.mark.parametrize(
    ("chain_id", "tx_hash", "account_id", "expected"),
    [
        ("eip155:1", "0xabc", "acct_1", GOLDEN_ID_MAINNET),
        ("eip155:137", "0xabc", "acct_1", GOLDEN_ID_CHAIN_137),
    ],
)
def test_transaction_id_duplicates_are_byte_identical(
    chain_id, tx_hash, account_id, expected
):
    assert decode_transaction_id(chain_id, tx_hash, account_id) == expected
    assert ledger_transaction_id(chain_id, tx_hash, account_id) == expected
