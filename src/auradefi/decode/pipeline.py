"""Decode pipeline: typed explorer records -> rich Transactions (SPEC §4.5).

Phase-3 scope: ERC-20 + native movements only (rules #4, #7). This module
turns ``sources.evm.txlist`` records for ONE account on ONE chain into
``decode.models.Transaction`` values with ``parts[]``, ``fees[]`` and
``acts[]``.

Layering (tests/style/test_layering.py): imports ``decode.models``,
``sources.evm.txlist``, ``chains.registry`` and ``money`` only, NEVER
``ledger`` (DECISIONS.md "Duplication waiver"), never httpx (no I/O here;
fetching is the sources layer's job).

All timestamps are ms-epoch ints; amounts are exact ``Quantity`` values,
never floats.
"""

from __future__ import annotations

from collections.abc import Sequence

from auradefi.chains.registry import Chain, ChainRegistry
from auradefi.decode.models import (
    Act,
    BorneBy,
    DataQuality,
    Direction,
    Fee,
    Part,
    Transaction,
    TxStatus,
    TxSubtype,
    transaction_id,
)
from auradefi.errors import DecodeError
from auradefi.money.quantity import Quantity
from auradefi.sources.evm.txlist import NormalTxRecord, TokenTxRecord

#: Pinned decoder version (DECISIONS.md "decoder_version"; SPEC rule #7).
#: Stamped identically on BOTH ``Transaction.decoder_version`` and
#: ``DataQuality.decoder_version``; bump whenever identical input would
#: decode differently.
DECODER_VERSION: int = 1

#: Phase 3 emits exactly one act per transaction (act_id_for(0)).
_ACT_ID = "act_0"

#: Enrichment is deferred: fiat value is the only missing facet, decoded
#: purely from Etherscan rows (SPEC §4.5 Phase-3 scope).
_DATA_QUALITY = DataQuality(("fiat_value",), 1.0, DECODER_VERSION, ("etherscan",))


def decode_account(
    chain_id: str,
    account_id: str,
    address: str,
    normal: Sequence[NormalTxRecord],
    tokens: Sequence[TokenTxRecord],
    registry: ChainRegistry | None = None,
) -> tuple[Transaction, ...]:
    """Decode one account's explorer records into rich Transactions.

    ``chain_id`` is CAIP-2; ``address`` is lowercased on entry;
    ``registry`` defaults to a fresh pre-seeded ``ChainRegistry()``. An
    unregistered ``chain_id`` propagates
    ``auradefi.errors.UnknownChainError``.

    Grouping and identity: all records are grouped by ``tx_hash``;
    exactly ONE ``Transaction`` is emitted per hash, sorted ascending by
    ``(block_number, tx_hash)``; ``id`` is
    ``decode.models.transaction_id(chain_id, tx_hash, account_id)``.

    Validation: ``auradefi.errors.DecodeError`` BEFORE any output when:
    rows of one hash disagree on ``block_number`` or ``time_stamp``, or
    any record has neither ``from`` nor ``to`` equal to the account
    address.

    Parts (rule #4): a native part iff the hash's ``NormalTxRecord`` has
    ``value_wei > 0`` and ``is_error`` False, with ``asset_id =
    registry.get(chain_id).native_caip19`` and ``Quantity(value_wei,
    native_decimals)``; then one part per ``TokenTxRecord`` in input
    order, with ``asset_id = f"{chain_id}/erc20:{contract_address}"``
    (already-lowercase contract = canonical CAIP-19) and
    ``Quantity(value_raw, token_decimal)``. Direction relative to the
    account: ``from == addr and to == addr`` -> SELF; ``from == addr`` ->
    OUT; ``to == addr`` -> IN.

    Status: ``is_error`` True -> FAILED with ZERO parts (tokentx rows
    sharing a failed hash are dropped too; the fee survives); otherwise
    CONFIRMED. PENDING/REVERTED/REPLACED/DROPPED are never emitted in
    Phase 3.

    Fee (sibling, never a movement, DECISIONS.md "Gas fee"): the gas
    row is the hash's ``NormalTxRecord`` if present, else its FIRST
    ``TokenTxRecord``; exactly one ``Fee(asset_id=native caip19,
    quantity=Quantity(gas_used * gas_price_wei, native_decimals),
    value=None, act_id="act_0", borne_by=SELF iff the gas row's ``from``
    == address else COUNTERPARTY)``, always emitted, failed
    transactions included.

    Acts: exactly one ``Act`` per transaction with ``act_id="act_0"``
    and ``protocol=None``; ``act.subtype == Transaction.subtype``: SWAP
    iff parts contain both IN and OUT; TRANSFER iff parts are non-empty
    otherwise; UNKNOWN iff parts are empty. Every part carries
    ``act_id="act_0"``.

    Timestamps: Etherscan seconds x 1000 -> ms epoch; ``initiated_at ==
    confirmed_at`` for mined rows.

    Enrichment is deferred: every ``value``/``price`` is None,
    ``meta_type`` None, ``other_parties`` ``()``; ``data_quality ==
    DataQuality(("fiat_value",), 1.0, DECODER_VERSION, ("etherscan",))``
    and ``decoder_version == DECODER_VERSION`` on every output.
    """
    chain = (registry or ChainRegistry()).get(chain_id)
    address = address.lower()
    groups = _group_by_hash(normal, tokens)
    _validate(groups, address)
    ordered = sorted(
        groups.items(), key=lambda item: (_any_record(item[1]).block_number, item[0])
    )
    return tuple(
        _decode_one(chain_id, account_id, address, chain, tx_hash, rows)
        for tx_hash, rows in ordered
    )


