"""A guard a docstring promises must be a guard some test kills.

MOTIVATING FINDING (0.2.0 phase 11, `tests/sources/evm/test_rpc.py`, major).
Round 1 of review found that `EvmRpc` leaked builtins for a wrong-typed
argument. Round 2 fixed it in `rpc.py`, adding a string check on `eth_call`'s
target and a pair check over `batch`'s requests. Round 3 deleted each of those
two guards again and ran the module's 101 tests: 101 passed, twice. The
finding was closed in the source and still open in the suite, because the
agent that writes the fix may not touch test files and no test-author pass was
scheduled behind it.

THE CLASS, which is wider than that one file: a refusal the module documents
in its own `Raises:` block, or writes a comment to justify, that no test in
the tree kills. Such a guard costs nothing to delete. It survives review
precisely because it reads as careful code, and the day it is dropped in a
refactor the suite stays green and a caller starts seeing an AttributeError
from three frames in.

HOW THE ROWS WERE CHOSEN, and why this file rather than a regex. The phase 11
EVM modules were parsed for every `if` whose whole body is a `raise`, 63 of
them. Each condition in turn was replaced with `False` in a scratch copy of
the tree and the suite was run: 52 died, 11 lived. Two of the 11 turned out to
be pinned after all, by `test_a_promised_taxonomy_holds_at_the_entry_door.py`,
which is the sibling gate a previous sweep left behind and the reason the
first finding's `eth_call(to=None)` is covered today. The nine that no test
anywhere killed are the rows below. There is no syntax that separates a pinned
guard from an unpinned one, so no static check can do this work; a table of
calls can, and one call per guard is cheap.

WHY THE ROWS MATCH ON THE MESSAGE. Asserting only that a domain error comes
out is too weak for most of these. Delete the length check at the head of
`decode_aggregate3` and the head-word check one line down refuses the same
input, so a bare `pytest.raises(ValidationError)` stays green over a deleted
guard. The message names which guard spoke, and each row here was verified by
deleting its guard in a scratch copy and watching this file go red.

THE SWEEP WIDENED, twice, and each widening found the class again.

First, to the WHOLE TREE. The EVM sweep above covered 63 guards in four
modules; the same mutation over `src/auradefi` covers 322 in seventy. Running
each mutant against the whole suite rather than the module's own tests, 80
distinct guards outside the original four survived every test in the tree.
That is the same defect, and it is not an EVM habit.

Second, to CLAUSE LEVEL, which is where the finding that ordered this sweep
came from. Replacing a whole condition with `False` is the strongest
mutation there is, so it flatters a compound guard: `not isinstance(topics,
Sequence) or isinstance(topics, (str, bytes))` dies the moment any test hands
it an int, while the second half of it, the half the comment above the line
exists to explain, can be deleted with the suite still green. Dropping ONE
operand at a time adds 127 mutants, and the ones that live are exactly the
clauses whose only justification is a project non-negotiable: `bool` refused
before `int` (rule #8's declare-never-coerce), and `str` excluded from
`Sequence` because Python calls a string a sequence of characters. Both read
as pedantry to a later fixer, and neither leaves a red test behind when it
goes.

WHY THE PYC CACHE ALMOST HID IT. The first pass of the widened sweep reported
these clauses as pinned. They were not: two mutants of one line can have the
SAME source length, and when the second is written within the same second as
the first, CPython's mtime-and-size check reuses the first one's cached
bytecode and the second mutant never runs at all. Any repeat of this work
wants `PYTHONDONTWRITEBYTECODE=1`, and wants a known-killed mutant re-run at
the end as its own control.

Offline by construction (profile rule 9): every transport is an
`httpx.MockTransport`, and most rows touch no transport at all.

DELIBERATELY NOT COVERED. Guards the sweep found pinned, and the remaining
unpinned guards reported to the orders that own them: accounting's plan
validation, the position and embed invariants, the tenancy token gate, and
xpub's two curve refusals, which no input can reach on purpose. A row added
here without its deletion proof is decoration, so the table grows only as
fast as the sweeps that justify it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from auradefi.accounting.acb import AcbPool
from auradefi.assets.caip import parse_caip19
from auradefi.chains import solana
from auradefi.chains.evm import caip2_from_chain_id, chain_id_from_caip2
from auradefi.embed.models import derive_connection_id
from auradefi.errors import AuradefiError
from auradefi.ledger.backends.models import decode_entries
from auradefi.money.decimal_json import quantity_from_wire
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.positions.models import make_group
from auradefi.sources.bitcoin.esplora import Esplora
from auradefi.sources.bitcoin.utxo import Utxo
from auradefi.sources.bitcoin.xpub import parse_xpub
from auradefi.sources.evm.codec import abi, aggregate3
from auradefi.sources.evm.etherscan import EtherscanV2
from auradefi.sources.evm.logs import scan_logs
from auradefi.sources.evm.reader import EvmContractReader
from auradefi.sources.evm.rpc import EvmRpc

#: A well-formed node answer, so nothing here can hang on a real socket.
_BODY = {"jsonrpc": "2.0", "id": 1, "result": "0x1"}

#: An address in the form every EVM boundary in the package accepts.
_ADDRESS = "0x" + "ab" * 20


def _word(value: int) -> bytes:
    """One 32-byte big-endian word, the ABI's unit of layout."""
    return value.to_bytes(32, "big")


