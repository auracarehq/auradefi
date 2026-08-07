"""CAIP-19 parsing and canonicalization (SPEC §4.2, rule #3).

Canonical strings feed the pinned asset-id hash (docs/internal/DECISIONS.md), so
every expected value here is a hardcoded literal, byte-for-byte.
"""

from __future__ import annotations

import dataclasses

import pytest

from auradefi.assets.caip import Caip19, canonical_caip19, format_caip19, parse_caip19
from auradefi.errors import CaipParseError

SOL_CHAIN = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
BTC_CHAIN = "bip122:000000000019d6689c085ae165831e93"

USDC_ETH_MIXED = "eip155:1/erc20:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_ETH_UPPER = "eip155:1/erc20:0xA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
USDC_ETH = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_SOL_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_SOL = f"{SOL_CHAIN}/token:{USDC_SOL_MINT}"
ETH_NATIVE = "eip155:1/slip44:60"
BTC_NATIVE = f"{BTC_CHAIN}/slip44:0"


# --- Caip19 the dataclass ----------------------------------------------------


def test_caip19_is_frozen():
    parsed = Caip19(chain_id="eip155:1", namespace="slip44", reference="60")
    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.reference = "61"  # type: ignore[misc]


def test_caip19_has_slots_no_instance_dict():
    parsed = Caip19(chain_id="eip155:1", namespace="slip44", reference="60")
    assert not hasattr(parsed, "__dict__")
    assert set(Caip19.__slots__) == {"chain_id", "namespace", "reference"}


def test_caip19_value_equality_by_fields():
    one = Caip19(chain_id="eip155:1", namespace="erc20", reference="0xab" + "0" * 38)
    two = Caip19(chain_id="eip155:1", namespace="erc20", reference="0xab" + "0" * 38)
    assert one == two
    assert one != dataclasses.replace(one, chain_id="eip155:137")


# --- parse: happy paths per namespace ---------------------------------------


def test_parse_erc20_lowercases_the_address():
    parsed = parse_caip19(USDC_ETH_MIXED)
    assert parsed == Caip19(
        chain_id="eip155:1",
        namespace="erc20",
        reference="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    )


def test_parse_erc20_already_lowercase_is_unchanged():
    parsed = parse_caip19(USDC_ETH)
    assert parsed.reference == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def test_parse_erc20_case_variants_parse_identically():
    assert parse_caip19(USDC_ETH_MIXED) == parse_caip19(USDC_ETH_UPPER) == parse_caip19(USDC_ETH)


def test_parse_slip44_ethereum_native():
    parsed = parse_caip19(ETH_NATIVE)
    assert parsed == Caip19(chain_id="eip155:1", namespace="slip44", reference="60")


def test_parse_slip44_zero_is_valid_bitcoin():
    parsed = parse_caip19(BTC_NATIVE)
    assert parsed == Caip19(chain_id=BTC_CHAIN, namespace="slip44", reference="0")


def test_parse_token_preserves_base58_case_exactly():
    parsed = parse_caip19(USDC_SOL)
    assert parsed == Caip19(chain_id=SOL_CHAIN, namespace="token", reference=USDC_SOL_MINT)


def test_parse_token_lowercased_mint_is_a_different_reference():
    # Base58 is case-sensitive: this parses, but is NOT the same asset.
    lowered = parse_caip19(f"{SOL_CHAIN}/token:{USDC_SOL_MINT.lower()}")
    assert lowered.reference == USDC_SOL_MINT.lower()
    assert lowered != parse_caip19(USDC_SOL)


# --- format round-trips -------------------------------------------------------


def test_format_serialises_the_canonical_parts():
    assert format_caip19(Caip19("eip155:1", "slip44", "60")) == ETH_NATIVE


@pytest.mark.parametrize("value", [USDC_ETH_MIXED, USDC_ETH, ETH_NATIVE, BTC_NATIVE, USDC_SOL])
def test_parse_format_parse_round_trips(value):
    parsed = parse_caip19(value)
    assert parse_caip19(format_caip19(parsed)) == parsed


def test_format_of_mixed_case_parse_is_the_lowercase_literal():
    assert format_caip19(parse_caip19(USDC_ETH_MIXED)) == USDC_ETH


# --- canonical_caip19 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        (USDC_ETH_MIXED, USDC_ETH),  # EVM address lowercased
        (USDC_ETH_UPPER, USDC_ETH),
        (USDC_ETH, USDC_ETH),
        (ETH_NATIVE, ETH_NATIVE),  # slip44 untouched
        (BTC_NATIVE, BTC_NATIVE),
        (USDC_SOL, USDC_SOL),  # base58 case preserved, byte-for-byte
    ],
)
def test_canonical_caip19_pinned_literals(value, canonical):
    assert canonical_caip19(value) == canonical


def test_canonical_caip19_is_idempotent():
    once = canonical_caip19(USDC_ETH_MIXED)
    assert canonical_caip19(once) == once


def test_canonical_never_touches_solana_case():
    assert canonical_caip19(USDC_SOL) == USDC_SOL
    assert USDC_SOL_MINT in canonical_caip19(USDC_SOL)


# --- malformed input ------------------------------------------------------------

MALFORMED = [
    "garbage",
    "",
    " ",
    ETH_NATIVE + " ",  # trailing whitespace
    " " + ETH_NATIVE,  # leading whitespace
    "eip155:1",  # no asset part
    "eip155:1/",  # empty asset part
    "eip155:1/erc20",  # no reference separator
    "eip155:1/erc20:",  # empty reference
    "eip155:1/erc20:0xshort",
    "eip155:1/erc20:a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # missing 0x
    "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb4",  # 39 hex digits
    "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb480",  # 41 hex digits
    "eip155:1/erc20:0xg0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # non-hex 'g'
    "eip155:1/unknownns:x",  # unknown namespace
    "eip155:1/slip44:abc",
    "eip155:1/slip44:-1",  # sign is not a decimal digit
    "eip155:1/slip44:0x60",
    "eip155:1/slip44:007",  # leading zeros are non-canonical
    "eip155:1/slip44:",
    f"{SOL_CHAIN}/token:",  # empty base58 reference
    f"{SOL_CHAIN}/token:O0Il",  # chars outside the base58 alphabet
    "/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # empty chain id
    "eip155/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # chain id missing ':'
    USDC_ETH + "/extra",  # a second '/'
    # The CHAIN half, canonical too. Both of these parsed as chain 1 until
    # 0.2.0 phase 11's pattern sweep, so USDC on Ethereum had three asset
    # ids depending on how the caller spelled the chain, and every id
    # derived over one of them forked with it. chains/evm.py had refused
    # the same spellings since Phase 0; this half never asked it.
    "eip155:01/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # chain 1, leading zero
    "eip155:0000001/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # chain 1, padded
    "eip155:01/slip44:60",  # same, on the native asset
]


@pytest.mark.parametrize("value", MALFORMED)
def test_parse_caip19_malformed_raises(value):
    with pytest.raises(CaipParseError):
        parse_caip19(value)


@pytest.mark.parametrize("value", MALFORMED)
def test_canonical_caip19_malformed_raises(value):
    with pytest.raises(CaipParseError):
        canonical_caip19(value)


@pytest.mark.parametrize("value", [None, 1, 0x60, ["eip155:1/slip44:60"]])
def test_parse_caip19_non_string_raises(value):
    with pytest.raises(CaipParseError):
        parse_caip19(value)  # type: ignore[arg-type]
