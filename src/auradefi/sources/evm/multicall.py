"""Multicall3 ``aggregate3``, with per-call failure isolated (§4).

RELEASE_0.2.0 §4, verbatim: "``multicall.py`` targets Multicall3's
``aggregate3((address,bool,bytes)[])``, whose whole reason for existing
here is ``allowFailure``. One reverting call must not void the batch; it
must come back as a declared failure for that call alone."

That sentence is the whole module. Five reads go out as ONE
``eth_call``; if the fourth reverts, the other four still come back and
the fourth arrives as :class:`CallResult` with ``success`` False, its
returndata preserved. Never a zero, never ``None``, never an exception.

THE DECLARED-FAILURE CARRIER. Profile rule 8 says incomplete data is
DECLARED rather than defaulted, and names ``data_quality`` for it, but
no ``data_quality`` carrier is reachable from ``sources/``: it lives in
``decode/models.py`` and the layering gate forbids ``sources``
importing ``decode``. So the declaration lives on the failure channel
that exists here, exactly as §4 already words it: ``CallResult.success
is False`` with the returndata kept. This is the sibling of ``rpc.py``'s
``BatchResult(result, error)``, which does the same job for the
JSON-RPC batch path. Two carriers, one per transport, deliberately.

REQUEST ORDER, and why it is safe here and not there. An ``aggregate3``
return is a positional array: element *i* is the answer to call *i* by
construction, so the results come back in REQUEST order with no
matching step. ``rpc.batch`` matches its answers by JSON-RPC ``id``
because a node may reorder a batch array. The two must not be unified:
one is an ABI array inside a single call, the other is a protocol-level
batch of separate calls.

Layering: ``codec/abi.py`` builds and reads the calldata (it owns the
two named dynamic special cases and returns primitive ``(bool, bytes)``
pairs, so the wrapping is owned here and ``abi`` never imports this
module), ``rpc.py`` carries it. Only classes from ``auradefi.errors``
are raised: :class:`~auradefi.errors.ValidationError` for the caller
input this module refuses before any HTTP, a batch that is not a
sequence of :class:`Call` and a target that is not an address, and
:class:`~auradefi.errors.SourceError` for everything that came off the
wire, including a ValidationError raised over returned bytes, which is
re-raised ``from`` the original so the traceback keeps the reason.

``reader.py`` does NOT batch through this module: it issues one
``eth_call`` per read. No dependency runs between the two in either
direction.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from auradefi.errors import SourceError, ValidationError, require_int
from auradefi.sources.evm.codec.aggregate3 import (
    decode_aggregate3,
    encode_aggregate3,
)
from auradefi.sources.evm.rpc import EvmRpc, block_tag

__all__ = ["MULTICALL3_ADDRESS", "Call", "CallResult", "Multicall3"]

#: The canonical deterministic deployment, identical on every EVM chain
#: and lowercased per the house address rule.
MULTICALL3_ADDRESS = "0xca11bde05977b3631167028862be2a173976ca11"

#: The house address form, the same pattern ``codec/abi.py`` anchors on.
#: fullmatch, so a trailing newline cannot slip a 41st character past the
#: width check.
_ADDRESS = re.compile("0x[0-9a-fA-F]{40}")

#: A returned blob: the ``0x`` prefix and an EVEN number of hex digits.
#: The prefix is required and checked, never assumed. ``result[2:]`` over
#: an unprefixed but otherwise valid blob eats its first two hex digits
#: and shifts every word left, which decodes into a plausible wrong
#: answer instead of failing, so the shape is refused before the strip.
#: The even count is here because ``bytes.fromhex`` refuses an odd one
#: with a ValueError, which is outside the taxonomy this door promises.
_RESULT_HEX = re.compile("0x(?:[0-9a-fA-F]{2})*")


def _checked_address(value: object, member: str) -> str:
    """``value`` as a lowercased 0x address, or a refusal.

    The isinstance check is not decoration: a bare ``value.lower()``
    hands ``None`` and ``123`` back as an AttributeError, past the
    ValidationError this module promises, and half a taxonomy is worse
    than none. Lowercasing happens only after the format holds, so one
    address is one key everywhere downstream.
    """
    if not isinstance(value, str) or _ADDRESS.fullmatch(value) is None:
        raise ValidationError(f"malformed multicall {member} address: {value!r}")
    return value.lower()


@dataclass(frozen=True, slots=True)
class Call:
    """One member of an ``aggregate3`` batch.

    ``allow_failure`` defaults to True, which is the reason this module
    exists: a reverting call comes back as a declared
    :class:`CallResult` failure instead of voiding its four neighbours.
    Set it False only when a revert should void the whole batch, which
    the node reports as a JSON-RPC error for the entire ``eth_call``.
    """

    target: str
    data: bytes
    allow_failure: bool = True

    def __post_init__(self) -> None:
        """Validate ``target`` as 0x plus 40 hex, and lowercase it.

        The house pattern from
        ``positions/protocol.py::ContractDescriptor.__post_init__``,
        tightened with the format check: a target is refused here,
        before any HTTP, because a malformed address is caller input
        and never a source failure.

        Raises:
            ValidationError: on a target that is not a string or does
                not match ``^0x[0-9a-fA-F]{40}$``.
        """
        object.__setattr__(self, "target", _checked_address(self.target, "target"))


@dataclass(frozen=True, slots=True)
class CallResult:
    """One ``aggregate3`` answer: a value, or a DECLARED failure.

    ``success`` False is the declaration. ``data`` is whatever the call
    returned, kept byte for byte: empty for a bare revert, the revert
    payload when the contract sent one. No zero is ever substituted,
    which is the coercion rule 8 exists to stop.
    """

    success: bool
    data: bytes


def _check_calls(calls: object) -> None:
    """Refuse a batch that is not a sequence of :class:`Call`.

    The batch is CONSUMED here, counted and then unpacked field by
    field, so it is refused here, by the rule ``rpc.py`` states for its
    own ``requests``: whatever touches a caller's argument first is what
    refuses it. Left unchecked, ``aggregate3(None)`` escapes as a
    TypeError off ``len`` and a plain ``(target, flag, data)`` triple as
    an AttributeError off ``.target``, both of them past the two classes
    this module promises, and half a taxonomy is worse than none.

    ``str`` and ``bytes`` are sequences to Python and are never a batch,
    so both are excluded by name, exactly as ``rpc.py``'s
    ``_is_pair_like`` excludes them. A generator is refused by the same
    check, which is what the ``Sequence`` annotation already asks for: a
    one-shot iterable cannot be counted and then read a second time.

    Nothing is returned, because the batch is used as the caller gave
    it: this reads the argument and never rebuilds it.

    Raises:
        ValidationError: on a ``calls`` that is not a sequence, or is a
            str or bytes, and on any element that is not a
            :class:`Call`. Caller input, so ValidationError and not
            SourceError: no node has been asked anything yet.
    """
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)):
        raise ValidationError(f"aggregate3 needs a sequence of Call: {calls!r}")
    for number, call in enumerate(calls, start=1):
        if not isinstance(call, Call):
            raise ValidationError(
                f"aggregate3 call {number} must be a Call, got "
                f"{type(call).__name__}: {call!r}"
            )


class Multicall3:
    """Batched reads through one Multicall3 ``aggregate3`` call.

    The ``EvmRpc`` is injected and the constructor performs no I/O, so
    a cassette or a mock transport plugs in unchanged.
    """

    def __init__(self, rpc: EvmRpc, address: str = MULTICALL3_ADDRESS) -> None:
        """Bind the transport and the Multicall3 deployment. No I/O.

        ``address`` defaults to :data:`MULTICALL3_ADDRESS` and is
        lowercased, so a checksummed override still reaches the wire in
        the one casing the rest of the EVM source uses.

        Raises:
            ValidationError: on an ``address`` that is not a 0x-prefixed
                40-hex string. It is CONSUMED here, being lowercased and
                stored, so it is refused here: the rule ``rpc.py`` states
                for its own ``to``.
        """
        self._rpc = rpc
        self._address = _checked_address(address, "deployment")

    def aggregate3(
        self, calls: Sequence[Call], block_number: int | None = None
    ) -> tuple[CallResult, ...]:
        """Run ``calls`` as one ``aggregate3``; the answers, in REQUEST order.

        Exactly ONE ``eth_call`` is issued for the whole batch, to the
        bound Multicall3 address, at ``block_tag(block_number)``. An
        empty ``calls`` issues ZERO requests and returns ``()``.

        A call that reverts with ``allow_failure`` True is a declared
        :class:`CallResult` failure and does NOT raise; the other
        answers are unaffected.

        Raises:
            ValidationError: on a ``calls`` that is not a sequence of
                :class:`Call`, refused on entry and before any HTTP,
                since a batch the caller got wrong is caller input.
            SourceError: when the decoded result count differs from
                ``len(calls)`` (both counts are named), when the node
                answers the whole ``eth_call`` with a JSON-RPC error,
                which is what a revert under ``allow_failure`` False
                produces, when the result is not a 0x-prefixed hex
                string, and when the returned bytes do not decode, in
                which case the abi ValidationError is the ``__cause__``.
        """
        # Checked before it is counted: len() over a None or a generator
        # is a TypeError, which is outside the taxonomy this door
        # promises, and "" would otherwise read as an empty batch.
        _check_calls(calls)
        # `block_number` is forwarded untouched to block_tag, whose hex()
        # is the first thing to read it, so the refusal belongs here.
        if block_number is not None:
            require_int(block_number, "block_number", ValidationError)
        # Nothing to ask, so nothing is asked. A refresh whose batch came
        # out empty must cost no node call, and an aggregate3 of zero
        # elements is a request with no answer worth paying for.
        if len(calls) == 0:
            return ()
        # encode_aggregate3 INCLUDES the 82ad56cb selector, so no selector
        # is prepended here: doing so would make the 260-byte one-call
        # vector 264 and send the array to a function that does not exist.
        calldata = encode_aggregate3(
            [(call.target, call.allow_failure, call.data) for call in calls]
        )
        # ONE eth_call for the whole batch. block_tag maps None to
        # "latest" and every int, zero included, to its minimal hex.
        result = self._rpc.eth_call(
            self._address, "0x" + calldata.hex(), block_tag(block_number)
        )
        return self._results(result, len(calls))

    @staticmethod
    def _results(result: object, expected: int) -> tuple[CallResult, ...]:
        """The node's blob as ``expected`` carriers, in REQUEST order.

        The returned array is positional by construction, element *i*
        answering call *i*, so the order it arrives in IS the order it is
        returned in and nothing is matched. ``rpc.batch`` matches by
        JSON-RPC id because that is a protocol-level batch a node may
        reorder; the two disciplines must not be unified.

        Raises:
            SourceError: on a result that is not a 0x-prefixed hex string
                of whole bytes, on bytes the codec refuses (chained from
                its ValidationError, since those bytes came off the wire),
                and on a decoded count that differs from ``expected``.
        """
        if not isinstance(result, str) or _RESULT_HEX.fullmatch(result) is None:
            raise SourceError(f"aggregate3 result is not 0x hex: {result!r}")
        try:
            decoded = decode_aggregate3(bytes.fromhex(result[2:]))
        except ValidationError as exc:
            # SourceError at this door because the bytes are the node's,
            # not the caller's; `from` so the codec's reason survives in
            # the traceback instead of being flattened to one sentence.
            raise SourceError(f"aggregate3 returndata did not decode: {exc}") from exc
        if len(decoded) != expected:
            # An inequality, never "at least enough": zipping a short
            # array against the first calls would silently answer call 4
            # with call 5's word, and a long one hides a decode error.
            raise SourceError(
                f"aggregate3 asked for {expected} calls and was answered "
                f"with {len(decoded)} results"
            )
        # Every carrier is built from the pair the wire carried. No zero
        # and no empty bytes is substituted for a failed call: an empty
        # returndata and a returned zero word stay distinguishable, which
        # is the whole of the declared-failure channel.
        return tuple(CallResult(success, payload) for success, payload in decoded)