def _client(status: int) -> httpx.Client:
    """A client whose every request is answered with `status` and no body."""
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text=""))
    )


def _rpc() -> EvmRpc:
    """An `EvmRpc` on a mock transport, at a url that parses."""
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_BODY))
    )
    return EvmRpc(client, "https://node.invalid/v1")


#: The element with its inner offset moved off 0x40, which is the crafted
#: response a checked decoder is for.
_WRONG_INNER = (
    _word(0x20) + _word(1) + _word(0x20) + _word(1) + _word(0x60) + _word(2)
    + b"\x01\x02" + bytes(30)
)

#: The same response with the element's length word cut off the end.
_SHORT_ELEMENT = _word(0x20) + _word(1) + _word(0x20) + _word(1) + _word(0x40)

# Each row is (the guard it pins, the call, the message fragment that says
# THIS guard refused and not the one after it). The source line is the line
# the guard sat on when the row was written; it is documentation, not an
# assertion, so an edit above it does not fail the gate.
_UNPINNED: list[tuple[str, Callable[[], object], str]] = [
    # abi.py:115. `_parse_type` is reached with whatever a caller put in the
    # types tuple, and `name.startswith` over a non-string is an AttributeError.
    (
        "abi.py:115 _parse_type(name) is a string",
        lambda: abi.encode((None,), (1,)),
        "ABI type must be a string",
    ),
    # abi.py:300. A three-way unpack over a string of length 3 succeeds and
    # encodes its characters, so the shape check is what refuses "abc".
    (
        "abi.py:300 _call_element(call) is a triple and not a string",
        lambda: aggregate3.encode_aggregate3(["abc"]),
        "aggregate3 call must be a triple",
    ),
    # abi.py:342. Without it a truncated element reads short slices as zeros
    # and the length check below reports a zero-length returndata instead, so
    # the fragment has to name the element and not the words "runs past".
    (
        "abi.py:342 the element fits the response",
        lambda: aggregate3.decode_aggregate3(_SHORT_ELEMENT),
        "element at 96 runs past",
    ),
    # abi.py:350. The inner offset is the one field a crafted response moves
    # to point the length word somewhere else in the payload.
    (
        "abi.py:350 the element offsets its bytes to 0x40",
        lambda: aggregate3.decode_aggregate3(_WRONG_INNER),
        "offsets its bytes to",
    ),
    # abi.py:380. Empty return data would otherwise read as head word 0 and be
    # refused by the next guard, with a message about the wrong thing.
    (
        "abi.py:380 the response has a head and a length word",
        lambda: aggregate3.decode_aggregate3(b""),
        "needs a head and a length word",
    ),
    # abi.py:388. A declared count larger than the payload is how a response
    # asks a decoder to read memory it was never sent.
    (
        "abi.py:388 the declared result count fits the response",
        lambda: aggregate3.decode_aggregate3(_word(0x20) + _word(5)),
        "declares 5 results",
    ),
    # reader.py:187. An unknown name with arguments cannot be encoded without
    # guessing ABI types, and a guessed type changes the selector.
    (
        "reader.py:187 an unknown name is refused arguments",
        lambda: EvmContractReader(_rpc()).call(_ADDRESS, "unknownFn", (1,)),
        "signature registry",
    ),
    # reader.py:263. `address` is consumed by .lower() inside the call, so a
    # None target leaks an AttributeError once this line is gone.
    (
        "reader.py:263 the address is a string",
        lambda: EvmContractReader(_rpc()).call(None, "decimals"),
        "needs a string address",
    ),
    # rpc.py:278. The per-entry guard covers a sequence of the wrong entries;
    # this one covers a `requests` that cannot be enumerated at all.
    (
        "rpc.py:278 batch takes a sequence",
        lambda: _rpc().batch(None),
        "needs a sequence of pairs",
    ),
]

