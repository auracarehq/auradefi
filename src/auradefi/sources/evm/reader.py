"""The concrete ContractReader: one ``eth_call`` per read (RELEASE_0.2.0 §4).

This is the module that lets an adapter ask a deployed contract a
question. ``rpc.py`` owns the JSON-RPC envelope and ``codec/abi.py`` owns
the words; everything left over is here, and it is small: resolve the
function's types, encode, post, decode, unwrap.

STRUCTURAL BINDING, and why it has to be structural. The adapter seam is
the ``ContractReader`` protocol declared in ``positions/protocol.py``, and
the layering gate (``tests/style/test_layering.py``) forbids ``sources``
from importing that domain in any form: not at module scope, not inside a
function body, not under ``TYPE_CHECKING``. So this class names the
protocol nowhere and matches it by shape instead. ``call``'s parameter
names, their order and the ``args`` default are therefore part of the
contract, and the proof is a ``runtime_checkable`` ``isinstance`` in the
mirrored test file, where the import is allowed.

THE REGISTRY IS DATA. :data:`SIGNATURES` maps a function NAME to its
``(arg_types, return_types)``, taken from §4's call-surface table:

    balanceOf           (address,)                  -> (uint256,)
    decimals            ()                          -> (uint8,)
    totalSupply         ()                          -> (uint256,)
    token0              ()                          -> (address,)
    token1              ()                          -> (address,)
    getReserves         ()                          -> (uint112,uint112,uint32)
    allPairsLength      ()                          -> (uint256,)
    allPairs            (uint256,)                  -> (address,)
    slot0               ()                          -> (uint160,int24,uint16,
                                                        uint16,uint16,uint8,bool)
    positions           (uint256,)                  -> twelve words, see below
    getPool             (address,address,uint24)    -> (address,)
    tokenOfOwnerByIndex (address,uint256)           -> (uint256,)
    getUserAccountData  (address,)                  -> six uint256 words
    getExchangeRate     ()                          -> (uint256,)

``positions`` returns ``(uint96, address, address, address, uint24,
int24, int24, uint128, uint256, uint256, uint128, uint128)``.

ONE DECLARED OPEN SHAPE. §4's table ends with a row spelled "receipt
``rate_fn``", which is host data and not a function name:
``adapters/tokens.py`` declares ``rate_fn: str | None`` on
``ReceiptToken`` and the Rocket Pool receipt supplies the only shipped
value, ``getExchangeRate``. Keying the registry literally would give it a
``rate_fn`` key, no ``getExchangeRate`` key, and an unknown-fn failure on
the Rocket Pool read. So ``getExchangeRate`` is the key, and a host that
declares a new receipt gets one declared fallback instead of a registry
edit: an unknown name called with NO arguments resolves as
:data:`DEFAULT_RETURN_TYPES`, a zero-argument function returning one
``uint256``, which is exactly what ``ReceiptToken.rate_fn`` promises. An
unknown name called WITH arguments is refused with
:class:`~auradefi.errors.ValidationError` naming the function, before any
HTTP, because the codec would have to guess the argument types and a
guessed selector reaches a function the contract does not have.

THE LENGTH-1 UNWRAP LIVES HERE AND NOWHERE ELSE. ``abi.decode`` always
returns a tuple; this module returns the bare value when the function
declares one return type. Two layers that both unwrap is a defect neither
can see alone.

FAILURE CHANNEL. Every wire failure raises
:class:`~auradefi.errors.SourceError`: a node JSON-RPC error, which is
what a reverting ``eth_call`` produces and which ``rpc.py`` already
raises; a result of ``'0x'`` where words were expected; a result whose
length is not ``32 * len(return_types)``; a malformed word (an address
with dirty high bytes, a bool that is not 0 or 1, an integer too wide for
its type), each wrapped from abi's ``ValidationError``. There is no zero
and no ``None`` on this path. Every adapter call site coerces the result
immediately, so a ``None`` return would surface as a ``TypeError`` inside
the adapter instead of a contained ``AuradefiError``, and
``positions/resolve.py`` files a raised ``SourceError`` as adapter-failure
data.

BLOCK PINNING IS FIXED AT CONSTRUCTION, because the protocol's ``call``
carries no block parameter. ``block_number=None`` reads ``latest``, an
int reads that block, and a caller that wants two blocks builds two
readers.

Layering: ``auradefi.errors``, ``codec/abi.py`` and ``rpc.py`` only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

from auradefi.errors import (
    SourceError,
    ValidationError,
    require_int,
    require_sequence,
)
from auradefi.sources.evm.codec.abi import (
    decode,
    encode,
    function_signature,
    selector,
)
from auradefi.sources.evm.rpc import EvmRpc, block_tag

__all__ = ["DEFAULT_RETURN_TYPES", "SIGNATURES", "EvmContractReader"]

#: A returned blob: the ``0x`` prefix and an EVEN number of hex digits.
#: The prefix is required and checked, never assumed. ``result[2:]`` over
#: an unprefixed but otherwise well-formed word eats its first two hex
#: digits and shifts the rest left, which decodes into a plausible wrong
#: answer instead of failing. The even count is here because
#: ``bytes.fromhex`` refuses an odd one with a ValueError, and so does a
#: digit that is not hex: both are outside the taxonomy this door
#: promises. ``multicall.py`` guards its own returndata the same way.
_RESULT_HEX = re.compile("0x(?:[0-9a-fA-F]{2})*")

#: What an unknown zero-argument function is assumed to return: one
#: ``uint256`` word. The open shape a host's ``rate_fn`` travels through.
DEFAULT_RETURN_TYPES: tuple[str, ...] = ("uint256",)

#: The declared call surface as data, ``fn`` to ``(arg_types,
#: return_types)``, exactly the table in this module's docstring. A name
#: absent here is resolved by the open shape above when it is called with
#: no arguments, and refused when it is called with any.
#:
#: Read-only through a ``MappingProxyType`` because it is shared module
#: state: a reader that memoised a resolved name into it would make the
#: table depend on what ran first, and the same host would then encode
#: one function two ways across two processes.
SIGNATURES: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = MappingProxyType(
    {
        "balanceOf": (("address",), ("uint256",)),
        "decimals": ((), ("uint8",)),
        "totalSupply": ((), ("uint256",)),
        "token0": ((), ("address",)),
        "token1": ((), ("address",)),
        "getReserves": ((), ("uint112", "uint112", "uint32")),
        "allPairsLength": ((), ("uint256",)),
        "allPairs": (("uint256",), ("address",)),
        "slot0": (
            (),
            ("uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"),
        ),
        "positions": (
            ("uint256",),
            (
                "uint96",
                "address",
                "address",
                "address",
                "uint24",
                "int24",
                "int24",
                "uint128",
                "uint256",
                "uint256",
                "uint128",
                "uint128",
            ),
        ),
        "getPool": (("address", "address", "uint24"), ("address",)),
        "tokenOfOwnerByIndex": (("address", "uint256"), ("uint256",)),
        "getUserAccountData": (("address",), ("uint256",) * 6),
        # The rate function under its ON-CHAIN name. §4's table spells this
        # row "receipt rate_fn", which is the host data field carrying the
        # name and not a name itself: `adapters/tokens.py` declares
        # `rate_fn: str | None` and the Rocket Pool receipt supplies
        # "getExchangeRate". A registry keyed literally would hold a
        # `rate_fn` key that no call site ever asks for, and would meet the
        # one read that matters as an unknown function.
        "getExchangeRate": ((), ("uint256",)),
    }
)


def _resolve(
    fn: str, args: tuple[object, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(arg_types, return_types)`` for ``fn``, or a refusal.

    A registered name gets its declared row. An unregistered one gets the
    open shape, ``() -> uint256``, but only when it was called with no
    arguments: with arguments the codec would have to guess their types,
    and a guessed type changes the signature, so the selector would go out
    for a function the contract does not have. The refusal names ``fn``,
    which is the one thing the codec cannot do for a name it has never
    heard of.

    Raises:
        ValidationError: on an unknown name called with arguments.
    """
    row = SIGNATURES.get(fn)
    if row is not None:
        return row
    if args:
        raise ValidationError(
            f"{fn} is not in the reader's signature registry, and the open "
            f"shape covers zero-argument reads only: {len(args)} arguments "
            "would need their ABI types guessed"
        )
    return ((), DEFAULT_RETURN_TYPES)


