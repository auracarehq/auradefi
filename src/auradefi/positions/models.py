"""Position data model (SPEC §4.3; DECISIONS.md pinned id algorithms).

Zapper's ontology, kept: an ``APP_TOKEN`` position IS a fungible token
(aToken, LP share, ERC-4626 share) and composes; a ``CONTRACT_POSITION``
is a non-tokenised leaf, valued only by summing its underlying balances.
``MetaType`` on each underlying yields debt sign for free — a
``BORROWED`` underlying carries a negative ``value`` (unit price stays
positive) and ``Position.value`` is the exact signed ``Money`` sum.

Zerion's two orthogonal axes classify every position:
``position_type`` (what state the asset is in) × ``protocol_module``
(where in the protocol). Zerion's two defects are fixed here: values are
signed, and group totals are COMPUTED by :func:`make_group`, never
caller-supplied (SPEC §4.3 defect #2).

Layering: stdlib + ``auradefi.money`` + ``auradefi.errors`` only. This
module must NEVER import ``auradefi.decode`` — ``MetaType`` below is a
deliberate value-identical duplicate of ``decode.models.MetaType``
(DECISIONS.md "Duplication waiver"; the layer contract forbids
positions→decode). Both test trees pin the same seven (name, value)
literals as hardcoded golden vectors, so drift is a red test.

Pinned wire contracts (DECISIONS.md — breaking to change):

    position_id = "pos_" + sha256(
        f"{adapter_id}|{chain_id}|{contract_lower}|{discriminator}"
    ).hexdigest()[:16]
    group_id    = "grp_" + sha256(
        f"{adapter_id}|{chain_id}|{group_key}"
    ).hexdigest()[:16]

0x addresses are lowercased. All timestamps are ms-epoch ints; amounts
are ``Quantity``/``Decimal``, never floats.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256

from auradefi.errors import ValidationError
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity


def _lower_0x(value: str) -> str:
    """Lowercase a 0x-prefixed address; leave others untouched."""
    return value.lower() if value.startswith("0x") else value


class PositionKind(StrEnum):
    """Zapper's asymmetry: tokenised (priceable, composes) vs leaf."""

    APP_TOKEN = "app_token"
    CONTRACT_POSITION = "contract_position"


class PositionType(StrEnum):
    """Zerion axis 1 — what state the asset is in (SPEC §4.3)."""

    WALLET = "wallet"
    DEPOSIT = "deposit"
    LOAN = "loan"
    LOCKED = "locked"
    STAKED = "staked"
    REWARD = "reward"
    INVESTMENT = "investment"


class ProtocolModule(StrEnum):
    """Zerion axis 2 — where in the protocol (SPEC §4.3)."""

    LENDING = "lending"
    LIQUIDITY_POOL = "liquidity_pool"
    YIELD = "yield"
    FARMING = "farming"
    STAKED = "staked"
    LEVERAGED_FARMING = "leveraged_farming"
    VESTING = "vesting"
    REWARDS = "rewards"
    LOCKED = "locked"
    NFT_STAKED = "nft_staked"
    DEPOSIT = "deposit"
    INVESTMENT = "investment"


class MetaType(StrEnum):
    """Meta-type on an underlying token (SPEC §4.3, verbatim).

    Deliberate value-identical duplicate of ``decode.models.MetaType`` —
    the layer contract forbids positions→decode imports (DECISIONS.md
    "Duplication waiver"). ``BORROWED`` flips value negative.
    """

    WALLET = "wallet"
    SUPPLIED = "supplied"
    BORROWED = "borrowed"
    CLAIMABLE = "claimable"
    VESTING = "vesting"
    LOCKED = "locked"
    NFT = "nft"


@dataclass(frozen=True, slots=True)
class Apy:
    """Explicitly typed yield — APR vs APY, gross vs net, source,
    staleness — not Zapper's untyped ``apy: number`` (SPEC §4.3).

    ``period`` must be ``'apr'`` or ``'apy'``; anything else raises
    ``ValidationError``. ``as_of_ms`` is a ms-epoch int.
    """

    rate: Decimal
    period: str
    gross: bool
    source: str
    as_of_ms: int

    def __post_init__(self) -> None:
        """Validate ``period`` ∈ {'apr', 'apy'}; ``ValidationError`` else."""
        if self.period not in ("apr", "apy"):
            raise ValidationError(
                f"period must be 'apr' or 'apy', got {self.period!r}"
            )


@dataclass(frozen=True, slots=True)
class Underlying:
    """One constituent token balance inside a position.

    ``asset_id`` is a canonical CAIP-19 string, never an ``ast_``
    registry id. An underlying is *raw* iff ``price`` and ``value`` are
    BOTH ``None`` (persisted raw, re-drilled against fresh prices —
    SPEC §5.3); exactly one of them ``None`` raises ``ValidationError``.
    A ``BORROWED`` underlying's ``value`` is negative; its unit ``price``
    stays positive (DECISIONS.md sign convention).
    """

    asset_id: str
    quantity: Quantity
    meta_type: MetaType
    price: Money | None = None
    value: Money | None = None

    def __post_init__(self) -> None:
        """Raw iff both ``price`` and ``value`` are None; else both set."""
        if (self.price is None) != (self.value is None):
            raise ValidationError(
                "price and value must both be set or both be None"
            )


