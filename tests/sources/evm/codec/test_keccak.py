"""Golden vectors for keccak-f[1600] and keccak256 (RELEASE_0.2.0 §3, §4).

Every expected digest below is a hardcoded literal. Three of them are
published values a reader can check without running this repository: the
Ethereum empty-input digest, keccak256(b"abc"), and the ERC-20 Transfer
topic0. The two selectors are published as well, on any 4byte directory.
The four rate-boundary digests reach past what the published set covers,
so they were derived from the pinned construction alone (state 5x5 lanes
little-endian, rate 136, 24 rounds, ROT and RC as pinned in the module
docstring, pad 0x01 then XOR 0x80 into the last byte of the padded
input) by an independent throwaway implementation, and corroborated by
the SHA3-256 parity sweep below, which walks every length from 0 to 299
and therefore crosses the same block boundaries with the stdlib holding
the answer.

That sweep is the load-bearing test in this file. keccak256 and SHA3-256
share a rate, a permutation and a squeeze, and differ in exactly one
byte of padding, so running this module's own sponge with pad byte 0x06
and comparing against ``hashlib.sha3_256`` checks the permutation and
the 136-byte rate boundary without a second keccak implementation and
without a network fixture.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import sys
from pathlib import Path

import pytest

from auradefi.errors import ValidationError
from auradefi.sources.evm.codec import keccak
from auradefi.sources.evm.codec.keccak import _sponge, keccak256

#: Published: the keccak256 of the empty string, and RELEASE_0.2.0 §4's
#: own Done-when value.
EMPTY_DIGEST = "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"

#: Published: SHA3-256 of the empty string. Present so the padding
#: difference is asserted mechanically and is not a comment.
SHA3_EMPTY_DIGEST = "a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a"

PUBLISHED_VECTORS = [
    (b"abc", "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"),
    # topic0 of the ERC-20 Transfer event, on every mainnet transfer log.
    (
        b"Transfer(address,address,uint256)",
        "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
    ),
    (
        b"Approval(address,address,uint256)",
        "8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925",
    ),
]

#: Signature to four-byte selector. Published in any 4byte directory.
SELECTORS = [
    (b"balanceOf(address)", "70a08231"),
    (b"getExchangeRate()", "e6aa216c"),
    (b"transfer(address,uint256)", "a9059cbb"),
    (b"decimals()", "313ce567"),
]

#: Lengths either side of the 136-byte rate. 135 is the case where the
#: 0x01 pad byte and the 0x80 land in the same byte of a single block;
#: 136 is a whole extra all-padding block; 137 is the first two-block
#: message; 272 is exactly two rates, so a third block again.
RATE_BOUNDARY_VECTORS = [
    (135, "34367dc248bbd832f4e3e69dfaac2f92638bd0bbd18f2912ba4ef454919cf446"),
    (136, "a6c4d403279fe3e0af03729caada8374b5ca54d8065329a3ebcaeb4b60aa386e"),
    (137, "d869f639c7046b4929fc92a4d988a8b22c55fbadb802c0c66ebcd484f1915f39"),
    (272, "cf7fcd4f705ee749930d19ca84561a9bf62516bd90a471545fa2f49fdc7e63c8"),
]

NON_BYTES = ["abc", "", None, 0, 1, 3.5, memoryview(b"abc"), [1, 2, 3], (), {"a": 1}]


# pins: keccak256 of the empty input is the Ethereum empty-input digest, so
#       an input shorter than the rate is absorbed as one all-padding block.
def test_keccak256_of_the_empty_input_is_the_release_done_when_vector() -> None:
    assert keccak256(b"").hex() == EMPTY_DIGEST


# pins: keccak256 reproduces the published digests for inputs below the rate.
@pytest.mark.parametrize(("message", "digest_hex"), PUBLISHED_VECTORS)
def test_published_keccak256_vectors(message: bytes, digest_hex: str) -> None:
    assert keccak256(message).hex() == digest_hex


# pins: the pad byte is 0x01 and not 0x06, so keccak256 is keccak and not the
#       stdlib SHA3-256, for which hashlib already exists.
def test_keccak256_is_not_sha3_256() -> None:
    assert hashlib.sha3_256(b"").hexdigest() == SHA3_EMPTY_DIGEST
    assert keccak256(b"").hex() != hashlib.sha3_256(b"").hexdigest()
    assert keccak256(b"abc").hex() != hashlib.sha3_256(b"abc").hexdigest()
    for length in (135, 136, 137):
        message = b"a" * length
        assert keccak256(message) != hashlib.sha3_256(message).digest()


# pins: the digest is squeezed in little-endian lane order, so the first four
#       bytes ARE the Ethereum selector with no further byte reversal.
@pytest.mark.parametrize(("signature", "selector"), SELECTORS)
def test_the_first_four_digest_bytes_are_the_published_selector(
    signature: bytes, selector: str
) -> None:
    assert keccak256(signature)[:4].hex() == selector
    # The reversal an implementer reaches for when a selector looks wrong.
    assert keccak256(signature)[:4].hex() != selector[::-1]


# pins: the rate is 136 bytes, so a message at, just under, and just over one
#       and two full blocks digests to the pinned values.
@pytest.mark.parametrize(("length", "digest_hex"), RATE_BOUNDARY_VECTORS)
def test_rate_boundary_digests(length: int, digest_hex: str) -> None:
    assert keccak256(b"a" * length).hex() == digest_hex


# pins: the permutation, the lane order and the 136-byte rate are correct,
#       because swapping only the pad byte for 0x06 turns this sponge into
#       SHA3-256 for every message length from 0 to 299.
def test_the_same_sponge_with_pad_byte_0x06_is_sha3_256() -> None:
    for length in range(300):
        message = b"a" * length
        assert _sponge(message, 0x06) == hashlib.sha3_256(message).digest(), length


# pins: keccak256 IS this module's sponge at pad byte 0x01, so the SHA3-256
#       parity sweep above constrains the public function and not a helper
#       that only the tests reach.
def test_keccak256_is_the_sponge_at_pad_byte_0x01() -> None:
    for message in (b"", b"abc", b"a" * 135, b"a" * 136, b"a" * 137, bytes(range(256))):
        assert keccak256(message) == _sponge(message, 0x01)


# pins: the digest is always exactly 32 bytes, of type bytes, whatever the
#       input length.
@pytest.mark.parametrize("length", [0, 1, 31, 32, 33, 135, 136, 137, 271, 272, 1000])
def test_the_digest_is_always_thirty_two_bytes(length: int) -> None:
    digest = keccak256(bytes(length))
    assert type(digest) is bytes
    assert len(digest) == 32


# pins: a bytearray is accepted and digests identically to the same bytes.
def test_a_bytearray_digests_the_same_as_the_equivalent_bytes() -> None:
    for message in (b"", b"abc", b"a" * 136, b"a" * 137):
        assert keccak256(bytearray(message)) == keccak256(message)


# pins: padding is applied to a copy, so a bytearray the caller still holds is
#       unchanged by the call.
def test_a_bytearray_argument_is_not_mutated() -> None:
    buffer = bytearray(b"Transfer(address,address,uint256)")
    before = bytes(buffer)
    digest = keccak256(buffer)
    assert bytes(buffer) == before
    assert len(buffer) == len(before)
    assert digest.hex() == (
        "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )


# pins: anything that is not bytes or bytearray is rejected with
#       ValidationError, before any hashing is attempted.
@pytest.mark.parametrize("bad", NON_BYTES, ids=lambda value: type(value).__name__ + repr(value)[:12])
def test_non_bytes_input_raises_validation_error(bad: object) -> None:
    with pytest.raises(ValidationError):
        keccak256(bad)  # type: ignore[arg-type]


# pins: a str is rejected rather than silently encoded, so a caller who passes
#       a signature as text learns it here and not from a wrong selector.
def test_a_str_signature_is_rejected_rather_than_encoded() -> None:
    with pytest.raises(ValidationError):
        keccak256("Transfer(address,address,uint256)")  # type: ignore[arg-type]


# pins: no lane state survives a call, so a digest does not depend on what was
#       hashed before it.
def test_repeated_calls_do_not_carry_state() -> None:
    first = keccak256(b"")
    keccak256(b"a" * 500)
    keccak256(b"Transfer(address,address,uint256)")
    assert keccak256(b"").hex() == EMPTY_DIGEST
    assert keccak256(b"") == first
    assert keccak256(b"abc").hex() == PUBLISHED_VECTORS[0][1]


# pins: the module imports nothing outside the stdlib and auradefi.errors, so
#       the no-new-dependency rule holds for the whole EVM codec path.
def test_the_module_imports_only_the_stdlib_and_auradefi_errors() -> None:
    """Walks the whole AST, so a function-local ``import pysha3`` fails too."""
    tree = ast.parse(Path(keccak.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    foreign = {
        name
        for name in imported
        if name != "auradefi.errors"
        and name.split(".")[0] not in sys.stdlib_module_names
    }
    assert not foreign, f"forbidden imports: {sorted(foreign)}"


# pins: importing the module opens no socket and does no work at import time.
def test_importing_the_module_opens_no_socket() -> None:
    """The autouse offline guard in tests/conftest.py raises on connect."""
    reloaded = importlib.reload(keccak)
    assert reloaded.keccak256(b"").hex() == EMPTY_DIGEST


# pins: keccak256 is the only public name, so callers cannot start depending
#       on the sponge internals as if they were the interface.
def test_keccak256_is_the_only_public_function() -> None:
    assert keccak.__all__ == ["keccak256"]
    public = sorted(
        name
        for name, value in vars(keccak).items()
        if not name.startswith("_")
        and inspect.isfunction(value)
        and value.__module__ == keccak.__name__
    )
    assert public == ["keccak256"]
