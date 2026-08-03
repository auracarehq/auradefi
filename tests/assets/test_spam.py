"""Additive spam scoring (SPEC §4.2, rule #9).

The load-bearing contracts under test:

* rule #9 — ``is_spam`` takes the THRESHOLD FROM THE CALLER; the library
  ships the numbers (``liquidity_usd`` as ``Decimal``, ``holder_count``)
  and holds no opinion.
* rotki's scar — ``merge`` is additive, never destructive: an empty new
  assessment (a transient source failure) erases nothing.

Score weights use binary-exact floats (0.25/0.5/0.75) so equality
assertions are exact. ``liquidity_usd`` is money-shaped: it must stay a
``Decimal`` end-to-end and is never asserted through float coercion.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from auradefi.assets.spam import SpamAssessment, SpamSignal, assess, is_spam, merge
from auradefi.errors import ValidationError

# --- SpamSignal -----------------------------------------------------------------


def test_spam_signal_fields_and_frozen():
    signal = SpamSignal(name="no_liquidity", weight=0.5)
    assert signal.name == "no_liquidity"
    assert signal.weight == 0.5
    with pytest.raises(dataclasses.FrozenInstanceError):
        signal.weight = 1.0  # type: ignore[misc]


def test_spam_signal_zero_weight_is_valid():
    assert SpamSignal(name="airdropped", weight=0.0).weight == 0.0


@pytest.mark.parametrize(
    "weight", [-0.25, -1.0, -float(10**77), float("nan"), -float("inf")]
)
def test_spam_signal_negative_weight_raises(weight):
    # NaN fails every comparison and -inf is negative: both would poison
    # sum/max scoring downstream, so both are rejected like negatives.
    with pytest.raises(ValidationError):
        SpamSignal(name="bad", weight=weight)


# --- SpamAssessment shape ---------------------------------------------------------


def test_spam_assessment_fields_and_frozen():
    assessment = SpamAssessment(
        score=0.75,
        reasons=("no_liquidity", "honeypot"),
        liquidity_usd=Decimal("12.50"),
        holder_count=3,
    )
    assert assessment.score == 0.75
    assert assessment.reasons == ("no_liquidity", "honeypot")
    assert assessment.liquidity_usd == Decimal("12.50")
    assert assessment.holder_count == 3
    with pytest.raises(dataclasses.FrozenInstanceError):
        assessment.score = 0.0  # type: ignore[misc]


# --- assess: additive score, reasons in input order --------------------------------


def test_assess_sums_weights_and_keeps_reason_input_order():
    signals = [
        SpamSignal(name="zebra_pattern", weight=0.5),
        SpamSignal(name="airdropped", weight=0.25),
        SpamSignal(name="no_liquidity", weight=1.0),
    ]
    assessment = assess(signals)
    assert assessment.score == 1.75  # 0.5 + 0.25 + 1.0, binary-exact
    # input order, NOT sorted order (zebra first proves it)
    assert assessment.reasons == ("zebra_pattern", "airdropped", "no_liquidity")


def test_assess_no_signals_scores_zero_with_no_reasons():
    assessment = assess([])
    assert assessment.score == 0.0
    assert assessment.reasons == ()
    assert assessment.liquidity_usd is None
    assert assessment.holder_count is None


def test_assess_defaults_liquidity_and_holders_to_none():
    assessment = assess([SpamSignal(name="honeypot", weight=0.5)])
    assert assessment.liquidity_usd is None
    assert assessment.holder_count is None


def test_assess_passes_liquidity_through_as_decimal():
    assessment = assess(
        [SpamSignal(name="low_liquidity", weight=0.5)],
        liquidity_usd=Decimal("1234.56"),
        holder_count=42,
    )
    assert isinstance(assessment.liquidity_usd, Decimal)
    assert not isinstance(assessment.liquidity_usd, float)
    assert assessment.liquidity_usd == Decimal("1234.56")
    assert isinstance(assessment.holder_count, int)
    assert assessment.holder_count == 42


def test_assess_does_not_mutate_the_signals_sequence():
    signals = [
        SpamSignal(name="a", weight=0.25),
        SpamSignal(name="b", weight=0.5),
    ]
    snapshot = list(signals)
    assess(signals)
    assert signals == snapshot


# --- is_spam: the threshold belongs to the caller (rule #9) ------------------------


def test_is_spam_same_assessment_different_caller_thresholds():
    assessment = SpamAssessment(
        score=0.75, reasons=("no_liquidity",), liquidity_usd=None, holder_count=None
    )
    assert is_spam(assessment, threshold=0.5) is True
    assert is_spam(assessment, threshold=0.9) is False


def test_is_spam_threshold_is_inclusive():
    assessment = SpamAssessment(
        score=0.5, reasons=("honeypot",), liquidity_usd=None, holder_count=None
    )
    assert is_spam(assessment, threshold=0.5) is True


def test_is_spam_zero_threshold_flags_a_zero_score():
    clean = SpamAssessment(score=0.0, reasons=(), liquidity_usd=None, holder_count=None)
    assert is_spam(clean, threshold=0.0) is True
    assert is_spam(clean, threshold=0.25) is False


# --- merge: additive, never destructive (rotki's scar) -----------------------------


def _old() -> SpamAssessment:
    return SpamAssessment(
        score=0.75,
        reasons=("no_liquidity", "honeypot"),
        liquidity_usd=Decimal("12.50"),
        holder_count=3,
    )


def test_merge_with_empty_new_preserves_everything_old():
    """The transient-source-failure scar test: a source that comes back
    with nothing must not erase previously-detected marks."""
    merged = merge(_old(), assess([]))
    assert merged.reasons == ("no_liquidity", "honeypot")
    assert merged.score == 0.75
    assert isinstance(merged.liquidity_usd, Decimal)
    assert merged.liquidity_usd == Decimal("12.50")
    assert merged.holder_count == 3


def test_merge_appends_only_unseen_reasons_in_new_order():
    old = SpamAssessment(
        score=0.5, reasons=("a", "b"), liquidity_usd=None, holder_count=None
    )
    new = SpamAssessment(
        score=0.25, reasons=("b", "c", "a", "d"), liquidity_usd=None, holder_count=None
    )
    merged = merge(old, new)
    assert merged.reasons == ("a", "b", "c", "d")


def test_merge_score_is_max_not_sum():
    old = SpamAssessment(score=0.5, reasons=("a",), liquidity_usd=None, holder_count=None)
    new = SpamAssessment(score=0.75, reasons=("b",), liquidity_usd=None, holder_count=None)
    assert merge(old, new).score == 0.75
    assert merge(new, old).score == 0.75  # never 1.25


def test_merge_new_metadata_wins_only_when_present():
    old = _old()
    new = SpamAssessment(
        score=0.0, reasons=(), liquidity_usd=Decimal("99.01"), holder_count=None
    )
    merged = merge(old, new)
    assert isinstance(merged.liquidity_usd, Decimal)
    assert not isinstance(merged.liquidity_usd, float)
    assert merged.liquidity_usd == Decimal("99.01")
    assert merged.holder_count == 3  # new None -> old kept

    flipped = merge(old, SpamAssessment(0.0, (), None, 7))
    assert flipped.liquidity_usd == Decimal("12.50")
    assert flipped.holder_count == 7


def test_merge_leaves_both_inputs_untouched():
    old, new = _old(), assess([SpamSignal(name="new_mark", weight=0.25)])
    merge(old, new)
    assert old == _old()
    assert new.reasons == ("new_mark",)
    assert new.score == 0.25