@dataclass(frozen=True, slots=True)
class Range:
    """Concentrated-liquidity tick range (SPEC §4.3, Uniswap V3)."""

    tick_lower: int
    tick_upper: int
    in_range: bool


@dataclass(frozen=True, slots=True)
class GroupInfo:
    """Risk-unit metadata an adapter attaches to a group (SPEC §4.3)."""

    health_factor: Decimal | None = None
    ltv: Decimal | None = None
    liquidation_price: Money | None = None


@dataclass(frozen=True, slots=True)
class Position:
    """One position: an app token or a contract-position leaf.

    ``underlyings`` must be non-empty (``ValidationError``). ``id`` and
    ``group_id`` come from :func:`position_id` / :func:`group_id_for`.
    """

    id: str
    adapter_id: str
    chain_id: str
    contract_address: str
    kind: PositionKind
    position_type: PositionType
    protocol_module: ProtocolModule
    group_id: str
    underlyings: tuple[Underlying, ...]
    apy: Apy | None = None
    range: Range | None = None
    group_info: GroupInfo | None = None

    def __post_init__(self) -> None:
        """Empty ``underlyings`` raises ``ValidationError``."""
        if not self.underlyings:
            raise ValidationError("underlyings must be non-empty")

    @property
    def value(self) -> Money | None:
        """``None`` if ANY underlying is unvalued, else the exact signed
        ``Money`` sum of underlying values (all USD;
        ``CurrencyMismatchError`` propagates)."""
        values = [underlying.value for underlying in self.underlyings]
        if any(value is None for value in values):
            return None
        total = values[0]
        for value in values[1:]:
            total = total + value
        return total

    @property
    def unclaimed_fees(self) -> tuple[Underlying, ...]:
        """The ``CLAIMABLE`` underlyings, in declaration order."""
        return tuple(
            underlying
            for underlying in self.underlyings
            if underlying.meta_type is MetaType.CLAIMABLE
        )


def position_id(
    adapter_id: str,
    chain_id: str,
    contract_address: str,
    discriminator: str = "",
) -> str:
    """Deterministic position id (DECISIONS.md, pinned):

    ``"pos_" + sha256(f"{adapter_id}|{chain_id}|{contract_lower}|{discriminator}").hexdigest()[:16]``

    0x addresses are lowercased. ``discriminator`` is ``""`` unless the
    position is sub-addressed (Uniswap V3 uses the NFT token_id decimal
    string).
    """
    contract = _lower_0x(contract_address)
    preimage = f"{adapter_id}|{chain_id}|{contract}|{discriminator}"
    return "pos_" + sha256(preimage.encode()).hexdigest()[:16]


def group_id_for(adapter_id: str, chain_id: str, group_key: str) -> str:
    """Deterministic group id (DECISIONS.md, pinned):

    ``"grp_" + sha256(f"{adapter_id}|{chain_id}|{group_key}").hexdigest()[:16]``

    ``group_key`` is the risk-unit contract (V2 pair, V3 pool, Aave
    Pool); 0x addresses are lowercased.
    """
    preimage = f"{adapter_id}|{chain_id}|{_lower_0x(group_key)}"
    return "grp_" + sha256(preimage.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class PositionGroup:
    """A risk unit, not a display unit (SPEC §4.3, LlamaFolio).

    Built ONLY via :func:`make_group`, which COMPUTES ``total_value`` —
    Zerion's defect #2 (no per-group total) fixed by construction.
    """

    group_id: str
    positions: tuple[Position, ...]
    total_value: Money
    health_factor: Decimal | None
    ltv: Decimal | None
    liquidation_price: Money | None


def make_group(
    positions: tuple[Position, ...],
    *,
    group_info: GroupInfo | None = None,
) -> PositionGroup:
    """Build a :class:`PositionGroup`, COMPUTING ``total_value`` as the
    exact ``Money`` sum of the positions' values (never caller-supplied
    — SPEC §4.3 defect #2).

    Raises ``ValidationError`` if ``positions`` is empty, any position
    is unvalued, or the positions' ``group_id`` values differ.
    """
    if not positions:
        raise ValidationError("a group needs at least one position")
    if len({position.group_id for position in positions}) != 1:
        raise ValidationError("positions span multiple group_ids")
    values = [position.value for position in positions]
    if any(value is None for value in values):
        raise ValidationError("every position in a group must be valued")
    total = values[0]
    for value in values[1:]:
        total = total + value
    info = group_info if group_info is not None else GroupInfo()
    return PositionGroup(
        group_id=positions[0].group_id,
        positions=tuple(positions),
        total_value=total,
        health_factor=info.health_factor,
        ltv=info.ltv,
        liquidation_price=info.liquidation_price,
    )
