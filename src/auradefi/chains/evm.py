"""EVM family helpers: CAIP-2 <-> numeric chain id, address hygiene (SPEC §4.2).

CAIP-2 is the only chain key — ``eip155:1``, never ``ethereum`` /
``eth-mainnet`` / ``1`` (the vendor name zoo SPEC §4.2 bans). stdlib only.
"""

from __future__ import annotations

import re

from auradefi.errors import CaipParseError, ValidationError

# Canonical eip155 CAIP-2: lowercase namespace, one colon, base-10 reference
# with no sign, no leading zeros (chain ids are positive, so no bare '0').
_EIP155_PATTERN = re.compile(r"eip155:[1-9][0-9]*")

# Literal '0x' prefix followed by exactly 40 hex digits, either case.
_ADDRESS_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}")


def caip2_from_chain_id(chain_id: int) -> str:
    """Return the CAIP-2 identifier ``eip155:{chain_id}`` for a chain id.

    ``chain_id`` must be a positive integer (arbitrary precision — Python
    ints have no ceiling and neither does this function).

    Raises:
        ValidationError: if ``chain_id`` is not a positive integer.
    """
    if isinstance(chain_id, bool) or not isinstance(chain_id, int) or chain_id < 1:
        raise ValidationError(f"chain id must be a positive integer: {chain_id!r}")
    return f"eip155:{chain_id}"


def chain_id_from_caip2(caip2: str) -> int:
    """Parse ``eip155:N`` back into the numeric chain id ``N``.

    Only the canonical form round-trips: lowercase ``eip155`` namespace,
    a single colon, and a base-10 reference with no sign, no leading
    zeros, and no surrounding whitespace.

    Raises:
        CaipParseError: for anything that is not canonical ``eip155:N``.
    """
    if not isinstance(caip2, str) or _EIP155_PATTERN.fullmatch(caip2) is None:
        raise CaipParseError(f"not a canonical eip155 CAIP-2: {caip2!r}")
    return int(caip2.partition(":")[2])


def normalize_address(address: str) -> str:
    """Return the canonical lowercase form of an EVM address.

    Input must be the literal prefix ``0x`` followed by exactly 40 hex
    digits (either case). Output is fully lowercased.

    EIP-55 checksum casing is explicitly NOT validated: verifying it
    requires keccak-256, which the stdlib does not provide (hashlib's
    sha3_256 is the finalised SHA-3, not keccak), and pulling in a hash
    dependency is out of Phase 0 scope (SPEC §11: stdlib only). Mixed-case
    input is therefore accepted and lowercased even when its checksum
    casing would be invalid under EIP-55.

    Raises:
        ValidationError: if ``address`` is not ``0x`` + 40 hex digits.
    """
    if not isinstance(address, str) or _ADDRESS_PATTERN.fullmatch(address) is None:
        raise ValidationError(
            f"not an EVM address ('0x' + 40 hex digits): {address!r}"
        )
    return address.lower()


def is_address(value: object) -> bool:
    """Predicate form of :func:`normalize_address`.

    True iff ``value`` is a string of ``0x`` + 40 hex digits. Never
    raises, whatever the input — including non-string junk.
    """
    return isinstance(value, str) and _ADDRESS_PATTERN.fullmatch(value) is not None
