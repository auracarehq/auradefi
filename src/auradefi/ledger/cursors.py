"""Sync cursor tokens (SPEC §6.4; DECISIONS pinned).

A cursor is opaque to callers. Internally it is a backend-monotonic
last-modified sequence serialised as ``f"{seq:020d}"`` so that
lexicographic order over tokens equals numeric order over sequences.
Malformed tokens raise ``auradefi.errors.CursorError``, never a silent
restart from zero.
"""

from __future__ import annotations

from auradefi.errors import CursorError

_TOKEN_LENGTH = 20
_ASCII_DIGITS = frozenset("0123456789")


def encode_cursor(seq: int) -> str:
    """``f"{seq:020d}"``: exactly 20 ASCII digits, zero-padded.

    ``seq`` must be an int ``>= 0``; a negative sequence raises
    ``auradefi.errors.CursorError`` (a signed token could never satisfy
    :func:`decode_cursor` and would break lexicographic ordering).
    """
    if seq < 0:
        raise CursorError(f"cursor sequence must be >= 0, got {seq}")
    return f"{seq:020d}"


def decode_cursor(token: str | None) -> int:
    """Inverse of :func:`encode_cursor`; ``None`` means "from the start" (0).

    Raises ``auradefi.errors.CursorError`` for anything that is not exactly
    20 ASCII digits ``0-9``: wrong length, signs, whitespace, and
    non-ASCII digit codepoints included.
    """
    if token is None:
        return 0
    if len(token) != _TOKEN_LENGTH or not _ASCII_DIGITS.issuperset(token):
        raise CursorError(
            f"cursor token must be exactly {_TOKEN_LENGTH} ASCII digits: {token!r}"
        )
    return int(token)
