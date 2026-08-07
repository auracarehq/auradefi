"""The JSON-RPC envelope vocabulary: block tags and batch answers.

Moved here with ``jsonrpc.py`` when ``rpc.py`` split at its line cap.
These are the tests that need no transport at all, which is exactly the
property the split was made to expose: a block tag and a batch answer are
values, and nothing about them requires a client in scope.

``block_tag`` is the shared one. ``logs.py``, ``multicall.py`` and
``reader.py`` all pin their reads with it, so a second spelling of a
block parameter would be two wire identities for one height.
"""

from __future__ import annotations

import dataclasses

import pytest

from auradefi.errors import ValidationError
from auradefi.sources.evm.jsonrpc import BatchResult, block_tag

WORD_SIX = "0x0000000000000000000000000000000000000000000000000000000000000006"

class TestBatchResultCarrier:
    # pins: BatchResult is frozen and slotted, so a caller cannot rewrite a
    #       declared failure into a success after the fact
    def test_batch_result_is_frozen_with_slots(self):
        carrier = BatchResult(WORD_SIX, None)
        assert carrier.result == WORD_SIX
        assert carrier.error is None
        with pytest.raises(dataclasses.FrozenInstanceError):
            carrier.result = "0x0"  # type: ignore[misc]
        assert not hasattr(carrier, "__dict__")  # slots=True

    # pins: the field order is (result, error), which is the positional
    #       contract multicall.py's CallResult mirrors
    def test_batch_result_field_order_is_positional_contract(self):
        assert [f.name for f in dataclasses.fields(BatchResult)] == ["result", "error"]

    # pins: a carrier with BOTH members set is refused, so a failure can
    #       never be smuggled alongside a value
    def test_both_members_set_is_a_validation_error(self):
        with pytest.raises(ValidationError):
            BatchResult(WORD_SIX, "execution reverted")

    # pins: a carrier with NEITHER member set is refused, so an unanswered
    #       item can never pass as an empty success
    def test_neither_member_set_is_a_validation_error(self):
        with pytest.raises(ValidationError):
            BatchResult(None, None)

    # pins: membership is decided by `is not None`, so a falsy but real
    #       result ('' or 0) is a set member and not a missing one
    def test_a_falsy_result_is_still_a_set_member(self):
        assert BatchResult("", None).result == ""
        assert BatchResult(0, None).result == 0
        assert BatchResult([], None).result == []

class TestBlockTag:
    # pins: a None block number is the string 'latest'
    def test_block_tag_of_none_is_latest(self):
        assert block_tag(None) == "latest"

    # pins: block ZERO is '0x0' and not 'latest', so the None test is on
    #       identity and never on truthiness
    def test_block_tag_of_zero_is_the_genesis_tag_not_latest(self):
        assert block_tag(0) == "0x0"

    # pins: a block number is minimal lowercase hex, never zero-padded and
    #       never uppercase
    @pytest.mark.parametrize(
        ("number", "tag"),
        [
            (1, "0x1"),
            (10, "0xa"),
            (255, "0xff"),
            (4096, "0x1000"),
            (20_450_000, "0x1380ad0"),
        ],
    )
    def test_block_tag_is_minimal_lowercase_hex(self, number, tag):
        assert block_tag(number) == tag
