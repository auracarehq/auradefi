"""Bitcoin family constants: pinned CAIP-2 identifiers and SLIP-44 (SPEC §4.2).

The bip122 references are the first 32 lowercase hex chars of the genesis
block hashes — wire-format contracts, hardcoded here byte-for-byte.
"""

from __future__ import annotations

import string

from auradefi.chains import bitcoin


def test_mainnet_caip2_is_pinned():
    # SPEC §4.2 golden vector: BTC native asset lives under this exact chain id.
    assert bitcoin.MAINNET == "bip122:000000000019d6689c085ae165831e93"


def test_testnet_caip2_is_pinned():
    assert bitcoin.TESTNET == "bip122:000000000933ea01ad0ee984209779ba"


def test_slip44_coin_type_is_zero():
    assert bitcoin.SLIP44 == 0
    assert isinstance(bitcoin.SLIP44, int)


def test_references_are_32_lowercase_hex_chars():
    hexdigits = set(string.digits + "abcdef")
    for caip2 in (bitcoin.MAINNET, bitcoin.TESTNET):
        namespace, _, reference = caip2.partition(":")
        assert namespace == "bip122"
        assert len(reference) == 32
        assert set(reference) <= hexdigits, "bip122 references are lowercase hex"


def test_mainnet_and_testnet_are_distinct():
    assert bitcoin.MAINNET != bitcoin.TESTNET


def test_native_asset_caip19_composes_from_the_constants():
    # The registry seeds BTC as MAINNET + '/slip44:' + SLIP44 — the parts
    # must compose to the exact pinned CAIP-19.
    assert (
        f"{bitcoin.MAINNET}/slip44:{bitcoin.SLIP44}"
        == "bip122:000000000019d6689c085ae165831e93/slip44:0"
    )