#: Rows from the tree-wide, clause-level sweep. Same three fields and the
#: same rule: each was verified by weakening its guard in a scratch copy and
#: watching this file go red. Where the guard is one clause of a compound
#: condition, the comment names the clause, because that is what survives.
_UNPINNED += [
    # evm/logs.py:226, the clause the ordering finding was about. Drop the
    # `(str, bytes)` exclusion and a caller's single topic0 becomes 66 slots
    # of one character each, which a node answers with nothing at all.
    (
        "logs.py:226 a bare string topics argument is not a sequence of topics",
        lambda: scan_logs(_rpc(), from_block=0, to_block=1, topics=7),
        "topics must be a sequence",
    ),
    # abi.py:300, the other half of the row above at abi.py:300. That one
    # pins the string exclusion; drop the `Sequence` clause instead and
    # `len(7)` is a TypeError one line down.
    (
        "abi.py:300 an aggregate3 call that is not a sequence at all",
        lambda: aggregate3.encode_aggregate3([7]),
        "must be a triple",
    ),
    # rpc.py:282. `len(request)` presumes the entry is sized, so the pair
    # check has to run before it and not beside it.
    (
        "rpc.py:282 a batch entry that cannot be sized",
        lambda: _rpc().batch([7]),
        r"must be a \(method, params\) pair",
    ),
    # chains/evm.py:30. `True < 1` is False, so without the bool clause a
    # flag becomes the chain id `eip155:True`.
    (
        "chains/evm.py:30 a bool is not a chain id",
        lambda: caip2_from_chain_id(True),
        "must be a positive integer",
    ),
    # chains/evm.py:45. `fullmatch` over a non-string raises TypeError, past
    # the CaipParseError the docstring promises.
    (
        "chains/evm.py:45 a non-string CAIP-2",
        lambda: chain_id_from_caip2(7),
        "not a canonical eip155",
    ),
    # chains/solana.py:33. `len()` over a non-string is the same leak.
    (
        "chains/solana.py:33 a non-string Solana address",
        lambda: solana.validate_address(7),
        "base58 chars",
    ),
    # caip.py:68. A second slash splits the asset part, and the chain-id
    # pattern below cannot see it.
    (
        "caip.py:68 a CAIP-19 with two slashes",
        lambda: parse_caip19("eip155:1/erc20:0xa/b"),
        "exactly one",
    ),
    # caip.py:73. Without the colon the whole asset part becomes the
    # namespace and the reference is empty.
    (
        "caip.py:73 an asset part with no namespace separator",
        lambda: parse_caip19("eip155:1/erc20"),
        "namespace:reference",
    ),
    # quantity.py:42 and :48. Money is Decimal and int by rule #1, and a
    # string raw would reach arithmetic three frames away.
    (
        "quantity.py:42 a string raw is not an amount",
        lambda: Quantity("1", 0),
        "raw must be an int",
    ),
    (
        "quantity.py:48 a string scale is not a scale",
        lambda: Quantity(1, "0"),
        "decimals must be an int",
    ),
    # fiat.py:70. `"/" in self.currency` one line down is a TypeError for a
    # non-string, so this guard is what keeps the promise.
    (
        "fiat.py:70 a non-string currency",
        lambda: Money(Decimal("1"), 7),
        "currency must be a str",
    ),
    # decimal_json.py:71. The wire scale arrives from a caller's JSON, where
    # a bool is a plausible typo for 0 or 1. The fragment names the WIRE
    # field: delete this guard and `Quantity.__post_init__` refuses the same
    # input one frame later, saying "decimals must be an int" itself.
    (
        "decimal_json.py:71 a wire scale that is not an int",
        lambda: quantity_from_wire({"raw": "1", "decimals": "8"}),
        "wire 'decimals' must be an int",
    ),
    # acb.py:91 and :104. A pool carries basis; a string here would survive
    # to the first addition.
    (
        "acb.py:91 a string pooled quantity",
        lambda: AcbPool("USD", quantity_raw="1"),
        "quantity_raw must be an int",
    ),
    (
        "acb.py:104 a string pool scale",
        lambda: AcbPool("USD", decimals="8"),
        "decimals must be an int",
    ),
    # ledger/backends/models.py:118 and :124. These rows come out of the
    # HOST's database, so a JSON number may have been written by something
    # that is not auradefi, and `1e77` parses to a float off by 10**60.
    (
        "backends/models.py:118 a JSON number amount from the host database",
        lambda: decode_entries(
            json.dumps([{"asset_id": "a", "raw": 1, "decimals": 0, "direction": "in"}])
        ),
        r"decimal-int string \(rule #2\)",
    ),
    (
        "backends/models.py:124 an amount with an underscore",
        lambda: decode_entries(
            json.dumps(
                [{"asset_id": "a", "raw": "1_0", "decimals": 0, "direction": "in"}]
            )
        ),
        "not a decimal int",
    ),
    # utxo.py:30 and :44. A UTXO is money; every field is checked before it
    # is counted.
    (
        "utxo.py:30 a non-string txid",
        lambda: Utxo(txid=7, vout=0, value_sats=1, confirmed=True),
        "txid must be a str",
    ),
    (
        "utxo.py:44 a string vout",
        lambda: Utxo(txid="ab", vout="0", value_sats=1, confirmed=True),
        "vout must be an int",
    ),
    # xpub.py:202. base58check_decode iterates its argument, so a bytes xpub
    # would be decoded character by character rather than refused.
    (
        "xpub.py:202 a non-string xpub",
        lambda: parse_xpub(7),
        "xpub must be a str",
    ),
    # embed/models.py:92. The guard exists because a seam audit swapped the
    # two trailing arguments and got a plausible connection id back.
    (
        "embed/models.py:92 address and chain_id swapped",
        lambda: derive_connection_id("tnt_1", "eip155:1", _ADDRESS),
        "must be CAIP-2",
    ),
    # positions/models.py:281. An empty group would sum to a zero total that
    # reads as a real valuation.
    (
        "positions/models.py:281 a group with no positions",
        lambda: make_group(()),
        "at least one position",
    ),
    # esplora.py:129 and etherscan.py:173. Neither non-2xx door was killed by
    # anything in the tree, so an upstream 500 could start returning an error
    # page parsed as data.
    (
        "esplora.py:129 a non-2xx from the Esplora node",
        lambda: Esplora(_client(500), "https://esplora.invalid").address_stats(
            "bc1qexample"
        ),
        "esplora HTTP 500",
    ),
    (
        "etherscan.py:173 a non-2xx from Etherscan",
        lambda: EtherscanV2(_client(500), base_url="https://scan.invalid").balances(
            "eip155:1", _ADDRESS
        ),
        "etherscan HTTP 500",
    ),
]


