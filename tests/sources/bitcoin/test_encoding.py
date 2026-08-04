"""Golden-vector tests for the pure Bitcoin encoding module (SPEC §3.2, §10).

Every expected value below is a hardcoded literal derived independently
from the DECISIONS-pinned algorithms (Base58Check alphabet, BIP173 bech32
charset/generators/xor-constant 1, hash160 = RIPEMD160(SHA256(x))) and
cross-checked against published vectors: the RIPEMD-160 paper test suite,
BIP173's own P2WPKH example over compressed G, and the BIP32 test-vector-1
master public key (whose hash160, 3442193e..., is the identifier printed
in BIP32 itself). Nothing here calls the function under test to produce
its own expectation.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from auradefi.errors import ValidationError
from auradefi.sources.bitcoin import encoding
from auradefi.sources.bitcoin.encoding import (
    _ripemd160_pure,
    base58check_decode,
    base58check_encode,
    hash160,
    p2pkh_address,
    p2wpkh_address,
    ripemd160,
    sha256d,
)

# Compressed generator point of secp256k1 — BIP173's own example key.
G_COMPRESSED = bytes.fromhex(
    "0279BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798"
)
G_HASH160 = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")
# BIP32 test vector 1, chain m, public key.
BIP32_M_PUB = bytes.fromhex(
    "0339a36013301597daef41fbe593a02cc513d0b55527ec2df1050e2e8ff49c85c2"
)

# RIPEMD-160 paper test suite (Dobbertin/Bosselaers/Preneel).
RIPEMD_VECTORS = [
    (b"", "9c1185a5c5e9fc54612808977ee8f548b2258d31"),
    (b"a", "0bdc9d2d256b3ee9daae347be6f4dc835a467ffe"),
    (b"abc", "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc"),
    (b"message digest", "5d0689ef49d2fae572b881b123a85ffa21595f36"),
    (b"abcdefghijklmnopqrstuvwxyz", "f71c27109c692c1b56bbdceb5b9d2865b3708dbc"),
    (
        b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
        "12a053384a9c0c88e405a06c27dcf49ada62eb2b",
    ),
    (
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
        "b0e20b6e3116640286ed3a87a5713079b21f5189",
    ),
    (b"1234567890" * 8, "9b752e45573d4b39f4dbd3323cab82bf63326bfb"),
]

BAD_PUBKEYS = [
    # 65-byte uncompressed G (0x04 || x || y) — compressed only, rejected.
    bytes.fromhex(
        "0479BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798"
        "483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8"
    ),
    bytes(32),  # 32 bytes — an x-only key is not a compressed pubkey
    b"\x04" + bytes(32),  # 33 bytes but lead 0x04
    b"\x01" + bytes(32),  # 33 bytes but lead outside {0x02, 0x03}
    b"",  # empty
    G_COMPRESSED + b"\x00",  # 34 bytes
]


@pytest.mark.parametrize(("message", "digest_hex"), RIPEMD_VECTORS)
def test_ripemd160_paper_vectors(message: bytes, digest_hex: str) -> None:
    assert ripemd160(message) == bytes.fromhex(digest_hex)


@pytest.mark.parametrize(("message", "digest_hex"), RIPEMD_VECTORS)
def test_ripemd160_pure_paper_vectors(message: bytes, digest_hex: str) -> None:
    assert _ripemd160_pure(message) == bytes.fromhex(digest_hex)


def test_ripemd160_falls_back_when_hashlib_new_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenSSL 3 CI reality: hashlib.new('ripemd160') raises ValueError.

    The public function must still return the pinned digests — i.e. the
    fallback decision happens per call, not once at import time.
    """

    def _no_legacy_digests(name: str, *args: object, **kwargs: object) -> object:
        raise ValueError(f"unsupported hash type {name}")

    monkeypatch.setattr(hashlib, "new", _no_legacy_digests)
    assert ripemd160(b"") == bytes.fromhex(
        "9c1185a5c5e9fc54612808977ee8f548b2258d31"
    )
    assert ripemd160(b"abc") == bytes.fromhex(
        "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc"
    )


def test_ripemd160_pure_agrees_with_openssl_where_available() -> None:
    try:
        hashlib.new("ripemd160")
    except ValueError:
        pytest.skip("this OpenSSL build ships no ripemd160 digest")
    # Includes a multi-block (>64-byte) deterministic message.
    for message in (b"", b"abc", b"1234567890" * 8, bytes(range(256))):
        assert _ripemd160_pure(message) == hashlib.new("ripemd160", message).digest()