def _group_by_hash(
    normal: Sequence[NormalTxRecord],
    tokens: Sequence[TokenTxRecord],
) -> dict[str, tuple[list[NormalTxRecord], list[TokenTxRecord]]]:
    """All records bucketed by ``tx_hash``, token rows in input order."""
    groups: dict[str, tuple[list[NormalTxRecord], list[TokenTxRecord]]] = {}
    for record in normal:
        groups.setdefault(record.tx_hash, ([], []))[0].append(record)
    for record in tokens:
        groups.setdefault(record.tx_hash, ([], []))[1].append(record)
    return groups


def _any_record(
    rows: tuple[list[NormalTxRecord], list[TokenTxRecord]],
) -> NormalTxRecord | TokenTxRecord:
    """The hash's NormalTxRecord if present, else its FIRST TokenTxRecord.

    Doubles as the gas row (DECISIONS.md "Gas fee") and as the block /
    timestamp witness once :func:`_validate` has proven agreement.
    """
    normal_rows, token_rows = rows
    return normal_rows[0] if normal_rows else token_rows[0]


def _validate(
    groups: dict[str, tuple[list[NormalTxRecord], list[TokenTxRecord]]],
    address: str,
) -> None:
    """Raise ``DecodeError`` before ANY output on inconsistent input.

    A hash's rows must agree on ``block_number`` and ``time_stamp``, and
    every record must touch the account on at least one side.
    """
    for tx_hash, (normal_rows, token_rows) in groups.items():
        rows: list[NormalTxRecord | TokenTxRecord] = [*normal_rows, *token_rows]
        if len({row.block_number for row in rows}) > 1:
            raise DecodeError(f"rows of {tx_hash} disagree on block_number")
        if len({row.time_stamp for row in rows}) > 1:
            raise DecodeError(f"rows of {tx_hash} disagree on time_stamp")
        for row in rows:
            if address not in (row.from_address, row.to_address):
                raise DecodeError(
                    f"record {tx_hash} touches neither side of account {address}"
                )


def _direction(row: NormalTxRecord | TokenTxRecord, address: str) -> Direction:
    """Movement direction relative to the account (validated to touch it)."""
    if row.from_address == address:
        return Direction.SELF if row.to_address == address else Direction.OUT
    return Direction.IN


def _decode_parts(
    chain_id: str,
    address: str,
    chain: Chain,
    normal_rows: list[NormalTxRecord],
    token_rows: list[TokenTxRecord],
) -> tuple[Part, ...]:
    """Movements per rule #4: native (iff value > 0) then tokens in order.

    A failed hash decodes to ZERO parts. Its tokentx rows are dropped
    too; the caller keeps the fee alive.
    """
    if normal_rows and normal_rows[0].is_error:
        return ()
    parts: list[Part] = []
    if normal_rows and normal_rows[0].value_wei > 0:
        row = normal_rows[0]
        parts.append(
            Part(
                act_id=_ACT_ID,
                direction=_direction(row, address),
                asset_id=chain.native_caip19,
                quantity=Quantity(row.value_wei, chain.native_decimals),
                value=None,
                price=None,
                from_address=row.from_address,
                to_address=row.to_address,
            )
        )
    for row in token_rows:
        parts.append(
            Part(
                act_id=_ACT_ID,
                direction=_direction(row, address),
                asset_id=f"{chain_id}/erc20:{row.contract_address}",
                quantity=Quantity(row.value_raw, row.token_decimal),
                value=None,
                price=None,
                from_address=row.from_address,
                to_address=row.to_address,
            )
        )
    return tuple(parts)


def _subtype(parts: tuple[Part, ...]) -> TxSubtype:
    """SWAP iff both IN and OUT; TRANSFER iff non-empty otherwise; UNKNOWN."""
    directions = {part.direction for part in parts}
    if Direction.IN in directions and Direction.OUT in directions:
        return TxSubtype.SWAP
    if parts:
        return TxSubtype.TRANSFER
    return TxSubtype.UNKNOWN


def _decode_one(
    chain_id: str,
    account_id: str,
    address: str,
    chain: Chain,
    tx_hash: str,
    rows: tuple[list[NormalTxRecord], list[TokenTxRecord]],
) -> Transaction:
    """One validated hash group -> exactly one Transaction."""
    normal_rows, token_rows = rows
    gas_row = _any_record(rows)
    failed = bool(normal_rows) and normal_rows[0].is_error
    parts = _decode_parts(chain_id, address, chain, normal_rows, token_rows)
    subtype = _subtype(parts)
    fee = Fee(
        asset_id=chain.native_caip19,
        quantity=Quantity(
            gas_row.gas_used * gas_row.gas_price_wei, chain.native_decimals
        ),
        value=None,
        act_id=_ACT_ID,
        borne_by=(
            BorneBy.SELF if gas_row.from_address == address else BorneBy.COUNTERPARTY
        ),
    )
    timestamp_ms = gas_row.time_stamp * 1000
    return Transaction(
        id=transaction_id(chain_id, tx_hash, account_id),
        chain_id=chain_id,
        tx_hash=tx_hash,
        account_id=account_id,
        status=TxStatus.FAILED if failed else TxStatus.CONFIRMED,
        block_number=gas_row.block_number,
        initiated_at=timestamp_ms,
        confirmed_at=timestamp_ms,
        subtype=subtype,
        parts=parts,
        fees=(fee,),
        acts=(Act(_ACT_ID, subtype, None),),
        protocol=None,
        decoder_version=DECODER_VERSION,
        data_quality=_DATA_QUALITY,
    )
