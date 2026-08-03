"""Quota: per-tenant Second/Day/Month windows (SPEC §7.3).

Golden ``reset_at_ms`` values below were derived independently from the
pinned window algorithms (docs/DECISIONS.md "Quota windows") with
python3/datetime — never with the code under test:

  T0            = 1767225600000  # 2026-01-01T00:00:00Z
  second reset  = 1767225601000
  day reset     = 1767312000000  # 2026-01-02T00:00:00Z
  month reset   = 1769904000000  # 2026-02-01T00:00:00Z
  March 1 2026  = 1772323200000

Year-rollover vectors (December's month window resets into the NEXT year):

  Dec 31 last ms = 1798761599999  # 2026-12-31T23:59:59.999Z
  Jan 1 2027     = 1798761600000  # 2027-01-01T00:00:00Z — (2026, 12) reset
  Feb 1 2027     = 1801440000000  # 2027-02-01T00:00:00Z — (2027, 1) reset
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from auradefi.clock import FrozenClock
from auradefi.errors import QuotaExceededError
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits, WindowSnapshot

T0 = 1_767_225_600_000  # 2026-01-01T00:00:00Z
SECOND_RESET = 1_767_225_601_000
DAY_RESET = 1_767_312_000_000  # 2026-01-02T00:00:00Z
MONTH_RESET = 1_769_904_000_000  # 2026-02-01T00:00:00Z
MARCH_1 = 1_772_323_200_000  # 2026-03-01T00:00:00Z
JAN_30 = 1_769_731_200_000  # 2026-01-30T00:00:00Z
JAN_LAST_MS = 1_769_903_999_999  # 2026-01-31T23:59:59.999Z
DEC_31_2026_LAST_MS = 1_798_761_599_999  # 2026-12-31T23:59:59.999Z
JAN_1_2027 = 1_798_761_600_000  # 2027-01-01T00:00:00Z
FEB_1_2027 = 1_801_440_000_000  # 2027-02-01T00:00:00Z
HUGE = 10**77

A = "proj_a"
B = "proj_b"


def make_counter(per_second=2, per_day=5, per_month=10, now_ms=T0):
    clock = FrozenClock(now_ms)
    limits = QuotaLimits(per_second=per_second, per_day=per_day, per_month=per_month)
    return QuotaCounter(limits, clock), clock


class RecordingClock:
    """Counts now_ms() reads — hit() must read the clock exactly once."""

    def __init__(self, now_ms: int) -> None:
        self._inner = FrozenClock(now_ms)
        self.calls = 0

    def now_ms(self) -> int:
        self.calls += 1
        return self._inner.now_ms()


# --------------------------------------------------------------- immutability


def test_quota_limits_is_frozen():
    limits = QuotaLimits(per_second=1, per_day=2, per_month=3)
    with pytest.raises(FrozenInstanceError):
        limits.per_second = 9


def test_window_snapshot_is_frozen():
    snap = WindowSnapshot(limit=1, remaining=1, reset_at_ms=T0)
    with pytest.raises(FrozenInstanceError):
        snap.remaining = 0


# --------------------------------------------------------------------- golden


def test_golden_snapshot_at_2026_01_01_utc_midnight():
    counter, _ = make_counter()
    snap = counter.snapshot(A)
    assert set(snap) == {"second", "day", "month"}
    assert snap["second"] == WindowSnapshot(limit=2, remaining=2, reset_at_ms=SECOND_RESET)
    assert snap["day"] == WindowSnapshot(limit=5, remaining=5, reset_at_ms=DAY_RESET)
    assert snap["month"] == WindowSnapshot(limit=10, remaining=10, reset_at_ms=MONTH_RESET)


def test_snapshot_is_read_only_and_hit_consumes_one_from_each_window():
    counter, _ = make_counter()
    assert counter.snapshot(A) == counter.snapshot(A)  # snapshot consumed nothing
    counter.hit(A)
    snap = counter.snapshot(A)
    assert snap["second"].remaining == 1
    assert snap["day"].remaining == 4
    assert snap["month"].remaining == 9


# ------------------------------------------------------------------ rejection


def test_second_window_rejection_names_window_and_reset():
    counter, _ = make_counter()
    counter.hit(A)
    counter.hit(A)
    with pytest.raises(QuotaExceededError) as excinfo:
        counter.hit(A)
    message = str(excinfo.value)
    assert "second" in message
    assert str(SECOND_RESET) in message


def test_rejected_hit_consumes_nothing():
    counter, _ = make_counter()
    counter.hit(A)
    counter.hit(A)
    before = counter.snapshot(A)
    assert before["day"].remaining == 3
    assert before["month"].remaining == 8
    with pytest.raises(QuotaExceededError):
        counter.hit(A)
    assert counter.snapshot(A) == before


# ---------------------------------------------------------------- walkthrough


def test_walkthrough_day_then_month_windows_across_january_2026():
    counter, clock = make_counter()  # 2/sec, 5/day, 10/month at T0
    counter.hit(A)
    counter.hit(A)  # totals 1-2; second window full
    with pytest.raises(QuotaExceededError) as exc3:
        counter.hit(A)
    assert "second" in str(exc3.value)

    clock.advance(1000)  # second window rolls
    counter.hit(A)
    counter.hit(A)  # totals 3-4
    clock.advance(1000)
    counter.hit(A)  # total 5; day window full
    with pytest.raises(QuotaExceededError) as exc6:
        counter.hit(A)
    message = str(exc6.value)
    assert "day" in message
    assert str(DAY_RESET) in message
    assert counter.snapshot(A)["month"].remaining == 5  # day rejection consumed nothing

    # Jan 30: day rolled, same month — consume month totals 6-10
    clock.advance(JAN_30 - (T0 + 2000))
    counter.hit(A)
    counter.hit(A)
    clock.advance(1000)
    counter.hit(A)
    counter.hit(A)
    clock.advance(1000)
    counter.hit(A)  # month window full at 10

    # last ms of January: second and day rolled, month still (2026, 1)
    clock.advance(JAN_LAST_MS - (JAN_30 + 2000))
    with pytest.raises(QuotaExceededError) as exc11:
        counter.hit(A)
    message = str(exc11.value)
    assert "month" in message
    assert str(MONTH_RESET) in message
    snap = counter.snapshot(A)
    assert snap["month"].remaining == 0
    assert snap["day"].remaining == 5  # rejection consumed nothing in the fresh day

    clock.advance(1)  # 2026-02-01T00:00:00Z — month rolls
    counter.hit(A)
    snap = counter.snapshot(A)
    assert snap["month"] == WindowSnapshot(limit=10, remaining=9, reset_at_ms=MARCH_1)
    assert snap["day"] == WindowSnapshot(limit=5, remaining=4, reset_at_ms=1_769_990_400_000)
    assert snap["second"] == WindowSnapshot(limit=2, remaining=1, reset_at_ms=1_769_904_001_000)


def test_golden_december_month_window_resets_into_the_next_year():
    # 2026-12-31T23:59:59.999Z: month key (2026, 12) must reset at
    # 2027-01-01T00:00:00Z — the year-rollover branch of the pinned
    # month algorithm. A `year + 1 -> year` mutation would yield
    # 1767225600000 (2026-01-01) and fail the golden literal here.
    counter, clock = make_counter(now_ms=DEC_31_2026_LAST_MS)
    snap = counter.snapshot(A)
    assert snap["month"] == WindowSnapshot(limit=10, remaining=10, reset_at_ms=JAN_1_2027)

    counter.hit(A)
    assert counter.snapshot(A)["month"].remaining == 9

    clock.advance(1)  # 2027-01-01T00:00:00Z — December's month window rolls
    snap = counter.snapshot(A)
    assert snap["month"] == WindowSnapshot(limit=10, remaining=10, reset_at_ms=FEB_1_2027)
    assert snap["second"] == WindowSnapshot(
        limit=2, remaining=2, reset_at_ms=1_798_761_601_000
    )
    assert snap["day"] == WindowSnapshot(
        limit=5, remaining=5, reset_at_ms=1_798_848_000_000
    )
    counter.hit(A)  # consumption resumes in the fresh (2027, 1) window
    assert counter.snapshot(A)["month"].remaining == 9


def test_stale_windows_roll_to_full_remaining():
    counter, clock = make_counter()
    counter.hit(A)
    counter.hit(A)
    clock.advance(1000)
    snap = counter.snapshot(A)
    assert snap["second"] == WindowSnapshot(limit=2, remaining=2, reset_at_ms=T0 + 2000)
    assert snap["day"].remaining == 3


# --------------------------------------------------------------- tenant scope


def test_exhausting_one_tenant_never_touches_another():
    counter, _ = make_counter()
    counter.hit(A)
    counter.hit(A)
    with pytest.raises(QuotaExceededError):
        counter.hit(A)
    assert counter.snapshot(B)["second"] == WindowSnapshot(
        limit=2, remaining=2, reset_at_ms=SECOND_RESET
    )
    counter.hit(B)  # succeeds: proj_a's exhaustion is invisible to proj_b
    counter.hit(B)
    assert counter.snapshot(B)["day"].remaining == 3
    assert counter.snapshot(A)["second"].remaining == 0
    assert counter.snapshot(A)["day"].remaining == 3  # proj_b's hits changed nothing


# ----------------------------------------------------------------- clock read


def test_hit_reads_the_clock_exactly_once():
    clock = RecordingClock(T0)
    counter = QuotaCounter(QuotaLimits(per_second=2, per_day=5, per_month=10), clock)
    clock.calls = 0
    counter.hit(A)
    assert clock.calls == 1
    counter.hit(A)
    clock.calls = 0
    with pytest.raises(QuotaExceededError):
        counter.hit(A)
    assert clock.calls == 1  # a rejection reads once too


# ----------------------------------------------------------------- boundaries


def test_zero_per_second_limit_rejects_the_first_hit_consuming_nothing():
    counter, _ = make_counter(per_second=0)
    with pytest.raises(QuotaExceededError) as excinfo:
        counter.hit(A)
    assert "second" in str(excinfo.value)
    assert counter.snapshot(A)["day"].remaining == 5


def test_huge_limits_are_exact_python_ints():
    counter, _ = make_counter(per_second=HUGE, per_day=HUGE, per_month=HUGE)
    counter.hit(A)
    counter.hit(A)
    snap = counter.snapshot(A)
    assert snap["second"].remaining == HUGE - 2
    assert snap["day"].remaining == HUGE - 2
    assert snap["month"].limit == HUGE
