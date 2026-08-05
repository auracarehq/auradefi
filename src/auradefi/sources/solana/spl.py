"""Pure SPL token-account parsing and balance assembly (SPEC §3.2, §3.3).

No HTTP: this module takes already-decoded ``jsonParsed`` rows from
``getTokenAccountsByOwner`` and turns them into typed records. Amounts
parse via ``int()`` from the ``amount`` STRING — never through float
(SPEC rules #1/#2), and the ``uiAmount`` float member of ``tokenAmount``
is NEVER read.

Token-2022 ScaledUiAmount (docs/internal/DECISIONS.md, "Solana ScaledUiAmount
detection"; SPEC §4.1 warning): the ``raw / 10**decimals`` identity does
not hold for every mint, so BOTH representations are carried —
``Quantity(int(amount), decimals)`` for arithmetic and ``uiAmountString``
verbatim for display — and the break is detected as

    scaled_ui = (ui_amount_string != str(quantity))

a pure string comparison. ``Quantity.__str__`` trims trailing fractional
zeros exactly as ``uiAmountString`` does, so a normal token compares
equal. The jsonParsed ``extensions`` list is deliberately NOT consulted:
older RPC nodes omit it, so it cannot be a correctness input.

CAIP-19 (SPEC §4.2) — Solana references keep base58 case, never
lowercased:

    native  solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/slip44:501
    token   solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/token:<mint>
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation, Overflow, localcontext

from auradefi.chains.solana import MAINNET, SLIP44
from auradefi.errors import SourceError, ValidationError
from auradefi.money.quantity import Quantity

NATIVE_DECIMALS = 9
NATIVE_CAIP19 = f"{MAINNET}/slip44:{SLIP44}"

# Unsigned base-10 digit strings only. Bare int() is too lenient for RPC
# amounts ("1_0", " 10 ", "+1" all parse); the wire contract is digits.
_DIGITS = re.compile(r"[0-9]+")

# uiAmountString is checked as strictly, because it is BOTH a display
# output and (when scaled) a summand: a quiet "NaN" never signals
# InvalidOperation, so it would reach a balance verbatim, and "1e30"
# would defeat the precision bound in _display_sum and round a display.
_NUMERAL = re.compile(r"[0-9]+(\.[0-9]+)?")


@dataclass(frozen=True, slots=True)
class TokenAccountRecord:
    """One parsed SPL token account owned by one address.

    ``program`` is the owning token program as reported by the RPC
    (``"spl-token"`` or ``"spl-token-2022"``). ``quantity`` is
    ``Quantity(int(amount), decimals)`` — exact base units.
    ``ui_amount_string`` is the RPC's ``uiAmountString`` VERBATIM, and
    ``scaled_ui`` is ``ui_amount_string != str(quantity)``.
    """

    pubkey: str
    mint: str
    owner: str
    program: str
    quantity: Quantity
    ui_amount_string: str
    scaled_ui: bool


@dataclass(frozen=True, slots=True)
class MintBalance:
    """The summed holding of one mint across an owner's token accounts.

    ``quantity`` sums the constituents' ``raw`` at their shared
    ``decimals``. ``scaled_ui`` is true when ANY constituent was scaled;
    ``ui_amount_string`` is ``str(quantity)`` when nothing was scaled,
    else the exact ``Decimal`` sum of the constituents' displayed
    strings.
    """

    mint: str
    quantity: Quantity
    ui_amount_string: str
    scaled_ui: bool


@dataclass(frozen=True, slots=True)
class SolanaBalance:
    """One typed balance of a Solana address: native SOL or one mint.

    Native SOL: ``caip19`` is :data:`NATIVE_CAIP19`, ``mint`` is
    ``None``, ``quantity`` is ``Quantity(lamports, 9)``, ``scaled_ui``
    is ``False``. SPL token: ``caip19`` is
    ``f"{MAINNET}/token:{mint}"`` with base58 case preserved verbatim.
    """

    caip19: str
    quantity: Quantity
    mint: str | None
    ui_amount_string: str
    scaled_ui: bool


def token_caip19(mint: str) -> str:
    """The CAIP-19 for an SPL mint — base58 case PRESERVED, never lowered."""
    return f"{MAINNET}/token:{mint}"


def _require_dict(value: object, label: str) -> dict:
    """``value`` as a dict, or ``SourceError`` naming ``label``."""
    if not isinstance(value, dict):
        raise SourceError(f"{label} must be an object, got {type(value).__name__}")
    return value


def _require_str(container: dict, key: str) -> str:
    """``container[key]`` as a str, or ``SourceError`` naming ``key``."""
    value = container.get(key)
    if not isinstance(value, str):
        raise SourceError(f"{key} must be a string, got {value!r}")
    return value


def _require_numeral(text: str, label: str) -> str:
    """``text`` fullmatching ``[0-9]+(\\.[0-9]+)?``, or ``SourceError``.

    Rejects the empty string, signs, whitespace, underscores, exponent
    forms and the specials ``"NaN"``/``"Infinity"``.
    """
    if _NUMERAL.fullmatch(text) is None:
        raise SourceError(f"{label} must be a plain decimal numeral: {text!r}")
    return text


def _quantity(token_amount: dict) -> Quantity:
    """The exact base-unit amount of a ``tokenAmount`` dict.

    ``amount`` must be an unsigned base-10 digit STRING — parsed with
    ``int()``, never through float — and ``decimals`` a non-bool
    ``int >= 0``.
    """
    raw = token_amount.get("amount")
    decimals = token_amount.get("decimals")
    if not isinstance(raw, str) or _DIGITS.fullmatch(raw) is None:
        raise SourceError(f"tokenAmount amount must be a digit string: {raw!r}")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
        raise SourceError(f"tokenAmount decimals must be an int >= 0: {decimals!r}")
    return Quantity(int(raw), decimals)


def _parse_row(row: object) -> TokenAccountRecord:
    """One jsonParsed ``result.value`` row as a record, or ``SourceError``."""
    envelope = _require_dict(row, "token account row")
    pubkey = _require_str(envelope, "pubkey")
    account = _require_dict(envelope.get("account"), "row account")
    data = _require_dict(account.get("data"), "account data")
    program = _require_str(data, "program")
    parsed = _require_dict(data.get("parsed"), "account data parsed")
    if parsed.get("type") != "account":
        raise SourceError(f"parsed type must be 'account': {parsed.get('type')!r}")
    info = _require_dict(parsed.get("info"), "parsed info")
    mint = _require_str(info, "mint")
    if not mint:
        raise SourceError("mint must be a non-empty string")
    owner = _require_str(info, "owner")
    token_amount = _require_dict(info.get("tokenAmount"), "info tokenAmount")
    quantity = _quantity(token_amount)
    displayed = _require_numeral(
        _require_str(token_amount, "uiAmountString"), "uiAmountString"
    )
    return TokenAccountRecord(
        pubkey=pubkey,
        mint=mint,
        owner=owner,
        program=program,
        quantity=quantity,
        ui_amount_string=displayed,
        scaled_ui=displayed != str(quantity),
    )


def parse_token_accounts(rows: Iterable[object]) -> list[TokenAccountRecord]:
    """Parse concatenated ``getTokenAccountsByOwner`` ``result.value`` rows.

    Each row must be a dict shaped
    ``{"pubkey": str, "account": {"data": {"program": str, "parsed":
    {"type": "account", "info": {"mint": str, "owner": str,
    "tokenAmount": {"amount": str, "decimals": int,
    "uiAmountString": str}}}}}}``.

    Every field is checked: ``pubkey`` a str, ``program`` a str,
    ``parsed.type`` exactly ``"account"``, ``mint`` a non-empty str,
    ``owner`` a str, ``tokenAmount`` a dict whose ``amount`` fullmatches
    ``[0-9]+`` (a base-10 digit STRING — parsed with ``int()``, never
    through float), ``decimals`` a non-bool ``int >= 0`` and
    ``uiAmountString`` a str fullmatching ``[0-9]+(\\.[0-9]+)?`` — a
    plain unsigned numeral, so ``""``, ``"NaN"``, ``"Infinity"`` and
    ``"1e30"`` are rejected instead of being displayed or summed. The
    ``uiAmount`` float member is NEVER read and may be absent or
    nonsense.

    Order is preserved. Zero-amount accounts parse normally — pruning
    happens in :func:`build_balances`.

    Raises:
        SourceError: on any shape or type violation above.
    """
    return [_parse_row(row) for row in rows]


def _plain(total: Decimal) -> str:
    """``total`` as a plain string: no exponent, no trailing zeros.

    ``format(_, "f")`` never emits scientific notation. Trailing zeros
    are trimmed only inside a fraction, so ``250`` stays ``"250"``.
    """
    text = format(total, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _display_sum(records: list[TokenAccountRecord]) -> str:
    """The exact ``Decimal`` sum of the constituents' displayed strings.

    Scaled displays cannot be recomputed from ``raw``, so the strings
    themselves are summed. Each is re-checked as a plain numeral —
    records need not come from :func:`parse_token_accounts` — which is
    what makes the precision bound sound: a numeral's digit count never
    exceeds its length, so widening past the total length holds every
    partial sum exactly. A fresh :class:`Context` keeps the traps
    deterministic whatever the caller's ambient context, and the total
    is checked finite: no non-finite value may become a display string.
    """
    strings = [
        _require_numeral(record.ui_amount_string, "ui_amount_string")
        for record in records
    ]
    context = Context(prec=sum(len(text) for text in strings) + 2)
    with localcontext(context):
        try:
            total = sum((Decimal(text) for text in strings), Decimal(0))
        except (InvalidOperation, Overflow) as exc:
            raise SourceError(f"unsummable displayed amounts: {strings!r}") from exc
    if not total.is_finite():
        raise SourceError(f"displayed amounts sum to {total}: {strings!r}")
    return _plain(total)


def _merge(mint: str, records: list[TokenAccountRecord]) -> MintBalance:
    """One mint's accounts as a single balance, or ``SourceError``."""
    decimals = records[0].quantity.decimals
    for record in records:
        if record.quantity.decimals != decimals:
            raise SourceError(
                f"token accounts of mint {mint} disagree on decimals: "
                f"{decimals} vs {record.quantity.decimals}"
            )
    quantity = Quantity(sum(record.quantity.raw for record in records), decimals)
    scaled_ui = any(record.scaled_ui for record in records)
    return MintBalance(
        mint=mint,
        quantity=quantity,
        ui_amount_string=_display_sum(records) if scaled_ui else str(quantity),
        scaled_ui=scaled_ui,
    )


