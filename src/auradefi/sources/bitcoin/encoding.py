"""Pure Bitcoin hash/encoding primitives and address codecs (SPEC §3.2, §10).

PURE stdlib module: ``hashlib`` and ``struct`` only — zero I/O, no httpx.
Every algorithm here is a pinned wire-format contract (docs/internal/DECISIONS.md):

* Base58Check alphabet ``123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnop
  qrstuvwxyz``; checksum = first 4 bytes of double-SHA256; each leading
  0x00 byte <-> one leading ``1``.
* bech32 per BIP173 (NOT bech32m): charset ``qpzry9x8gf2tvdw0s3jn54khce6
  mua7l``, generators ``[0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd,
  0x2a1462b3]``, xor-constant 1, hrp ``bc``, witness v0, all-lowercase.
* hash160 = RIPEMD160(SHA256(x)); ``hashlib.new('ripemd160')`` when the
  OpenSSL build provides it, else a pure-Python fallback pinned to the
  RIPEMD-160 paper vectors (OpenSSL 3 CI reality).

Mainnet only. Compressed public keys only (33 bytes, lead 0x02/0x03).
"""

from __future__ import annotations

import hashlib
import struct

from auradefi.errors import ValidationError

__all__ = [
    "base58check_decode",
    "base58check_encode",
    "hash160",
    "p2pkh_address",
    "p2wpkh_address",
    "ripemd160",
    "sha256d",
]

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {char: index for index, char in enumerate(_BASE58_ALPHABET)}

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_GENERATORS = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)

# RIPEMD-160 round constants and per-round schedules (left line, right line).
_RMD_K_LEFT = (0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E)
_RMD_K_RIGHT = (0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000)
_RMD_R_LEFT = (
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
    3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
    1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
    4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13,
)
_RMD_R_RIGHT = (
    5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
    6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
    15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
    8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
    12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11,
)
_RMD_S_LEFT = (
    11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
    7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
    11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
    11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
    9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6,
)
_RMD_S_RIGHT = (
    8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
    9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
    9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
    15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
    8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11,
)
_MASK32 = 0xFFFFFFFF


def _rol(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (32 - shift))) & _MASK32


def _rmd_f(round_index: int, x: int, y: int, z: int) -> int:
    if round_index == 0:
        return x ^ y ^ z
    if round_index == 1:
        return (x & y) | (~x & z)
    if round_index == 2:
        return (x | ~y) ^ z
    if round_index == 3:
        return (x & z) | (y & ~z)
    return x ^ (y | ~z)


def _rmd_compress(state: tuple[int, ...], block: bytes) -> tuple[int, ...]:
    words = struct.unpack("<16I", block)
    al, bl, cl, dl, el = state
    ar, br, cr, dr, er = state
    for j in range(80):
        round_index = j // 16
        t = (
            al
            + _rmd_f(round_index, bl, cl, dl)
            + words[_RMD_R_LEFT[j]]
            + _RMD_K_LEFT[round_index]
        ) & _MASK32
        t = (_rol(t, _RMD_S_LEFT[j]) + el) & _MASK32
        al, el, dl, cl, bl = el, dl, _rol(cl, 10), bl, t
        t = (
            ar
            + _rmd_f(4 - round_index, br, cr, dr)
            + words[_RMD_R_RIGHT[j]]
            + _RMD_K_RIGHT[round_index]
        ) & _MASK32
        t = (_rol(t, _RMD_S_RIGHT[j]) + er) & _MASK32
        ar, er, dr, cr, br = er, dr, _rol(cr, 10), br, t
    h0, h1, h2, h3, h4 = state
    return (
        (h1 + cl + dr) & _MASK32,
        (h2 + dl + er) & _MASK32,
        (h3 + el + ar) & _MASK32,
        (h4 + al + br) & _MASK32,
        (h0 + bl + cr) & _MASK32,
    )


def _ripemd160_pure(data: bytes) -> bytes:
    """Pure-Python RIPEMD-160 (20-byte digest).

    The fallback for OpenSSL builds without the legacy digest. Kept as a
    named function so tests exercise it directly even where
    ``hashlib.new('ripemd160')`` works. Pinned vectors:
    ``b'' -> 9c1185a5c5e9fc54612808977ee8f548b2258d31``,
    ``b'abc' -> 8eb208f7e05d987a9b044a8e98c6b087f15a0bfc``.
    """
    state = (0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0)
    padded = data + b"\x80" + b"\x00" * ((55 - len(data)) % 64)
    padded += struct.pack("<Q", 8 * len(data))
    for offset in range(0, len(padded), 64):
        state = _rmd_compress(state, padded[offset : offset + 64])
    return struct.pack("<5I", *state)


