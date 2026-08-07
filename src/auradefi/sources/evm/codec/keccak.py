"""keccak-f[1600] and keccak256, stdlib only (RELEASE_0.2.0 §3, §4).

``hashlib`` provides ``sha3_256``, which is NOT keccak256: the two differ
in one padding byte, so a selector computed with the stdlib SHA-3
addresses a different function than the one you named. Under the rule
forbidding new third-party dependencies, this module hand-rolls the
permutation. It has published vectors, so it is verifiable.

PINNED CONSTRUCTION, which the golden vectors in the mirrored test file
were derived from and which no reader should have to re-derive:

* State is 5x5 lanes of 64-bit words, held little-endian.
* Rate 136 bytes (1088 bits), capacity 512.
* Absorb each 136-byte block by XOR into ``lane[i % 5][i // 5]`` for
  ``i`` in 0..16, then permute.
* 24 rounds of theta, rho, pi, chi, iota.
* Rotation offsets ``ROT[x][y]``: ``ROT[0] = [0, 36, 3, 41, 18]``,
  ``ROT[1] = [1, 44, 10, 45, 2]``, ``ROT[2] = [62, 6, 43, 15, 61]``,
  ``ROT[3] = [28, 55, 25, 21, 56]``, ``ROT[4] = [27, 20, 39, 8, 14]``.
* pi is ``B[y][(2 * x + 3 * y) % 5] = rot(A[x][y], ROT[x][y])``.
* The 24 standard round constants, starting 0x0000000000000001,
  0x0000000000008082, 0x800000000000808A, 0x8000000080008000 and ending
  0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
  0x8000000080008081, 0x8000000000008080, 0x0000000080000001,
  0x8000000080008008.
* Padding, the one byte that separates this from the stdlib: append
  0x01, zero-fill to a multiple of the rate, then XOR 0x80 into the
  final byte of the padded input. SHA3-256 appends 0x06 instead.
* Squeeze 32 bytes from the lanes in the same lane order.

SEAMS this module owes its callers:

* ``keccak256(data) -> bytes`` returns exactly 32 bytes. It is consumed
  by ``codec/abi.py::selector`` and asserted in
  ``tests/golden/test_phase11_reader.py``.
* The digest is the squeezed lane bytes in little-endian lane order, so
  ``keccak256(signature)[:4]`` IS the four-byte selector, with no
  further reversal anywhere.
* :func:`_sponge` is a documented private seam, not an accident of
  factoring. ``tests/sources/evm/codec/test_keccak.py`` calls it with
  ``pad_byte=0x06`` and compares against ``hashlib.sha3_256`` across
  every length either side of the 136-byte rate boundary. Identical rate
  and identical permutation mean SHA3-256 differs from keccak256 in that
  byte alone, so the sweep is a stdlib-only proof of the permutation
  that the two published keccak vectors cannot reach. Keep the pad byte
  a parameter.

Pure module: stdlib only, no HTTP, no module-level mutable state, no
socket at import.
"""

from __future__ import annotations

from auradefi.errors import ValidationError

__all__ = ["keccak256"]

#: Bytes absorbed per permutation. Rate 1088 bits, capacity 512, which
#: is what pairs a 256-bit output with the 1600-bit state.
_RATE = 136

#: Lanes read from and written to one rate block: 136 // 8.
_LANES_PER_BLOCK = _RATE // 8

#: Lanes squeezed for a 32-byte digest: 32 // 8.
_LANES_PER_DIGEST = 4

_ROUNDS = 24

_LANE_MASK = (1 << 64) - 1

#: ``_ROT[x][y]``, the rho offsets, indexed column then row to match the
#: pi expression below.
_ROT = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)

#: The 24 standard iota round constants, in order.
_RC = (
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
)


def _rotl(lane: int, offset: int) -> int:
    """Rotate a 64-bit lane left by ``offset`` bits."""
    return ((lane << offset) | (lane >> (64 - offset))) & _LANE_MASK


def _keccak_f(lanes: list[list[int]]) -> None:
    """Apply keccak-f[1600] to ``lanes`` in place.

    ``lanes[x][y]`` is the lane at column ``x`` and row ``y``, held as a
    64-bit integer whose bytes are little-endian on the wire.
    """
    for round_constant in _RC:
        # theta: fold each column's parity into its two neighbours.
        column = [
            lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4]
            for x in range(5)
        ]
        for x in range(5):
            delta = column[(x - 1) % 5] ^ _rotl(column[(x + 1) % 5], 1)
            row = lanes[x]
            for y in range(5):
                row[y] ^= delta

        # rho and pi together: rotate each lane, then move it. Writing
        # both in one pass is why the destination index carries the
        # pinned (2x + 3y) expression rather than a second loop.
        moved = [[0] * 5 for _ in range(5)]
        for x in range(5):
            rot_x = _ROT[x]
            lane_x = lanes[x]
            for y in range(5):
                moved[y][(2 * x + 3 * y) % 5] = _rotl(lane_x[y], rot_x[y])

        # chi: the only nonlinear step, applied along each row.
        for x in range(5):
            near = moved[(x + 1) % 5]
            far = moved[(x + 2) % 5]
            own = moved[x]
            row = lanes[x]
            for y in range(5):
                row[y] = own[y] ^ (~near[y] & far[y] & _LANE_MASK)

        # iota: break the symmetry the other four steps preserve.
        lanes[0][0] ^= round_constant


def _sponge(data: bytes, pad_byte: int) -> bytes:
    """Absorb ``data`` at rate 136 and squeeze 32 bytes.

    The documented seam described in the module docstring. ``pad_byte``
    is 0x01 for keccak256 and 0x06 for SHA3-256; everything else about
    the two functions is identical, which is what makes the parity sweep
    in the mirrored test file a check on the permutation itself.

    Takes already-validated input: :func:`keccak256` is the guarded
    entry point. Must not mutate ``data``.
    """
    # Pad a copy. A caller's bytearray must come back untouched, and the
    # zero fill below would otherwise grow it.
    padded = bytearray(data)
    padded.append(pad_byte)
    padded.extend(bytes(-len(padded) % _RATE))
    padded[-1] ^= 0x80

    # Fresh state per call: no digest may depend on an earlier one.
    lanes = [[0] * 5 for _ in range(5)]
    for start in range(0, len(padded), _RATE):
        block = padded[start : start + _RATE]
        for i in range(_LANES_PER_BLOCK):
            word = int.from_bytes(block[i * 8 : i * 8 + 8], "little")
            lanes[i % 5][i // 5] ^= word
        _keccak_f(lanes)

    digest = bytearray()
    for i in range(_LANES_PER_DIGEST):
        digest.extend(lanes[i % 5][i // 5].to_bytes(8, "little"))
    return bytes(digest)


def keccak256(data: bytes) -> bytes:
    """Return the 32-byte keccak256 digest of ``data``.

    The only public name in this module, and the only keccak in the
    package: every selector and every log topic in the release derives
    from it.

    Accepts ``bytes`` and ``bytearray`` only. Any other type, including
    ``str``, ``memoryview`` and ``None``, raises
    :class:`~auradefi.errors.ValidationError` before any work is
    attempted. A ``bytearray`` argument is left unmodified.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ValidationError(
            f"keccak256 takes bytes or bytearray, got {type(data).__name__}"
        )
    return _sponge(bytes(data), 0x01)
