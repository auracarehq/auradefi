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
    def now_ms(self) -> int:
        """Current time as integer milliseconds since the Unix epoch."""
        ...


class SystemClock:
    def now_ms(self) -> int:
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
