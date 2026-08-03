"""Cursor token contract (SPEC §6.4; DECISIONS pinned: f"{seq:020d}").

Golden token literals are hardcoded from the pinned format, never computed
by calling the code under test.
"""

from __future__ import annotations

import pytest

from auradefi.errors import CursorError
from auradefi.ledger.cursors import decode_cursor, encode_cursor

TWENTY_ZEROS = "00000000000000000000"


class TestEncode:
    def test_zero_is_twenty_ascii_zeros(self):
        assert encode_cursor(0) == TWENTY_ZEROS
        assert encode_cursor(0) == "0" * 20

    @pytest.mark.parametrize(
        ("seq", "token"),
        [
            (1, "00000000000000000001"),
            (99, "00000000000000000099"),
            (123, "00000000000000000123"),
            (1_754_000_000_000, "00000001754000000000"),
            (10**19, "10000000000000000000"),
            (10**20 - 1, "99999999999999999999"),
        ],
    )
    def test_pinned_golden_tokens(self, seq, token):
        assert encode_cursor(seq) == token
        assert len(encode_cursor(seq)) == 20

    def test_lexicographic_order_equals_numeric_order(self):
        # 99 vs 123 is the classic trap: unpadded, "99" > "123".
        seqs = [123, 0, 99, 7, 10**19, 1000, 1, 10**20 - 1, 100]
        tokens = [encode_cursor(seq) for seq in seqs]
        assert sorted(tokens) == [encode_cursor(seq) for seq in sorted(seqs)]
        assert encode_cursor(99) < encode_cursor(123)

    def test_negative_seq_raises_cursor_error(self):
        with pytest.raises(CursorError):
            encode_cursor(-1)


class TestDecode:
    def test_none_means_from_the_start(self):
        assert decode_cursor(None) == 0

    def test_twenty_zeros_decodes_to_zero(self):
        assert decode_cursor(TWENTY_ZEROS) == 0

    @pytest.mark.parametrize(
        ("token", "seq"),
        [
            ("00000000000000000001", 1),
            ("00000000000000000099", 99),
            ("00000000000000000123", 123),
            ("00000001754000000000", 1_754_000_000_000),
            ("99999999999999999999", 10**20 - 1),
        ],
    )
    def test_pinned_golden_decodes(self, token, seq):
        assert decode_cursor(token) == seq

    @pytest.mark.parametrize(
        "seq", [0, 1, 99, 123, 10**19, 10**20 - 1]
    )
    def test_round_trip(self, seq):
        assert decode_cursor(encode_cursor(seq)) == seq

    @pytest.mark.parametrize(
        "token",
        [
            pytest.param("x" * 20, id="letters-right-length"),
            pytest.param("123", id="too-short-unpadded"),
            pytest.param("-" + "0" * 19, id="negative-sign"),
            pytest.param("+" + "0" * 19, id="plus-sign"),
            pytest.param("", id="empty"),
            pytest.param("0" * 19, id="nineteen-digits"),
            pytest.param("0" * 21, id="twentyone-digits"),
            pytest.param(" " + "0" * 19, id="leading-space"),
            pytest.param("0" * 19 + " ", id="trailing-space"),
            pytest.param("0" * 19 + "\n", id="trailing-newline"),
            pytest.param("0" * 10 + "x" + "0" * 9, id="letter-in-middle"),
            pytest.param("١" * 20, id="arabic-indic-digits-isdigit-true"),
            pytest.param("０" * 20, id="fullwidth-digits-isdigit-true"),
            pytest.param("0" * 19 + "٥", id="one-non-ascii-digit"),
            pytest.param("1e3".ljust(20, "0"), id="scientific-notation"),
            pytest.param("0x" + "0" * 18, id="hex-prefix"),
        ],
    )
    def test_anything_not_exactly_20_ascii_digits_raises(self, token):
        with pytest.raises(CursorError):
            decode_cursor(token)
