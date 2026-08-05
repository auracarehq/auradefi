"""Rich decoded transactions (SPEC §4.4; rules #1 #2 #4 #7).

A transaction is a bag of signed movements: every movement is a ``Part``,
fees are ``Fee`` siblings that can never corrupt the trade legs, and
``acts[]`` gives sub-operation linkage via ``act_id`` back-references.
``Transaction.type`` is DERIVED from the shape of ``parts[]``: a computed
property, never a stored field.

Layering: stdlib + ``auradefi.money`` only. This module must NEVER import
``auradefi.ledger``. ``Direction`` and ``transaction_id`` below are
deliberate value-identical duplicates of ``auradefi.ledger.models``
(DECISIONS.md "Duplication waiver"); ``ledger/bridge.py`` maps by value and
golden vectors in ``tests/ledger/test_bridge.py`` pin both to the same
bytes.

All timestamps are ms-epoch ints; amounts are exact ``Quantity`` values,
never floats.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from auradefi.errors import ValidationError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity


class TxStatus(StrEnum):
    """Real, never permanently null (SPEC §4.4, Vezgo's six null fields)."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    REVERTED = "reverted"
    REPLACED = "replaced"
    DROPPED = "dropped"


class TxType(StrEnum):
    """Derived label over the shape of ``parts[]``. See :func:`derive_tx_type`."""

    SEND = "send"
    RECEIVE = "receive"
    TRADE = "trade"
    SELF = "self"
    INTERACTION = "interaction"


class TxSubtype(StrEnum):
    """Phase-3 subset of the SPEC §4.4 subtype vocabulary."""

    TRANSFER = "transfer"
    SWAP = "swap"
    APPROVE = "approve"
    FEE = "fee"
    UNKNOWN = "unknown"


class MetaType(StrEnum):
    """Position meta-type on an underlying movement (SPEC §4.3, verbatim)."""

    WALLET = "wallet"
    SUPPLIED = "supplied"
    BORROWED = "borrowed"
    CLAIMABLE = "claimable"
    VESTING = "vesting"
    LOCKED = "locked"
    NFT = "nft"


class BorneBy(StrEnum):
    """Who pays a fee. ``Counterparty`` keeps inbound-transfer gas visible
    without letting naive summation over-count (Vezgo's inversion)."""

    SELF = "self"
    COUNTERPARTY = "counterparty"


class Direction(StrEnum):
    """Which way a movement goes relative to the owning account.

    Deliberate value-identical duplicate of
    ``auradefi.ledger.models.Direction``: the layer contract forbids
    decode→ledger imports (DECISIONS.md "Duplication waiver"). The bridge
    maps by value; drift is a red golden-vector test, not a debate.
    """

    IN = "in"
    OUT = "out"
    SELF = "self"


@dataclass(frozen=True, slots=True)
class Part:
    """One movement of a single asset inside a transaction (rule #4).

    ``asset_id`` is a canonical CAIP-19 string (native =
    ``Chain.native_caip19``; ERC-20 = ``f"{caip2}/erc20:{contract_lower}"``),
    never an ``ast_`` registry id. ``act_id`` is a back-reference into the
    owning transaction's ``acts[]`` or ``None``.
    """

    act_id: str | None
    direction: Direction
    asset_id: str
    quantity: Quantity
    value: Money | None
    price: Money | None
    from_address: str
    to_address: str
    meta_type: MetaType | None = None
    other_parties: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Fee:
    """A fee: a sibling of ``parts[]``, NEVER a movement (SPEC §4.4).

    Fees never appear in ``parts[]`` and never become ledger entries.
    ``borne_by`` is ``counterparty`` on inbound transfers so summation
    skips the fee while keeping it visible.
    """

    asset_id: str
    quantity: Quantity
    value: Money | None
    act_id: str | None
    borne_by: BorneBy


@dataclass(frozen=True, slots=True)
class Act:
    """One sub-operation of a transaction; ``act_id`` = :func:`act_id_for`."""

    act_id: str
    subtype: TxSubtype
    protocol: str | None = None


@dataclass(frozen=True, slots=True)
class DataQuality:
    """First-class decode-quality metadata (SPEC §4.4).

    ``decoder_version`` must equal the owning transaction's
    ``decoder_version``. A mismatch is a ``ValidationError``.
    """

    incomplete: tuple[str, ...]
    confidence: float
    decoder_version: int
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Transaction:
    """A rich decoded transaction (SPEC §4.4).

    ``account_id`` is a deliberate addition to §4.4's sketch: the pinned
    :func:`transaction_id` hashes over it. ``initiated_at`` /
    ``confirmed_at`` are ms-epoch ints. ``type`` is a property computed by
    :func:`derive_tx_type` over ``parts``, never a stored field.

    ``__post_init__`` raises ``auradefi.errors.ValidationError`` when
    (a) any ``part.act_id`` or ``fee.act_id`` is non-``None`` and not among
    ``{a.act_id for a in acts}``, or (b) ``data_quality.decoder_version``
    differs from ``decoder_version`` (rule #7).
    """

    id: str
    chain_id: str
    tx_hash: str
    account_id: str
    status: TxStatus
    block_number: int | None
    initiated_at: int
    confirmed_at: int | None
    subtype: TxSubtype
    parts: tuple[Part, ...]
    fees: tuple[Fee, ...]
    acts: tuple[Act, ...]
    protocol: str | None
    decoder_version: int
    data_quality: DataQuality

    def __post_init__(self) -> None:
        """Validate act_id back-references and decoder-version agreement.

        Raises ``auradefi.errors.ValidationError`` on a dangling
        ``part.act_id``/``fee.act_id`` or when
        ``data_quality.decoder_version != decoder_version``.
        """
        known = {act.act_id for act in self.acts}
        for referrer in (*self.parts, *self.fees):
            if referrer.act_id is not None and referrer.act_id not in known:
                raise ValidationError(
                    f"act_id {referrer.act_id!r} is not in acts[] "
                    f"of transaction {self.id!r}"
                )
        if self.data_quality.decoder_version != self.decoder_version:
            raise ValidationError(
                f"data_quality.decoder_version "
                f"{self.data_quality.decoder_version} != transaction "
                f"decoder_version {self.decoder_version}"
            )

    @property
    def type(self) -> TxType:
        """DERIVED from parts, never stored: ``derive_tx_type(self.parts)``."""
        return derive_tx_type(self.parts)


def derive_tx_type(parts: tuple[Part, ...]) -> TxType:
    """Derive the top-level type from part directions (DECISIONS pinned).

    Over ``parts[]`` only. Fees are siblings, structurally excluded.
    Empty → ``INTERACTION``; all ``in`` → ``RECEIVE``; all ``out`` →
    ``SEND``; all ``self`` → ``SELF``; any mixture → ``TRADE``.
    """
    directions = {part.direction for part in parts}
    if not directions:
        return TxType.INTERACTION
    if directions == {Direction.IN}:
        return TxType.RECEIVE
    if directions == {Direction.OUT}:
        return TxType.SEND
    if directions == {Direction.SELF}:
        return TxType.SELF
    return TxType.TRADE


def transaction_id(chain_id: str, tx_hash: str, account_id: str) -> str:
    """Deterministic transaction id (DECISIONS pinned; SPEC §4.4).

    ``"txn_" + sha256(f"{chain_id}|{tx_hash}|{account_id}".encode())
    .hexdigest()[:16]``: a deliberate byte-identical duplicate of
    ``auradefi.ledger.models.transaction_id`` (DECISIONS.md "Duplication
    waiver"), drift-proofed by golden vectors in
    ``tests/ledger/test_bridge.py``.
    """
    digest = hashlib.sha256(
        f"{chain_id}|{tx_hash}|{account_id}".encode()
    ).hexdigest()
    return f"txn_{digest[:16]}"


def act_id_for(index: int) -> str:
    """``f"act_{index}"``: zero-based position in ``acts[]`` (DECISIONS pinned)."""
    return f"act_{index}"
