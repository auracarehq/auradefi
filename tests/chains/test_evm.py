"""EVM helpers: CAIP-2 <-> chain id, address hygiene (SPEC §4.2).

EIP-55 checksum validation is deliberately absent (needs keccak-256, which
the stdlib lacks — Phase 0 is stdlib only), so normalize_address must accept
ANY casing of valid hex, including casings that are invalid under EIP-55.
"""

from __future__ import annotations

import pytest

from auradefi.chains.evm import (
    caip2_from_chain_id,
    chain_id_from_caip2,
    is_address,
    normalize_address,
)
from auradefi.errors import CaipParseError, ValidationError

# EIP-55 checksummed form of vitalik.eth's address — golden vector for
# "checksum casing survives lowercasing, is never validated".
VITALIK_CHECKSUM = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
VITALIK_LOWER = "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"


# --- caip2_from_chain_id -------------------------------------------------


def test_caip2_from_chain_id_golden_vectors():
    assert caip2_from_chain_id(1) == "eip155:1"
    assert caip2_from_chain_id(137) == "eip155:137"
    assert caip2_from_chain_id(8453) == "eip155:8453"


def test_caip2_from_chain_id_has_no_ceiling():
    huge = 10**77
    assert caip2_from_chain_id(huge) == f"eip155:{huge}"


@pytest.mark.parametrize("bad", [0, -1, -8453, -(10**77)])
def test_caip2_from_chain_id_rejects_non_positive(bad):
    with pytest.raises(ValidationError):
        caip2_from_chain_id(bad)


# --- chain_id_from_caip2 -------------------------------------------------


def test_chain_id_from_caip2_golden_vectors():
    assert chain_id_from_caip2("eip155:1") == 1
    assert chain_id_from_caip2("eip155:8453") == 8453
    assert chain_id_from_caip2("eip155:137") == 137


def test_caip2_round_trips_both_ways():
    for chain_id in (1, 137, 8453, 42161, 10**77):
        assert chain_id_from_caip2(caip2_from_chain_id(chain_id)) == chain_id
    for caip2 in ("eip155:1", "eip155:8453"):
        assert caip2_from_chain_id(chain_id_from_caip2(caip2)) == caip2


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "eip155",
        "eip155:",
        ":1",
        "eip155:abc",
        "eip155:1:2",
        "eip155:1.5",
        "eip155:-1",
        "eip155:+1",  # int('+1') parses in Python; the canonical form must not
        "eip155:0",  # chain ids are positive; 0 never round-trips
        "eip155:01",  # leading zero is non-canonical, breaks round-trip
        "eip155: 1",
        " eip155:1",
        "eip155:1 ",
        "EIP155:1",
        "ethereum",  # the name zoo stays dead
        "eth-mainnet",
        "1",
        "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",  # wrong namespace here
        "bip122:000000000019d6689c085ae165831e93",
    ],
)
def test_chain_id_from_caip2_rejects_non_canonical(bad):
    with pytest.raises(CaipParseError):
        chain_id_from_caip2(bad)


def test_caip_parse_error_is_a_validation_error():
    # errors.py taxonomy: CaipParseError subclasses ValidationError, so a
    # host catching ValidationError at the boundary catches parse failures.
    with pytest.raises(ValidationError):
        chain_id_from_caip2("not-a-caip2")


# --- normalize_address ---------------------------------------------------


def test_normalize_address_lowercases_uppercase_hex():
    assert (
        normalize_address("0xD8DA6BF26964AF9D7EED9E03E53415D37AA96045")
        == VITALIK_LOWER
    )


def test_normalize_address_is_idempotent_on_lowercase():
    assert normalize_address(VITALIK_LOWER) == VITALIK_LOWER


def test_normalize_address_accepts_eip55_casing_without_validating_it():
    # Valid EIP-55 casing lowercases fine...
    assert normalize_address(VITALIK_CHECKSUM) == VITALIK_LOWER
    # ...and so does a casing that is INVALID under EIP-55 (first hex nibble
    # flipped to the wrong case): checksum is documented as not checked.
    invalid_checksum_casing = "0xD8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    assert normalize_address(invalid_checksum_casing) == VITALIK_LOWER


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "0x",
        "d8da6bf26964af9d7eed9e03e53415d37aa96045",  # no 0x prefix
        "0Xd8da6bf26964af9d7eed9e03e53415d37aa96045",  # prefix is literally 0x
        "0xd8da6bf26964af9d7eed9e03e53415d37aa9604",  # 39 hex chars
        "0xd8da6bf26964af9d7eed9e03e53415d37aa960455",  # 41 hex chars
        "0xg8da6bf26964af9d7eed9e03e53415d37aa96045",  # non-hex char
        "0x d8da6bf26964af9d7eed9e03e53415d37aa96045",
        " 0xd8da6bf26964af9d7eed9e03e53415d37aa96045",
        "0xd8da6bf26964af9d7eed9e03e53415d37aa96045 ",
    ],
)
def test_normalize_address_rejects_malformed(bad):
    with pytest.raises(ValidationError):
        normalize_address(bad)


# --- is_address ----------------------------------------------------------


def test_is_address_true_for_valid_any_casing():
    assert is_address(VITALIK_LOWER) is True
    assert is_address(VITALIK_CHECKSUM) is True
    assert is_address("0xD8DA6BF26964AF9D7EED9E03E53415D37AA96045") is True


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "0x",
        "d8da6bf26964af9d7eed9e03e53415d37aa96045",
        "0xd8da6bf26964af9d7eed9e03e53415d37aa9604",
        "0xg8da6bf26964af9d7eed9e03e53415d37aa96045",
        "ethereum",
    ],
)
def test_is_address_false_for_malformed_strings(bad):
    assert is_address(bad) is False


@pytest.mark.parametrize("junk", [None, 1, 0x123, b"0x" + b"a" * 40, ["0x"], object()])
def test_is_address_never_raises_even_on_non_strings(junk):
    assert is_address(junk) is False
