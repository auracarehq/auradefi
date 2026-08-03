"""Solana constants and base58 address validation (SPEC §4.2).

The base58 alphabet excludes 0, O, I and l; addresses are 32..44 chars.
Golden addresses: the system program (32 ones) and the mainnet USDC mint.
"""

from __future__ import annotations

import pytest

from auradefi.chains import solana
from auradefi.errors import ValidationError

SYSTEM_PROGRAM = "11111111111111111111111111111111"  # 32 chars, minimum length
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # 44 chars, maximum
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def test_mainnet_caip2_is_pinned():
    assert solana.MAINNET == "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"


def test_slip44_coin_type_is_501():
    assert solana.SLIP44 == 501
    assert isinstance(solana.SLIP44, int)


def test_native_asset_caip19_composes_from_the_constants():
    assert (
        f"{solana.MAINNET}/slip44:{solana.SLIP44}"
        == "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/slip44:501"
    )


def test_mainnet_reference_is_itself_a_valid_address_prefix():
    # The CAIP-2 reference is the genesis hash truncated to 32 base58 chars,
    # so it must satisfy our own validator at the minimum length.
    reference = solana.MAINNET.partition(":")[2]
    assert len(reference) == 32
    solana.validate_address(reference)


# --- validate_address: happy path ---------------------------------------


@pytest.mark.parametrize(
    "good",
    [
        SYSTEM_PROGRAM,  # length 32 boundary
        USDC_MINT,  # length 44 boundary
        "z" * 32,
        "9" * 44,
        BASE58_ALPHABET[:37],  # every digit+upper region chunk, length 37
    ],
)
def test_validate_address_accepts_valid_base58(good):
    solana.validate_address(good)  # must not raise


def test_validate_address_accepts_every_alphabet_character():
    # 58-char alphabet does not fit one address; check it in two halves.
    solana.validate_address(BASE58_ALPHABET[:29] + BASE58_ALPHABET[:8])
    solana.validate_address(BASE58_ALPHABET[29:] + BASE58_ALPHABET[-8:])


# --- validate_address: rejections ----------------------------------------


@pytest.mark.parametrize("banned", ["0", "O", "I", "l"])
def test_validate_address_rejects_the_four_banned_characters(banned):
    candidate = SYSTEM_PROGRAM[:-1] + banned  # still length 32
    with pytest.raises(ValidationError):
        solana.validate_address(candidate)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "1" * 31,  # one under minimum
        "1" * 45,  # one over maximum
        "z",
        "E" * 100,
        SYSTEM_PROGRAM + "!",  # non-alphabet punctuation
        SYSTEM_PROGRAM[:-1] + "+",
        SYSTEM_PROGRAM[:-1] + " ",
        " " + USDC_MINT[1:],
        "0x" + "a" * 40,  # an EVM address is not a Solana address
    ],
)
def test_validate_address_rejects_wrong_length_or_charset(bad):
    with pytest.raises(ValidationError):
        solana.validate_address(bad)
