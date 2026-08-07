"""CAIP-19 asset identifiers: parse, format, canonicalize (SPEC §4.2, rule #3).

Grammar (the Phase 0 subset)::

    caip19    = chain_id "/" namespace ":" reference
    chain_id  = CAIP-2, e.g. "eip155:1", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
    namespace = "erc20" | "slip44" | "token"

Both halves must be canonical, not merely well formed. Where a chain
namespace has a canonical spelling this module knows, the CAIP-2 half is
validated against it (``eip155:01`` is refused, not read as chain 1); see
``_CHAIN_ID_VALIDATORS``.

Reference rules per namespace: canonical form feeds the pinned asset-id
hash (docs/internal/DECISIONS.md), so this is a wire-format contract:

* ``erc20``: literal ``0x`` + exactly 40 hex digits, either case in;
  canonical form is fully LOWERCASED.
* ``slip44``, canonical base-10 integer: digits only, no sign, no
  leading zeros (bare ``0`` itself is valid, Bitcoin).
* ``token``, base58, Bitcoin alphabet (no ``0``, ``O``, ``I``, ``l``);
  case is PRESERVED, Solana mints are case-sensitive.

Anything else, unknown namespace, missing or extra parts, a malformed
reference, surrounding whitespace, non-string input, raises
CaipParseError. stdlib only; may import money/ and chains/ only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from auradefi.chains.evm import chain_id_from_caip2
from auradefi.errors import CaipParseError

# CAIP-2 chain id: lowercase namespace, one colon, alphanumeric reference.
_CHAIN_ID = re.compile(r"[-a-z0-9]{3,8}:[-_a-zA-Z0-9]{1,32}")

#: Per-namespace canonical-form validators for the CAIP-2 half.
#:
#: The generic grammar above is CAIP-2's own, and it is deliberately
#: permissive: any alphanumeric reference is well formed. That is the wrong
#: contract for a string that feeds the pinned asset-id hash. ``eip155:01``
#: and ``eip155:1`` are one chain written two ways, so a token on it minted
#: two asset ids, and every id derived over one of those (position, ledger
#: row, transaction) forked with it. The reference half of this module has
#: refused non-canonical spellings since Phase 0 ("no leading zeros", and
#: ``slip44:007`` is a pinned refusal); the chain half never did.
#:
#: ``chains/evm.py`` already owns the canonical eip155 rule and its
#: ``[1-9][0-9]*`` pattern, so this calls it rather than restating it. One
#: definition cannot drift from itself.
_CHAIN_ID_VALIDATORS = {"eip155": chain_id_from_caip2}
# Per-namespace reference validators; canonical form feeds the asset-id hash.
_ERC20_REFERENCE = re.compile(r"0x[0-9a-fA-F]{40}")
_SLIP44_REFERENCE = re.compile(r"0|[1-9][0-9]*")
_TOKEN_REFERENCE = re.compile(r"[1-9A-HJ-NP-Za-km-z]+")


@dataclass(frozen=True, slots=True)
class Caip19:
    """A parsed CAIP-19 identifier, already in canonical form.

    ``parse_caip19`` is the validating constructor; the dataclass itself
    performs no validation.
    """

    chain_id: str
    namespace: str
    reference: str


def parse_caip19(value: str) -> Caip19:
    """Parse ``value`` into a canonical :class:`Caip19`.

    The returned parts are canonical: an ``erc20`` reference comes back
    lowercased; ``slip44`` and ``token`` references come back untouched.

    Raises:
        CaipParseError: on any input that is not a well-formed CAIP-19
            in a supported namespace (see module docstring), including
            non-string input.
    """
    if not isinstance(value, str):
        raise CaipParseError(
            f"CAIP-19 must be a string, got {type(value).__name__}"
        )
    chain_id, slash, asset_part = value.partition("/")
    if not slash or "/" in asset_part:
        raise CaipParseError(f"not a CAIP-19 (need exactly one '/'): {value!r}")
    if _CHAIN_ID.fullmatch(chain_id) is None:
        raise CaipParseError(f"malformed CAIP-2 chain id in: {value!r}")
    validator = _CHAIN_ID_VALIDATORS.get(chain_id.partition(":")[0])
    if validator is not None:
        validator(chain_id)  # CaipParseError on a non-canonical spelling
    namespace, colon, reference = asset_part.partition(":")
    if not colon:
        raise CaipParseError(f"asset part needs 'namespace:reference': {value!r}")
    return Caip19(
        chain_id=chain_id,
        namespace=namespace,
        reference=_canonical_reference(namespace, reference, value),
    )


def _canonical_reference(namespace: str, reference: str, value: str) -> str:
    """Validate ``reference`` for ``namespace`` and return its canonical form."""
    if namespace == "erc20":
        if _ERC20_REFERENCE.fullmatch(reference) is None:
            raise CaipParseError(f"erc20 reference must be 0x+40 hex: {value!r}")
        return reference.lower()
    if namespace == "slip44":
        if _SLIP44_REFERENCE.fullmatch(reference) is None:
            raise CaipParseError(
                f"slip44 reference must be a canonical base-10 integer: {value!r}"
            )
        return reference
    if namespace == "token":
        if _TOKEN_REFERENCE.fullmatch(reference) is None:
            raise CaipParseError(f"token reference must be base58: {value!r}")
        return reference
    raise CaipParseError(f"unsupported CAIP-19 namespace {namespace!r} in: {value!r}")


def format_caip19(parsed: Caip19) -> str:
    """Serialise ``parsed`` back to ``{chain_id}/{namespace}:{reference}``.

    Round-trip law: ``parse_caip19(format_caip19(parse_caip19(s)))``
    equals ``parse_caip19(s)`` for every parseable ``s``.
    """
    return f"{parsed.chain_id}/{parsed.namespace}:{parsed.reference}"


def canonical_caip19(value: str) -> str:
    """Return the canonical string form of ``value``.

    Equivalent to ``format_caip19(parse_caip19(value))``: EVM (erc20)
    addresses are lowercased, everything else is untouched. Idempotent.

    Raises:
        CaipParseError: as :func:`parse_caip19`.
    """
    return format_caip19(parse_caip19(value))
