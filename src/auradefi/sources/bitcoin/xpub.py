"""BIP32 CKDpub derivation over secp256k1 (SPEC §3.2, §10).

PURE stdlib module: ``hashlib``, ``hmac``, ``dataclasses`` and the pure
:mod:`auradefi.sources.bitcoin.encoding` codecs only — zero I/O, no
httpx, no esplora import. SPEC §10 is the reason: an extended key is
derived locally and NEVER travels off-box. :func:`derive_addresses`
returns plain addresses, which is all the HTTP layer ever sees.

Pinned in docs/DECISIONS.md:

* **secp256k1**: ``p = 2**256 - 2**32 - 977``; affine add/double,
  double-and-add scalar mult, inversion via ``pow(x, -1, p)``; compressed
  parse ``y = (x**3 + 7)**((p + 1) // 4) mod p`` with a parity fix and
  ``ValidationError`` off-curve; serialize = ``(0x02 | y & 1) || x``.
* **BIP32 CKDpub**: version ``0x0488B21E`` only; a hardened index from an
  xpub raises; ``I_L >= n`` or a point-at-infinity child raises
  (deliberate deviation from BIP32's skip-to-next-index — probability
  < 2**-127, raising is honest); parent fingerprint =
  ``hash160(parent pubkey)[:4]``.

Mainnet only, compressed keys only. Every failure is ``ValidationError``.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from auradefi.errors import ValidationError
from auradefi.sources.bitcoin.encoding import (
    base58check_decode,
    base58check_encode,
    hash160,
    p2pkh_address,
    p2wpkh_address,
)

__all__ = [
    "Xpub",
    "ckd_pub",
    "derive_addresses",
    "derive_path",
    "parse_xpub",
    "serialize_xpub",
]

CURVE_P = 2**256 - 2**32 - 977
CURVE_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GENERATOR = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
XPUB_VERSION = 0x0488B21E
HARDENED = 2**31
SERIALIZED_LENGTH = 78

_MAX_UINT32 = 2**32 - 1
_DIGITS = frozenset("0123456789")
_ADDRESS_CODECS = {"p2pkh": p2pkh_address, "p2wpkh": p2wpkh_address}


def _require_int(value: object, name: str) -> None:
    """A non-bool ``int`` or ``ValidationError``.

    ``bool`` is rejected FIRST: it is an ``int`` subclass, so ``True``
    would otherwise pass every numeric check as 1.
    """
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be an int, got bool")
    if not isinstance(value, int):
        raise ValidationError(f"{name} must be an int, got {type(value).__name__}")


def _require_range(value: object, name: str, low: int, high: int) -> None:
    """A non-bool ``int`` within ``low..high`` inclusive, or ``ValidationError``."""
    _require_int(value, name)
    if not low <= value <= high:  # type: ignore[operator]
        raise ValidationError(f"{name} must be in {low}..{high}, got {value}")


def _require_compressed_lead(pubkey: bytes) -> None:
    """Lead byte 0x02 or 0x03 (compressed SEC) or ``ValidationError``."""
    if pubkey[0] not in (0x02, 0x03):
        raise ValidationError(
            f"pubkey lead byte must be 0x02/0x03, got {pubkey[0]:#04x}"
        )


def _require_bytes(value: object, name: str, length: int) -> None:
    """Exactly ``length`` ``bytes`` or ``ValidationError``."""
    if not isinstance(value, bytes):
        raise ValidationError(f"{name} must be bytes, got {type(value).__name__}")
    if len(value) != length:
        raise ValidationError(f"{name} must be {length} bytes, got {len(value)}")


def _add(left: tuple[int, int] | None, right: tuple[int, int] | None):
    """Affine secp256k1 point addition; ``None`` is the point at infinity."""
    if left is None:
        return right
    if right is None:
        return left
    left_x, left_y = left
    right_x, right_y = right
    if left_x == right_x:
        if (left_y + right_y) % CURVE_P == 0:
            return None
        slope = 3 * left_x * left_x * pow(2 * left_y, -1, CURVE_P) % CURVE_P
    else:
        slope = (right_y - left_y) * pow(right_x - left_x, -1, CURVE_P) % CURVE_P
    x = (slope * slope - left_x - right_x) % CURVE_P
    return (x, (slope * (left_x - x) - left_y) % CURVE_P)


def _multiply(scalar: int, point: tuple[int, int] = GENERATOR):
    """Double-and-add scalar multiplication; ``None`` at infinity."""
    result: tuple[int, int] | None = None
    addend: tuple[int, int] | None = point
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def _parse_point(pubkey: bytes) -> tuple[int, int]:
    """Decompress a 33-byte SEC key to ``(x, y)``.

    ``y = (x**3 + 7)**((p + 1) // 4) mod p`` — valid because
    ``p % 4 == 3`` — then the parity of the lead byte selects ``y`` or
    ``p - y``.

    Raises:
        ValidationError: wrong length, lead byte outside 0x02/0x03,
            ``x >= p``, or ``x`` with no square root (off the curve).
    """
    _require_bytes(pubkey, "pubkey", 33)
    _require_compressed_lead(pubkey)
    x = int.from_bytes(pubkey[1:], "big")
    if x >= CURVE_P:
        raise ValidationError("pubkey x coordinate is not a field element")
    alpha = (pow(x, 3, CURVE_P) + 7) % CURVE_P
    y = pow(alpha, (CURVE_P + 1) // 4, CURVE_P)
    if y * y % CURVE_P != alpha:
        raise ValidationError(f"pubkey is not on secp256k1: x={x:#x}")
    if y & 1 != pubkey[0] & 1:
        y = CURVE_P - y
    return (x, y)


def _serialize_point(point: tuple[int, int]) -> bytes:
    """Compressed SEC encoding: ``(0x02 | y & 1) || x`` (33 bytes)."""
    x, y = point
    return bytes([0x02 | (y & 1)]) + x.to_bytes(32, "big")


@dataclass(frozen=True, slots=True)
class Xpub:
    """One BIP32 extended PUBLIC key — the five serialized fields.

    ``child_number >= 2**31`` is LEGAL: it records that this node's own
    derivation from ITS parent was hardened. Only DERIVING a hardened
    child from an xpub is impossible, and that is :func:`ckd_pub`'s
    error, not this record's.

    ``ValidationError`` unless: ``depth`` is a non-bool int in 0..255,
    ``parent_fingerprint`` is exactly 4 bytes, ``child_number`` is a
    non-bool int in ``0 .. 2**32 - 1``, ``chain_code`` is exactly 32
    bytes, and ``pubkey`` is 33 bytes leading 0x02 or 0x03.
    """

    depth: int
    parent_fingerprint: bytes
    child_number: int
    chain_code: bytes
    pubkey: bytes

    def __post_init__(self) -> None:
        """Validate every field; raise ``ValidationError`` on violation."""
        _require_range(self.depth, "depth", 0, 255)
        _require_bytes(self.parent_fingerprint, "parent_fingerprint", 4)
        _require_range(self.child_number, "child_number", 0, _MAX_UINT32)
        _require_bytes(self.chain_code, "chain_code", 32)
        _require_bytes(self.pubkey, "pubkey", 33)
        _require_compressed_lead(self.pubkey)


def parse_xpub(encoded: str) -> Xpub:
    """Decode a Base58Check mainnet xpub string into an :class:`Xpub`.

    The payload must be exactly 78 bytes and carry version
    ``0x0488B21E``: an xprv (``0x0488ADE4``), a ypub, and a zpub are all
    ``ValidationError`` — this module speaks BIP44 mainnet xpub only.
    The 33-byte key must parse to a point ON the curve.

    Raises:
        ValidationError: bad Base58 character, checksum mismatch, wrong
            payload length, wrong version, or an off-curve/malformed key.
    """
    if not isinstance(encoded, str):
        raise ValidationError(f"xpub must be a str, got {type(encoded).__name__}")
    payload = base58check_decode(encoded)
    if len(payload) != SERIALIZED_LENGTH:
        raise ValidationError(
            f"extended key payload must be {SERIALIZED_LENGTH} bytes, "
            f"got {len(payload)}"
        )
    version = int.from_bytes(payload[:4], "big")
    if version != XPUB_VERSION:
        raise ValidationError(
            f"version {version:#010x} is not a mainnet xpub ({XPUB_VERSION:#010x}) — "
            "xprv, ypub and zpub are all refused"
        )
    pubkey = payload[45:78]
    _parse_point(pubkey)  # on-curve or ValidationError
    return Xpub(
        depth=payload[4],
        parent_fingerprint=payload[5:9],
        child_number=int.from_bytes(payload[9:13], "big"),
        chain_code=payload[13:45],
        pubkey=pubkey,
    )


def serialize_xpub(xpub: Xpub) -> str:
    """Serialize to Base58Check — the exact inverse of :func:`parse_xpub`.

    Layout: ``version(4) || depth(1) || parent_fingerprint(4) ||
    child_number(4, big-endian) || chain_code(32) || pubkey(33)``.
    """
    payload = (
        XPUB_VERSION.to_bytes(4, "big")
        + bytes([xpub.depth])
        + xpub.parent_fingerprint
        + xpub.child_number.to_bytes(4, "big")
        + xpub.chain_code
        + xpub.pubkey
    )
    return base58check_encode(payload)


def ckd_pub(xpub: Xpub, index: int) -> Xpub:
    """BIP32 CKDpub: the public child at ``index``.

    ``I = HMAC-SHA512(chain_code, ser_P(K_par) || ser_32(index))``; the
    child point is ``I_L * G + K_par``; the child carries ``depth + 1``,
    ``hash160(parent.pubkey)[:4]`` as fingerprint, ``index``, ``I_R`` as
    chain code.

    Raises:
        ValidationError: ``index`` is not a non-bool int in
            ``0 .. 2**31 - 1`` (a hardened index cannot be derived from a
            public key), ``I_L >= n``, or the child is the point at
            infinity.
    """
    _require_range(index, "index", 0, HARDENED - 1)
    digest = hmac.new(
        xpub.chain_code,
        xpub.pubkey + index.to_bytes(4, "big"),
        hashlib.sha512,
    ).digest()
    offset = int.from_bytes(digest[:32], "big")
    if offset >= CURVE_N:
        raise ValidationError(f"CKDpub I_L >= n at index {index}")
    child = _add(_multiply(offset), _parse_point(xpub.pubkey))
    if child is None:
        raise ValidationError(f"CKDpub child is the point at infinity at index {index}")
    return Xpub(
        depth=xpub.depth + 1,
        parent_fingerprint=hash160(xpub.pubkey)[:4],
        child_number=index,
        chain_code=digest[32:],
        pubkey=_serialize_point(child),
    )


def derive_path(xpub: Xpub, path: str) -> Xpub:
    """Walk a NON-hardened BIP32 path: ``'m'`` or ``'m/0/1'``.

    ``'m'`` and ``'M'`` alone are the identity. Every later segment is a
    plain unsigned decimal index applied via :func:`ckd_pub`.

    Raises:
        ValidationError: the path does not start with ``m``/``M``, a
            segment is empty, negative, non-numeric, or carries a
            hardened marker (``'``, ``h``, or ``H``).
    """
    if not isinstance(path, str):
        raise ValidationError(f"path must be a str, got {type(path).__name__}")
    segments = path.split("/")
    if segments[0] not in ("m", "M"):
        raise ValidationError(f"path must start with 'm' or 'M', got {path!r}")
    node = xpub
    for segment in segments[1:]:
        # ASCII digits only: a hardened marker, a sign, a separator, or a
        # non-ASCII digit is a malformed segment, never a silent index.
        if not segment or not _DIGITS.issuperset(segment):
            raise ValidationError(
                f"path segment {segment!r} must be an unsigned decimal index "
                "(hardened derivation is impossible from a public key)"
            )
        node = ckd_pub(node, int(segment))
    return node


def derive_addresses(
    xpub: str,
    kind: str,
    chain: int,
    start: int,
    count: int,
) -> tuple[str, ...]:
    """Addresses for ``chain`` indices ``start .. start + count - 1``.

    PARAMETER ORDER IS LOAD-BEARING:
    ``functools.partial(derive_addresses, xpub, kind)`` is exactly the
    ``derive(chain, start, count)`` callable
    :func:`auradefi.sources.bitcoin.esplora.scan` takes — the scanner
    therefore never holds an extended key (SPEC §10).

    The chain node ``m/chain`` is derived ONCE via :func:`ckd_pub`, then
    one further :func:`ckd_pub` per address.

    Raises:
        ValidationError: ``kind`` outside ``{'p2pkh', 'p2wpkh'}``,
            ``chain`` outside ``{0, 1}``, ``start < 0``, ``count < 0``
            (bool rejected before int on all three), or any failure
            propagated from :func:`parse_xpub` / :func:`ckd_pub`.
    """
    if not isinstance(kind, str) or kind not in _ADDRESS_CODECS:
        raise ValidationError(
            f"kind must be one of {sorted(_ADDRESS_CODECS)}, got {kind!r}"
        )
    _require_range(chain, "chain", 0, 1)
    # Each is bounded by the derivable index space; a start+count window
    # that runs off the end is ckd_pub's error to raise, not a silent wrap.
    _require_range(start, "start", 0, HARDENED - 1)
    _require_range(count, "count", 0, HARDENED - 1)
    codec = _ADDRESS_CODECS[kind]
    node = ckd_pub(parse_xpub(xpub), chain)
    return tuple(
        codec(ckd_pub(node, index).pubkey) for index in range(start, start + count)
    )
