"""Pure UTXO models for the Bitcoin source (SPEC §3.2, §10 Bitcoin row).

PURE by contract: dataclasses, typing, ``auradefi.errors``,
``auradefi.money.quantity`` and ``auradefi.chains.bitcoin`` only — no
httpx, no I/O. All amounts are integer satoshis; the only Quantity
minted here is 8-decimal BTC (DECISIONS "Gap-limit scan": BTC decimals
= 8, caip19 = ``bip122:000000000019d6689c085ae165831e93/slip44:0``).

Validation follows house style: ``bool`` is rejected BEFORE the int
check on every integer field — ``True`` is never a sat count, a chain
number, or an index. All failures raise ``ValidationError``
(auradefi.errors).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from auradefi.chains import bitcoin
from auradefi.errors import ValidationError
from auradefi.money.quantity import Quantity

SATS_DECIMALS = 8
BTC_CAIP19 = f"{bitcoin.MAINNET}/slip44:{bitcoin.SLIP44}"


def _require_text(value: object, name: str) -> None:
    """A non-empty ``str`` or ``ValidationError``."""
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a str, got {type(value).__name__}")
    if not value:
        raise ValidationError(f"{name} must not be empty")


def _require_unsigned(value: object, name: str) -> None:
    """A non-bool ``int >= 0`` or ``ValidationError``.

    ``bool`` is rejected FIRST: it is an ``int`` subclass, so ``True``
    would otherwise pass every numeric check as 1.
    """
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be an int, got bool")
    if not isinstance(value, int):
        raise ValidationError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ValidationError(f"{name} must be >= 0, got {value}")


@dataclass(frozen=True, slots=True)
class Utxo:
    """One unspent transaction output as Esplora reports it.

    ``ValidationError`` unless: ``txid`` is a non-empty str, ``vout >= 0``,
    ``value_sats >= 0`` (bool rejected before int on both ints), and
    ``confirmed`` is exactly a bool.
    """

    txid: str
    vout: int
    value_sats: int
    confirmed: bool

    def __post_init__(self) -> None:
        """Validate fields; raise ``ValidationError`` on any violation."""
        _require_text(self.txid, "txid")
        _require_unsigned(self.vout, "vout")
        _require_unsigned(self.value_sats, "value_sats")
        if not isinstance(self.confirmed, bool):
            raise ValidationError(
                f"confirmed must be a bool, got {type(self.confirmed).__name__}"
            )


@dataclass(frozen=True, slots=True)
class AddressStats:
    """Esplora ``chain_stats`` for one address (mempool_stats is ignored).

    ``ValidationError`` unless all three fields are non-bool ints ``>= 0``
    and ``spent_txo_sum <= funded_txo_sum``.
    """

    funded_txo_sum: int
    spent_txo_sum: int
    tx_count: int

    def __post_init__(self) -> None:
        """Validate fields; raise ``ValidationError`` on any violation."""
        _require_unsigned(self.funded_txo_sum, "funded_txo_sum")
        _require_unsigned(self.spent_txo_sum, "spent_txo_sum")
        _require_unsigned(self.tx_count, "tx_count")
        if self.spent_txo_sum > self.funded_txo_sum:
            raise ValidationError(
                f"spent_txo_sum {self.spent_txo_sum} exceeds funded_txo_sum "
                f"{self.funded_txo_sum}"
            )

    @property
    def confirmed_sats(self) -> int:
        """``funded_txo_sum - spent_txo_sum`` — never negative (validated)."""
        return self.funded_txo_sum - self.spent_txo_sum


@dataclass(frozen=True, slots=True)
class AddressBalance:
    """One used derived address with its confirmed balance.

    ``ValidationError`` unless: ``address`` is a non-empty str, ``chain``
    is 0 (external) or 1 (change), ``index >= 0``, ``balance_sats >= 0``,
    ``tx_count >= 0`` — bool rejected before int on every int field.
    """

    address: str
    chain: int
    index: int
    balance_sats: int
    tx_count: int

    def __post_init__(self) -> None:
        """Validate fields; raise ``ValidationError`` on any violation."""
        _require_text(self.address, "address")
        _require_unsigned(self.chain, "chain")
        if self.chain > 1:
            raise ValidationError(
                f"chain must be 0 (external) or 1 (change), got {self.chain}"
            )
        _require_unsigned(self.index, "index")
        _require_unsigned(self.balance_sats, "balance_sats")
        _require_unsigned(self.tx_count, "tx_count")


@dataclass(frozen=True, slots=True)
class BitcoinScanResult:
    """The full derived-address balance set from one gap-limit scan.

    ``addresses`` is ordered chain 0 then 1, index ascending — used
    addresses only, balance-0 swept ones included (DECISIONS).
    """

    addresses: tuple[AddressBalance, ...]

    @property
    def total_sats(self) -> int:
        """Exact int sum of ``balance_sats`` over ``addresses``."""
        return sum(balance.balance_sats for balance in self.addresses)

    @property
    def total(self) -> Quantity:
        """``Quantity(total_sats, SATS_DECIMALS)`` — exact 8-decimal BTC."""
        return Quantity(self.total_sats, SATS_DECIMALS)

    @property
    def caip19(self) -> str:
        """``BTC_CAIP19`` — the mainnet native-asset CAIP-19."""
        return BTC_CAIP19


def confirmed_sats(utxos: Iterable[Utxo]) -> int:
    """Exact int sum of ``value_sats`` over the CONFIRMED utxos only."""
    return sum(row.value_sats for row in utxos if row.confirmed)


def total_sats(utxos: Iterable[Utxo]) -> int:
    """Exact int sum of ``value_sats`` over ALL utxos, confirmed or not."""
    return sum(row.value_sats for row in utxos)
