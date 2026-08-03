"""Etherscan V2 txlist/tokentx typed raw records — parse only (SPEC §3.3).

The sources half of "raw chain bytes -> typed records" for Etherscan V2
account rows. Stdlib only: NO httpx here (fetching is the separate
txlist-fetch order), NO imports of decode/ — sources may import only
money/, chains/, assets/ (SPEC §3.3 layer contract).

Parse rules, shared by both parsers:

* Every field is read from a STRING. Numeric fields (``blockNumber``,
  ``timeStamp``, ``value``, ``gasUsed``, ``gasPrice``, ``tokenDecimal``)
  are unsigned base-10 digit strings converted via ``int``; a non-str or
  non-digit value raises — never trust JSON numbers (SPEC rule #2).
* ``timeStamp`` stays in SECONDS exactly as delivered; the decoder
  converts to ms epoch (DECISIONS: Etherscan ``timeStamp`` × 1000).
* Hex addresses and transaction hashes are lowercased on parse
  (DECISIONS pinned canonicalization). ``to`` may be ``""`` (contract
  creation) and is kept as ``""``.
* ``isError`` must be exactly ``"0"`` or ``"1"`` -> ``False``/``True``.
* A missing key, non-string field, or malformed number raises
  ``auradefi.errors.SourceError`` whose message names the offending key.
* The input dict is never mutated; unknown extra keys are ignored.

Total functions, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from auradefi.errors import SourceError


@dataclass(frozen=True, slots=True)
class NormalTxRecord:
    """One typed ``module=account&action=txlist`` row.

    ``time_stamp`` is SECONDS as delivered by Etherscan; ``to_address``
    is ``""`` for contract creations; ``tx_hash``, ``from_address`` and
    ``to_address`` are lowercased.
    """

    tx_hash: str
    block_number: int
    time_stamp: int
    from_address: str
    to_address: str
    value_wei: int
    gas_used: int
    gas_price_wei: int
    is_error: bool


@dataclass(frozen=True, slots=True)
class TokenTxRecord:
    """One typed ``module=account&action=tokentx`` row.

    ``value_raw`` is the transfer amount in base units;
    ``token_decimal`` from the row's ``tokenDecimal`` string;
    ``contract_address`` lowercased. ``time_stamp`` is SECONDS.
    """

    tx_hash: str
    block_number: int
    time_stamp: int
    from_address: str
    to_address: str
    contract_address: str
    value_raw: int
    token_decimal: int
    token_symbol: str
    gas_used: int
    gas_price_wei: int


def _str_field(row: dict, key: str) -> str:
    """The value at ``key``, required to exist and be a ``str``."""
    if key not in row:
        raise SourceError(f"row is missing key '{key}'")
    value = row[key]
    if type(value) is not str:
        raise SourceError(f"key '{key}' must be a string, got {type(value).__name__}")
    return value


def _hex_field(row: dict, key: str) -> str:
    """A string field lowercased on parse (pinned hex canonicalization)."""
    return _str_field(row, key).lower()


def _uint_field(row: dict, key: str) -> int:
    """An unsigned base-10 digit string converted to ``int``.

    Only ASCII digits pass — no sign, whitespace, underscores, dots,
    exponents, hex, or unicode digits; JSON numbers are rejected as
    non-strings (SPEC rule #2: never trust JSON numbers).
    """
    value = _str_field(row, key)
    if not (value.isascii() and value.isdigit()):
        raise SourceError(f"key '{key}' is not an unsigned base-10 integer: {value!r}")
    return int(value)


def _is_error_field(row: dict, key: str) -> bool:
    """Exactly ``"0"`` -> ``False`` or ``"1"`` -> ``True``; anything else raises."""
    value = _str_field(row, key)
    if value == "0":
        return False
    if value == "1":
        return True
    raise SourceError(f"key '{key}' must be exactly '0' or '1', got {value!r}")


def parse_normal_row(row: dict) -> NormalTxRecord:
    """Parse one txlist row over keys {hash, blockNumber, timeStamp,
    from, to, value, gasUsed, gasPrice, isError}.

    Raises ``SourceError`` naming the offending key on a missing key,
    a non-string field, a malformed number, or an ``isError`` that is
    not exactly ``"0"``/``"1"``. Never mutates ``row``; ignores unknown
    extra keys.
    """
    return NormalTxRecord(
        tx_hash=_hex_field(row, "hash"),
        block_number=_uint_field(row, "blockNumber"),
        time_stamp=_uint_field(row, "timeStamp"),
        from_address=_hex_field(row, "from"),
        to_address=_hex_field(row, "to"),
        value_wei=_uint_field(row, "value"),
        gas_used=_uint_field(row, "gasUsed"),
        gas_price_wei=_uint_field(row, "gasPrice"),
        is_error=_is_error_field(row, "isError"),
    )


def parse_tokentx_row(row: dict) -> TokenTxRecord:
    """Parse one tokentx row over keys {hash, blockNumber, timeStamp,
    from, to, contractAddress, value, tokenDecimal, tokenSymbol,
    gasUsed, gasPrice}.

    Raises ``SourceError`` naming the offending key on a missing key,
    a non-string field, or a malformed number. Never mutates ``row``;
    ignores unknown extra keys.
    """
    return TokenTxRecord(
        tx_hash=_hex_field(row, "hash"),
        block_number=_uint_field(row, "blockNumber"),
        time_stamp=_uint_field(row, "timeStamp"),
        from_address=_hex_field(row, "from"),
        to_address=_hex_field(row, "to"),
        contract_address=_hex_field(row, "contractAddress"),
        value_raw=_uint_field(row, "value"),
        token_decimal=_uint_field(row, "tokenDecimal"),
        token_symbol=_str_field(row, "tokenSymbol"),
        gas_used=_uint_field(row, "gasUsed"),
        gas_price_wei=_uint_field(row, "gasPrice"),
    )