def ripemd160(data: bytes) -> bytes:
    """RIPEMD-160 digest of ``data`` (20 bytes).

    Uses ``hashlib.new('ripemd160', data)`` when OpenSSL provides the
    digest; when ``hashlib.new`` raises, falls back to
    :func:`_ripemd160_pure`. The attempt happens per call, so a host
    whose OpenSSL lacks the digest still gets correct output.
    """
    try:
        return hashlib.new("ripemd160", data).digest()
    except ValueError:
        return _ripemd160_pure(data)


def sha256d(data: bytes) -> bytes:
    """Double SHA-256: ``SHA256(SHA256(data))`` (32 bytes)."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def hash160(data: bytes) -> bytes:
    """Bitcoin HASH160: ``RIPEMD160(SHA256(data))`` (20 bytes)."""
    return ripemd160(hashlib.sha256(data).digest())


def base58check_encode(payload: bytes) -> str:
    """Base58Check-encode ``payload`` (DECISIONS-pinned alphabet).

    Appends the 4-byte checksum ``sha256d(payload)[:4]``, then encodes;
    each leading 0x00 byte of ``payload + checksum`` becomes one leading
    ``'1'``.
    """
    full = payload + sha256d(payload)[:4]
    leading_zeros = len(full) - len(full.lstrip(b"\x00"))
    number = int.from_bytes(full, "big")
    digits: list[str] = []
    while number:
        number, remainder = divmod(number, 58)
        digits.append(_BASE58_ALPHABET[remainder])
    return "1" * leading_zeros + "".join(reversed(digits))


def base58check_decode(encoded: str) -> bytes:
    """Decode Base58Check, returning the payload without its checksum.

    Raises:
        ValidationError: on any character outside the pinned alphabet
            (``0``, ``O``, ``I``, ``l`` included) or on checksum mismatch.
    """
    number = 0
    for char in encoded:
        if char not in _BASE58_INDEX:
            raise ValidationError(f"character {char!r} outside the Base58 alphabet")
        number = number * 58 + _BASE58_INDEX[char]
    leading_ones = len(encoded) - len(encoded.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    full = b"\x00" * leading_ones + body
    if len(full) < 4 or sha256d(full[:-4])[:4] != full[-4:]:
        raise ValidationError("Base58Check checksum mismatch")
    return full[:-4]


def _require_compressed_pubkey(pubkey: bytes) -> None:
    """Reject anything but a 33-byte compressed SEC key (lead 0x02/0x03)."""
    if len(pubkey) != 33 or pubkey[0] not in (0x02, 0x03):
        raise ValidationError(
            "public key must be 33 bytes with lead byte 0x02 or 0x03 "
            f"(got {len(pubkey)} bytes)"
        )


def p2pkh_address(pubkey: bytes) -> str:
    """Mainnet P2PKH address: ``base58check_encode(b'\\x00' + hash160(pubkey))``.

    Raises:
        ValidationError: unless ``pubkey`` is exactly 33 bytes with lead
            byte 0x02 or 0x03 (compressed only).
    """
    _require_compressed_pubkey(pubkey)
    return base58check_encode(b"\x00" + hash160(pubkey))


def _bech32_polymod(values: list[int]) -> int:
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for bit in range(5):
            if (top >> bit) & 1:
                checksum ^= _BECH32_GENERATORS[bit]
    return checksum


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _regroup_8_to_5(data: bytes) -> list[int]:
    """Regroup 8-bit bytes into 5-bit values, zero-padding the tail."""
    groups: list[int] = []
    accumulator = 0
    bits = 0
    for byte in data:
        accumulator = (accumulator << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            groups.append((accumulator >> bits) & 31)
    if bits:
        groups.append((accumulator << (5 - bits)) & 31)
    return groups


def p2wpkh_address(pubkey: bytes) -> str:
    """Mainnet P2WPKH address per BIP173 bech32 (NOT bech32m).

    hrp ``bc``, witness version 0, program = ``hash160(pubkey)`` regrouped
    8-to-5 zero-padded; xor-constant 1 checksum; all-lowercase output.

    Raises:
        ValidationError: unless ``pubkey`` is exactly 33 bytes with lead
            byte 0x02 or 0x03 (compressed only).
    """
    _require_compressed_pubkey(pubkey)
    hrp = "bc"
    data = [0] + _regroup_8_to_5(hash160(pubkey))
    polymod = _bech32_polymod(_bech32_hrp_expand(hrp) + data + [0] * 6) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32_CHARSET[value] for value in data + checksum)
