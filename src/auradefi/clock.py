"""Time as a port.

Every timestamp in auradefi is integer milliseconds since the Unix epoch
(SPEC §4.4: "ms epoch, everywhere, always"). Code that needs the current
time takes a Clock, so tests inject FrozenClock and stay deterministic.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """The time port: one method, integer milliseconds.

    A host may bind anything with this shape — a frozen clock, a replay
    clock, a clock skewed to another timezone's business day. It is a
    ``Protocol``, so there is nothing to inherit.
    """

    def now_ms(self) -> int:
        """Current time as integer milliseconds since the Unix epoch."""
        ...


class SystemClock:
    """The wall clock, and the default when a host binds no other.

    Time is a port precisely so this class is replaceable: quota windows,
    sync throttling and ``as_of_ms`` are all derived from ``now_ms()``, so
    swapping it is what makes those testable without sleeping.
    """

    def now_ms(self) -> int:
        """Current time as integer milliseconds since the Unix epoch."""
        return time.time_ns() // 1_000_000


class FrozenClock:
    """Deterministic clock for tests; moves only when advance() is called."""

    def __init__(self, now_ms: int) -> None:
        self._now_ms = int(now_ms)

    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, ms: int) -> None:
        if ms < 0:
            raise ValueError("FrozenClock only advances forward")
        self._now_ms += ms
