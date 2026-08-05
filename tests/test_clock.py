"""Foundation: Clock port: ms epoch ints, everywhere, always (SPEC §4.4)."""

from __future__ import annotations

import time

import pytest

from auradefi.clock import Clock, FrozenClock, SystemClock


def test_system_clock_returns_ms_epoch_int():
    now = SystemClock().now_ms()
    assert isinstance(now, int)
    assert abs(now - time.time() * 1000) < 5_000


def test_frozen_clock_is_deterministic(frozen_clock):
    assert frozen_clock.now_ms() == 1_754_000_000_000
    assert frozen_clock.now_ms() == 1_754_000_000_000


def test_frozen_clock_advances_explicitly(frozen_clock):
    frozen_clock.advance(1_500)
    assert frozen_clock.now_ms() == 1_754_000_001_500


def test_frozen_clock_never_goes_backwards(frozen_clock):
    with pytest.raises(ValueError):
        frozen_clock.advance(-1)


def test_both_implementations_satisfy_the_protocol():
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FrozenClock(0), Clock)
