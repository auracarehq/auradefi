"""A spelling that parses but is not canonical must be REFUSED, never accepted.

MOTIVATING FINDING (0.2.0 phase 11, `src/auradefi/sources/evm/codec/abi.py:126`,
spec-fidelity, major). `_parse_type` read an integer width with plain `int()`,
so `uint08`, `int024`, `uint0256` and `uint` followed by forty zeros and an 8
all parsed as their canonical twins. Every one of them ENCODES identically to
`uint8` / `int24` / `uint256`, and every one of them hashes to a different
four-byte selector: `selector(function_signature('f', ('uint8',)))` is
`3120d434`, `uint08` gives `fe73da5e`. A single typo in `reader.py`'s
`SIGNATURES` table would therefore have put perfectly well-formed argument
words behind a selector for a function the contract does not have, and the
only symptom would have been a revert from a node.

WHY THE CLASS IS DANGEROUS. It needs two properties that keep showing up
together in this codebase:

* the same string is BOTH parsed for its meaning (an int, a chain id, a port)
  and hashed or concatenated into a wire identifier (a selector, `ast_`,
  `grp_`, `conn_`, `whe_`, a request key);
* the parse is lenient enough that two spellings mean the same thing.

Those two together mean the value and its identity disagree: the code computes
the right bytes and files them under the wrong name. Nothing raises, no test
goes red on the module that did it, and the damage lands in another system
entirely (a node, a stored row, an id a client already holds). Normalising
instead of refusing does not save you either, unless EVERY site that hashes
the string normalises first, which is a coupling no type checker enforces.
`webhooks/urls.py` states the discipline in one line and is the model here:
"No normalisation whatsoever ... because endpoint_id hashes the exact string",
so it refuses `h.t:080` outright.

THE RULES, mechanically:

1. Every parser below that PROMISES a canonical form must refuse each listed
   non-canonical spelling of a value it accepts in canonical form. The
   spellings are the cheap ones a human or a JSON producer actually writes:
   a leading zero, a unicode digit, a redundant default.
2. Every ABI type name in `reader.SIGNATURES` must be canonical by
   `abi._parse_type`'s own reckoning, since `function_signature` hashes those
   literals verbatim into the selector. This catches the registry typo the
   finding is about at import time rather than at revert time.

Adding a parser here is cheap and the table is meant to grow: any new function
that both canonicalises a string and feeds a hash belongs in it.
"""

from __future__ import annotations

import pytest

from auradefi.assets.caip import parse_caip19
from auradefi.chains.evm import chain_id_from_caip2
from auradefi.errors import AuradefiError
from auradefi.ledger.cursors import decode_cursor
from auradefi.sources.evm.codec.abi import _parse_type
from auradefi.sources.evm.reader import DEFAULT_RETURN_TYPES, SIGNATURES
from auradefi.webhooks.urls import validate_endpoint_url

_ADDRESS = "0x" + "a" * 40

#: `(parser, spelling, why it is not canonical)`. Each spelling denotes a
#: value the same parser accepts in exactly one other spelling, and each
#: parser's output reaches a hash preimage or a selector.
NON_CANONICAL = [
    (_parse_type, "uint08", "uint8 with a leading zero: another selector"),
    (_parse_type, "uint0256", "uint256 with a leading zero"),
    (_parse_type, "int024", "int24 with a leading zero"),
    (_parse_type, "uint" + "0" * 40 + "8", "uint8 padded past any width"),
    (_parse_type, "uint٨", "an Arabic-Indic 8 that int() would read"),
    (chain_id_from_caip2, "eip155:01", "chain 1 with a leading zero"),
    (chain_id_from_caip2, "eip155:0000001", "chain 1, padded"),
    (parse_caip19, "eip155:1/slip44:007", "slip44 60 written 007"),
    (parse_caip19, f"eip155:01/erc20:{_ADDRESS}", "chain 1 with a leading zero"),
    (parse_caip19, f"eip155:0000001/erc20:{_ADDRESS}", "chain 1, padded"),
    (validate_endpoint_url, "http://h.t:080/x", "port 80 with a leading zero"),
    (validate_endpoint_url, "http://h.t:80/x", "the scheme's default port"),
    (decode_cursor, "٠" * 20, "twenty Arabic-Indic zeros"),
]


@pytest.mark.parametrize(
    "parser,spelling,why",
    NON_CANONICAL,
    ids=[f"{p.__module__.split('.')[-1]}:{s}" for p, s, _ in NON_CANONICAL],
)
def test_non_canonical_spelling_is_refused(parser, spelling, why):
    """The canonical spelling is the only one that parses (see module docstring)."""
    try:
        accepted = parser(spelling)
    except AuradefiError:
        return
    pytest.fail(f"{parser.__name__} accepted {spelling!r} as {accepted!r}: {why}")


def _registry_type_names():
    """Every ABI type literal `function_signature` will hash, once each."""
    names = set(DEFAULT_RETURN_TYPES)
    for arg_types, return_types in SIGNATURES.values():
        names.update(arg_types)
        names.update(return_types)
    return sorted(names)


@pytest.mark.parametrize("name", _registry_type_names())
def test_registry_type_name_is_canonical(name):
    """A typo in SIGNATURES is a selector for a function that does not exist."""
    kind, bits = _parse_type(name)
    if bits:
        assert name == f"{kind}{bits}", f"SIGNATURES spells {kind}{bits} as {name!r}"