def test_sha256d_golden() -> None:
    assert sha256d(b"") == bytes.fromhex(
        "5df6e0e2761359d30a8275058e299fcc0381534545f55cf43e41983f5d4c9456"
    )
    assert sha256d(b"hello") == bytes.fromhex(
        "9595c9df90075148eb06860365df33584b75bff782a510c6cd4883a419833d50"
    )


def test_hash160_golden() -> None:
    # BIP173's own example over compressed G.
    assert hash160(G_COMPRESSED) == G_HASH160
    assert hash160(b"") == bytes.fromhex("b472a266d0bd89c13706a4132ccfb16f7c3b9fcb")
    # BIP32 test vector 1 master key identifier, printed in the BIP itself.
    assert hash160(BIP32_M_PUB) == bytes.fromhex(
        "3442193e1bb70916e914552172cd4e2dbc9df811"
    )


def test_p2pkh_address_of_compressed_g() -> None:
    assert p2pkh_address(G_COMPRESSED) == "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"


def test_p2wpkh_address_of_compressed_g_matches_bip173_example() -> None:
    address = p2wpkh_address(G_COMPRESSED)
    assert address == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    assert address == address.lower()  # BIP173: all-lowercase


def test_addresses_of_bip32_vector1_master_key() -> None:
    assert p2pkh_address(BIP32_M_PUB) == "15mKKb2eos1hWa6tisdPwwDC1a5J1y9nma"
    assert p2wpkh_address(BIP32_M_PUB) == "bc1qx3ppj0smkuy3d6g525sh9n2w9k7fm7q3x30rtg"


def test_p2pkh_address_decodes_to_versioned_hash160() -> None:
    payload = base58check_decode("1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH")
    assert payload == b"\x00" + G_HASH160


@pytest.mark.parametrize("bad", BAD_PUBKEYS, ids=lambda b: f"{len(b)}B-{b[:1].hex() or 'empty'}")
def test_p2pkh_rejects_non_compressed_pubkeys(bad: bytes) -> None:
    with pytest.raises(ValidationError):
        p2pkh_address(bad)


@pytest.mark.parametrize("bad", BAD_PUBKEYS, ids=lambda b: f"{len(b)}B-{b[:1].hex() or 'empty'}")
def test_p2wpkh_rejects_non_compressed_pubkeys(bad: bytes) -> None:
    with pytest.raises(ValidationError):
        p2wpkh_address(bad)


def test_base58check_leading_zero_bytes_round_trip() -> None:
    encoded = base58check_encode(b"\x00\x00\x00\x01")
    assert encoded == "111E1CgqW"
    # exactly three leading '1's — one per leading 0x00 byte, no more
    assert len(encoded) - len(encoded.lstrip("1")) == 3
    assert base58check_decode(encoded) == b"\x00\x00\x00\x01"


def test_base58check_empty_payload_round_trip() -> None:
    assert base58check_encode(b"") == "3QJmnh"
    assert base58check_decode("3QJmnh") == b""


def test_base58check_huge_payload_round_trip() -> None:
    payload = (10**77).to_bytes(32, "big")
    encoded = base58check_encode(payload)
    assert encoded == "2gNLtkcaKAzb125tXe8N1YWryoLSJtNR2dZqWHgYYLwBrQ7dwA"
    assert base58check_decode(encoded) == payload


@pytest.mark.parametrize("confusable", ["0", "O", "I", "l"])
def test_base58check_decode_rejects_confusable_characters(confusable: str) -> None:
    with pytest.raises(ValidationError):
        base58check_decode("111E1Cgq" + confusable)


def test_base58check_decode_rejects_characters_outside_alphabet() -> None:
    with pytest.raises(ValidationError):
        base58check_decode(" 111E1CgqW")  # leading space
    with pytest.raises(ValidationError):
        base58check_decode("111E1+CgqW")  # symbol mid-string


def test_base58check_decode_rejects_final_character_mutation() -> None:
    # Both mutants stay inside the alphabet, so only the checksum catches them.
    with pytest.raises(ValidationError):
        base58check_decode("111E1CgqX")
    with pytest.raises(ValidationError):
        base58check_decode("1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMJ")


def test_module_imports_hashlib_struct_errors_only() -> None:
    """PURE contract: hashlib, struct, auradefi.errors — nothing else, ever.

    Walks the whole AST, so a function-local ``import httpx`` fails too.
    """
    tree = ast.parse(Path(encoding.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    allowed = {"__future__", "hashlib", "struct", "auradefi.errors"}
    assert imported <= allowed, f"forbidden imports: {sorted(imported - allowed)}"
