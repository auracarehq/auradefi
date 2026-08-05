"""Additive spam scoring (SPEC §4.2, rule #9, rotki's layered model).

Rule #9: return the liquidity number, not just a spam boolean. The
threshold is a PRODUCT decision, so ``is_spam`` takes it from the
caller. ``liquidity_usd`` is a ``Decimal`` end-to-end, never a float.

Rotki's scar: a transient source failure once wiped previously-detected
tokens. Detection here is therefore ADDITIVE, NEVER DESTRUCTIVE.
``merge`` can only ever add reasons and raise the score, and an empty
new assessment changes nothing. Assessments are values; Asset objects
are never mutated. stdlib only; may import money/ and chains/ only.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from auradefi.errors import ValidationError


@dataclass(frozen=True, slots=True)
class SpamSignal:
    """One heuristic mark: a name and a non-negative weight.

    Raises:
        ValidationError: unless ``weight >= 0``: rejects negatives AND
            NaN (NaN fails every comparison, so it would silently poison
            ``sum``/``max`` scoring downstream).
    """

    name: str
    weight: float

    def __post_init__(self) -> None:
        if not (self.weight >= 0.0):
            raise ValidationError(f"weight must be >= 0, got {self.weight}")


@dataclass(frozen=True, slots=True)
class SpamAssessment:
    """The evidence, not a verdict (rule #9): score plus the raw numbers
    so the CONSUMER picks the threshold. ``liquidity_usd`` stays a
    ``Decimal``; ``None`` means "not measured", never "zero"."""

    score: float
    reasons: tuple[str, ...]
    liquidity_usd: Decimal | None
    holder_count: int | None


def assess(
    signals: Sequence[SpamSignal],
    liquidity_usd: Decimal | None = None,
    holder_count: int | None = None,
) -> SpamAssessment:
    """Fold signals into one assessment.

    ``score`` is the plain sum of the weights; ``reasons`` are the
    signal names in input order. ``liquidity_usd`` and ``holder_count``
    pass through untouched (``Decimal`` in, ``Decimal`` out). No input
    is mutated.
    """
    return SpamAssessment(
        score=sum((signal.weight for signal in signals), 0.0),
        reasons=tuple(signal.name for signal in signals),
        liquidity_usd=liquidity_usd,
        holder_count=holder_count,
    )


def is_spam(assessment: SpamAssessment, threshold: float) -> bool:
    """True iff ``assessment.score >= threshold`` (inclusive).

    The threshold is the CALLER's decision (rule #9). This function
    holds no opinion of its own.
    """
    return assessment.score >= threshold


def merge(old: SpamAssessment, new: SpamAssessment) -> SpamAssessment:
    """Combine two assessments ADDITIVELY, NEVER DESTRUCTIVELY.

    ``reasons`` = old reasons, then new reasons not already present
    (order preserved). ``score`` = ``max(old.score, new.score)``.
    ``liquidity_usd``/``holder_count`` = the new value when it is not
    ``None``, else the old. Merging an empty ``new`` therefore returns
    an assessment equal to ``old``. A transient source failure can
    never erase detections (rotki's scar).
    """
    reasons = list(old.reasons)
    seen = set(old.reasons)
    for reason in new.reasons:
        if reason not in seen:
            reasons.append(reason)
            seen.add(reason)
    liquidity = new.liquidity_usd if new.liquidity_usd is not None else old.liquidity_usd
    holders = new.holder_count if new.holder_count is not None else old.holder_count
    return SpamAssessment(
        score=max(old.score, new.score),
        reasons=tuple(reasons),
        liquidity_usd=liquidity,
        holder_count=holders,
    )