#: THE SWEEP RUN AGAIN, after the round that closed the rows above.
#:
#: MOTIVATING FINDING (0.2.0 phase 11, `tests/sources/evm/test_multicall.py`,
#: major). `multicall.py`'s `_check_calls` was added by a fix round and the
#: fixer said in his own report that the pin was owed to a test file he was
#: not allowed to edit. That is the class arriving on a schedule rather than
#: by accident: a fix round adds a guard, the round ends, and whether the
#: guard is ever pinned depends on a second agent being booked behind the
#: first. So the sweep was re-run over every guard in the six phase-11
#: modules, 99 mutants counting one per dropped clause, each against the whole
#: suite with `PYTHONDONTWRITEBYTECODE=1`. `_check_calls` came back PINNED, by
#: `test_a_batch_that_is_not_a_sequence_is_refused_before_any_http` and
#: `test_a_batch_element_that_is_not_a_call_is_refused_before_any_http`, both
#: landed while the sweep ran. 97 of the 99 died. The row below is one of the
#: two that lived.
#:
#: The other survivor gets no row and the reason is worth writing down.
#: `multicall.py:259` guards `_results` with `not isinstance(result, str) or
#: _RESULT_HEX.fullmatch(result) is None`, and the isinstance half cannot be
#: killed through any public door, because `EvmRpc.eth_call` refuses a
#: non-string result one frame earlier (rpc.py:202, killed by the suite). It
#: is defence in depth behind a door that already holds, and `reader.py:211`
#: writes the same check without the isinstance half for the same reason.
#: Pinning it would mean calling a private staticmethod directly, which
#: freezes an internal instead of a promise. Reported, not gated.
_UNPINNED += [
    # rpc.py:324, the `isinstance(number, bool)` clause of the batch id
    # check. `True` is an `int` whose value is 1, so with that clause gone a
    # node answering `{"id": true}` is silently accepted as the answer to
    # request 1: `1 <= True <= 1` holds and `answers[True]` and `answers[1]`
    # are the same dict key. Nothing raises and nothing logs; the caller gets
    # a wrong-but-plausible answer matched to the wrong request, which is the
    # failure the by-id discipline exists to prevent. Rule #8's declare, never
    # coerce, at the id level. The message fragment names this guard: the
    # duplicate and out-of-range guards below it speak differently.
    (
        "rpc.py:324 a batch answer whose id is the bool True",
        lambda: _batch_answered_with({"jsonrpc": "2.0", "id": True, "result": "0x1"}),
        "carries no usable id",
    ),
]


def _batch_answered_with(item: dict) -> object:
    """A one-request `EvmRpc.batch` whose node answers with `item`.

    Written as a helper rather than inline because the batch answer is a JSON
    ARRAY, which the module-level `_rpc` above does not model: its handler
    returns a single envelope, and a batch against it fails on the body-shape
    guard long before the id is read.
    """
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[item]))
    )
    return EvmRpc(client, "https://node.invalid/v1").batch([("eth_blockNumber", [])])


@pytest.mark.parametrize("guard,call,message", _UNPINNED, ids=lambda v: v)
def test_a_documented_refusal_is_the_one_that_speaks(
    guard: str, call: Callable[[], object], message: str
) -> None:
    """Each guard refuses its own case, in its own words.

    Red when the guard is deleted, either because nothing raises or because
    the refusal that answers instead is a different one. The class asserted
    is the package root, since which class a module owes for which failure
    belongs to that module's own tests; this file is about the message.
    """
    with pytest.raises(AuradefiError, match=message):
        call()