def aggregate_by_mint(records: Iterable[TokenAccountRecord]) -> list[MintBalance]:
    """Group parsed accounts by exact mint, summing raw base units.

    ``quantity`` is ``Quantity(sum of raws, decimals)`` at the mint's
    shared scale. ``scaled_ui`` is ``any(constituent.scaled_ui)``.
    ``ui_amount_string`` is ``str(quantity)`` when not scaled; when
    scaled it is the exact ``Decimal`` sum of the constituents'
    ``ui_amount_string`` values rendered with ``format(total, "f")``
    (never ``str()`` — that emits scientific notation below 1e-6) with
    trailing fractional zeros and any trailing ``"."`` stripped. A
    single scaled constituent therefore passes through verbatim.

    Output is sorted ascending by mint (base58, case-sensitive).

    Raises:
        SourceError: when two accounts of the SAME mint disagree on
            ``decimals`` (the message names the mint), or when a scaled
            group carries a ``ui_amount_string`` that is not a plain
            unsigned numeral.
    """
    groups: dict[str, list[TokenAccountRecord]] = {}
    for record in records:
        groups.setdefault(record.mint, []).append(record)
    return [_merge(mint, groups[mint]) for mint in sorted(groups)]


def build_balances(lamports: int, mints: Iterable[MintBalance]) -> list[SolanaBalance]:
    """Assemble an address's balance set: native SOL first, then mints.

    The native record is emitted FIRST and only when ``lamports > 0``:
    ``Quantity(lamports, 9)``, ``ui_amount_string == str(quantity)``,
    ``scaled_ui`` False, ``mint`` None. Then one record per
    :class:`MintBalance` whose ``quantity.raw`` is non-zero, in the
    given order — zero-raw holdings are omitted, mirroring
    ``sources/evm/etherscan.py``.

    Raises:
        ValidationError: if ``lamports`` is a ``bool``, is not an
            ``int``, or is negative.
    """
    if isinstance(lamports, bool) or not isinstance(lamports, int):
        raise ValidationError(f"lamports must be an int, got {type(lamports).__name__}")
    if lamports < 0:
        raise ValidationError(f"lamports must be >= 0, got {lamports}")

    balances: list[SolanaBalance] = []
    if lamports > 0:
        native = Quantity(lamports, NATIVE_DECIMALS)
        balances.append(SolanaBalance(NATIVE_CAIP19, native, None, str(native), False))
    balances.extend(
        SolanaBalance(
            caip19=token_caip19(balance.mint),
            quantity=balance.quantity,
            mint=balance.mint,
            ui_amount_string=balance.ui_amount_string,
            scaled_ui=balance.scaled_ui,
        )
        for balance in mints
        if balance.quantity.raw != 0
    )
    return balances
