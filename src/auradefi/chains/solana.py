"""Solana family constants and address validation (SPEC §4.2).

The CAIP-2 reference is the first 32 chars of the genesis hash in base58.
SLIP-44 coin type 501 keys the native-asset CAIP-19 (``solana:.../slip44:501``).
Canonical CAIP-19 keeps base58 case (docs/DECISIONS.md — asset-id pin).
"""

from __future__ import annotations

from auradefi.errors import ValidationError

MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SLIP44 = 501

# The Bitcoin base58 alphabet: no 0, O, I or l.
_BASE58_CHARS = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
_MIN_LENGTH = 32
_MAX_LENGTH = 44


def validate_address(address: str) -> None:
    """Validate a Solana address (a base58-encoded 32-byte key).

    Accepts strings of 32..44 characters drawn from the Bitcoin base58
    alphabet — which excludes ``0``, ``O``, ``I`` and ``l``. Phase 0 checks
    charset and length only; it does not base58-decode to verify the
    payload is exactly 32 bytes.

    Raises:
        ValidationError: on a wrong-length string or any character outside
            the base58 alphabet.
    """
    if not isinstance(address, str) or not _MIN_LENGTH <= len(address) <= _MAX_LENGTH:
        raise ValidationError(
            f"Solana address must be {_MIN_LENGTH}..{_MAX_LENGTH} base58 chars: "
            f"{address!r}"
        )
    if not set(address) <= _BASE58_CHARS:
        raise ValidationError(
            f"Solana address contains non-base58 characters: {address!r}"
        )
