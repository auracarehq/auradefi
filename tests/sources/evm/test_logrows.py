"""The ``eth_getLogs`` row grammar, strictly (RELEASE_0.2.0 §4).

Moved here with ``logrows.py`` when ``logs.py`` split at its line cap.
These are the tests about what a row IS; the ones about which rows get
requested stayed with the scanner next door.

The refusal set is the load-bearing part. A log is evidence a decoder
attributes value from, so every missing field, non-hex quantity and
short topic gets its own assertion rather than a degraded row.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from auradefi.errors import SourceError
from auradefi.sources.evm.logrows import LogRecord, _record

# The row literals, shared verbatim with test_logs.py. Duplicated on
# purpose: a golden row is the thing under test here, and importing it
# from a sibling test file would make one file's edit silently move the
# other's expectations.
URL = "https://node.example.invalid/v1"

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_CHECKSUMMED = "0xA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"
DAI_CHECKSUMMED = "0x6B175474E89094C44DA98B954EEDEAC495271D0F"
VITALIK = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"

#: keccak256("Transfer(address,address,uint256)"), the topic0 every ERC-20
#: transfer carries and the reason this module exists.
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

#: An indexed address topic: 12 zero bytes then the 20-byte address.
PAD_VITALIK = "0x000000000000000000000000d8da6bf26964af9d7eed9e03e53415d37aa96045"
PAD_USDC = "0x000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
PAD_USDC_CHECKSUMMED = (
    "0x000000000000000000000000A0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
)

TX = "0x9a8f1c2b3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8"
TX_LOUD = "0x9A8F1C2B3D4E5F60718293A4B5C6D7E8F90A1B2C3D4E5F60718293A4B5C6D7E8"

#: 1,000,000 as a 32-byte big-endian word, which is 1 USDC at 6 decimals.
DATA_ONE_MILLION = (
    "0x00000000000000000000000000000000000000000000000000000000000f4240"
)

#: The same word as bytes, written as its padding plus its three
#: significant bytes so a reader can check it by eye: 0x0f4240 is
#: 1,000,000, and 29 + 3 is 32.
DATA_ONE_MILLION_BYTES = bytes(29) + b"\x0f\x42\x40"

GOLDEN_BLOCK = 20_450_000

#: One well-formed row, deliberately loud where the contract says the
#: typed record is quiet: the address, the transaction hash and the third
#: topic are all checksummed on the wire and lowercase in the record. It
#: also carries a key nobody models and NO "removed" key, which is the
#: absent-reads-False branch.
BASE_ROW = {
    "address": USDC_CHECKSUMMED,
    "topics": [TRANSFER, PAD_VITALIK, PAD_USDC_CHECKSUMMED],
    "data": DATA_ONE_MILLION,
    "blockNumber": "0x1380ad0",
    "transactionHash": TX_LOUD,
    "logIndex": "0x2a",
    "aFieldNobodyModels": {"nested": [1, 2, 3]},
}

GOLDEN_RECORD = LogRecord(
    address=USDC,
    topics=(TRANSFER, PAD_VITALIK, PAD_USDC),
    data=DATA_ONE_MILLION_BYTES,
    block_number=GOLDEN_BLOCK,
    transaction_hash=TX,
    log_index=42,
    removed=False,
)

#: Sentinel for :func:`_row`: this key is DELETED, not overwritten.
ABSENT = object()



def _row(**overrides: object) -> dict:
    """:data:`BASE_ROW` with keys replaced, or removed via :data:`ABSENT`."""
    row = dict(BASE_ROW)
    for key, value in overrides.items():
        if value is ABSENT:
            row.pop(key, None)
        else:
            row[key] = value
    return row


class TestLogRecordShape:
    # pins: the field set is exactly the seven phases 13 and 14 consume, in
    #       order, and carries NO timestamp: eth_getLogs returns no time
    #       and rule #3 leaves no room for a half-populated one
    def test_the_field_set_is_the_seven_with_no_timestamp(self):
        assert [field.name for field in dataclasses.fields(LogRecord)] == [
            "address",
            "topics",
            "data",
            "block_number",
            "transaction_hash",
            "log_index",
            "removed",
        ]

    # pins: LogRecord is FROZEN, so a consumer cannot edit a scanned row in
    #       place and pass the edit on as something a node said
    def test_a_log_record_refuses_attribute_assignment(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            GOLDEN_RECORD.block_number = 1

    # pins: LogRecord uses SLOTS, so it carries no instance dict and a
    #       misspelled field is a refusal instead of a silent extra
    def test_a_log_record_uses_slots(self):
        assert "__slots__" in vars(LogRecord)
        assert not hasattr(GOLDEN_RECORD, "__dict__")

    # pins: an equal record hashes equal, which tuple topics and bytes data
    #       make possible and a list or bytearray would not
    def test_an_equal_record_hashes_equal(self):
        twin = LogRecord(
            address=USDC,
            topics=(TRANSFER, PAD_VITALIK, PAD_USDC),
            data=DATA_ONE_MILLION_BYTES,
            block_number=GOLDEN_BLOCK,
            transaction_hash=TX,
            log_index=42,
            removed=False,
        )
        assert hash(twin) == hash(GOLDEN_RECORD)
        assert twin == GOLDEN_RECORD




# pins: importing the module opens no socket and adds no import edge out of
#       the sources layer, and it consumes the wave-1 rpc module instead of
#       carrying a second copy of the block tag and the transport