def _decoded(
    fn: str, return_types: tuple[str, ...], result: str
) -> tuple[object, ...]:
    """The node's ``eth_call`` result as its declared words.

    Every failure here is a :class:`~auradefi.errors.SourceError` because
    the bytes are the node's and not the caller's: an empty ``0x`` where a
    word was due, a length that is not ``32 * len(return_types)``, and any
    word that does not fit its type. Read as zero instead, a call to a
    non-contract address would report an empty account as a zero balance.

    Raises:
        SourceError: on a result that is not ``0x`` plus an even number of
            hex digits, and on abi's ValidationError over the words.
    """
    if _RESULT_HEX.fullmatch(result) is None:
        raise SourceError(f"{fn} result is not 0x hex: {result!r}")
    try:
        return decode(return_types, bytes.fromhex(result[2:]))
    except ValidationError as exc:
        # SourceError at this door, with the abi failure as __cause__. The
        # same ValidationError over a caller's arguments stays a
        # ValidationError; what changes it here is where the bytes came
        # from. `positions/resolve.py` files this as adapter-failure data.
        raise SourceError(f"{fn} result did not decode: {exc}") from exc


class EvmContractReader:
    """One ``eth_call`` per read, pinned at a block chosen at construction.

    Binds the adapter seam structurally: ``call``'s name, parameter names,
    order and default match the protocol exactly, and this module never
    names that package.
    """

    def __init__(self, rpc: EvmRpc, block_number: int | None = None) -> None:
        """Bind the transport and the block pin. Performs NO I/O.

        The pin is refused here even though nothing reads it until a
        ``call``. A reader built around a bad pin is already wrong, and
        the alternative surfaces it one method later with a builtin from
        ``hex()``, which is the wrong exception at the wrong moment.
        """
        self._rpc = rpc
        if block_number is not None:
            require_int(block_number, "block_number", ValidationError)
        self._block_number = block_number

    def call(
        self, address: str, fn: str, args: tuple[object, ...] = ()
    ) -> object:
        """Return the decoded result of ``fn(*args)`` at ``address``.

        Resolves ``(arg_types, return_types)`` from :data:`SIGNATURES`,
        falling back to ``() -> uint256`` for an unknown zero-argument
        name. Builds the calldata as
        ``abi.selector(abi.function_signature(fn, arg_types)) +
        abi.encode(arg_types, args)``, posts it through
        ``rpc.eth_call(address.lower(), data, rpc.block_tag(block))``, and
        decodes the result with ``abi.decode(return_types, ...)``. Returns
        the bare value when ``len(return_types) == 1``, else the tuple.

        Raises:
            ValidationError: on an ``address`` that is not a string, on an
                unknown name called with arguments, and on a known name
                called with the wrong number of arguments. All three are
                refused before any HTTP.
            SourceError: on a node error (a reverting call), an empty or
                short or over-long result, and any word that does not fit
                its declared type.
        """
        # `address` is CONSUMED here by .lower(), so it is refused here.
        # rpc.py's rule: whatever touches a caller's argument first is what
        # refuses it. A bare .lower() over None or an int is an
        # AttributeError, past the taxonomy an adapter's caller catches.
        if not isinstance(address, str):
            raise ValidationError(f"{fn} needs a string address: {address!r}")
        # Counted twice below, so it is refused before the first len().
        args = require_sequence(args, "args", ValidationError)
        arg_types, return_types = _resolve(fn, args)
        if len(args) != len(arg_types):
            # Checked against the REGISTRY, in both directions, and named.
            # abi.encode refuses a length mismatch too, but it has never
            # heard of `balanceOf`, so it can only report word counts; the
            # caller's mistake is the function it thought it was calling.
            raise ValidationError(
                f"{fn} takes {len(arg_types)} arguments and got {len(args)}"
            )
        calldata = selector(function_signature(fn, arg_types)) + encode(
            arg_types, args
        )
        result = self._rpc.eth_call(
            address.lower(),
            "0x" + calldata.hex(),
            # The pin is the CONSTRUCTOR's, because the protocol's `call`
            # carries no block parameter. A caller reading two blocks
            # builds two readers.
            block_tag(self._block_number),
        )
        values = _decoded(fn, return_types, result)
        # The length-1 unwrap, here and nowhere else: abi.decode always
        # returns a tuple, and every adapter call site coerces what it gets
        # straight away, so a forwarded (n,) becomes a TypeError inside the
        # adapter rather than a contained AuradefiError.
        if len(return_types) == 1:
            return values[0]
        return values
