"""Per-tenant three-window quota counters (SPEC §7.3).

Zerion's three-window shape — Second / Day / Month, each with
limit / remaining / reset — scoped **per tenant (project), never per org**.

Pinned window algorithms (docs/internal/DECISIONS.md "Quota windows"):

  second: key = now_ms // 1000;        reset_at_ms = (key + 1) * 1000
  day:    key = now_ms // 86_400_000;  reset_at_ms = (key + 1) * 86_400_000  (UTC)
  month:  key = (year, month) in UTC from
          datetime.fromtimestamp(now_ms // 1000, tz=timezone.utc);
          reset_at_ms = first instant of the next UTC month, in ms

A rejected hit consumes nothing. All timestamps are ms-epoch ints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from auradefi.clock import Clock
from auradefi.errors import QuotaExceededError

_SECOND_MS = 1_000
_DAY_MS = 86_400_000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_MS = timedelta(milliseconds=1)

# Window key type: int for second/day, (year, month) for month.
_WindowKey = int | tuple[int, int]


@dataclass(frozen=True, slots=True)
class QuotaLimits:
    """Per-tenant limits for the three windows."""

    per_second: int
    per_day: int
    per_month: int


@dataclass(frozen=True, slots=True)
class WindowSnapshot:
    """One window's limit / remaining / reset, all ints (reset is ms epoch)."""

    limit: int
    remaining: int
    reset_at_ms: int


def _month_key(now_ms: int) -> tuple[int, int]:
    moment = datetime.fromtimestamp(now_ms // 1_000, tz=timezone.utc)
    return (moment.year, moment.month)


def _month_reset_ms(key: tuple[int, int]) -> int:
    year, month = key
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return (datetime(year, month, 1, tzinfo=timezone.utc) - _EPOCH) // _ONE_MS


def _current_windows(now_ms: int) -> dict[str, tuple[_WindowKey, int]]:
    """Each window's ``(key, reset_at_ms)`` at ``now_ms``, keyed by name."""
    second_key = now_ms // _SECOND_MS
    day_key = now_ms // _DAY_MS
    month_key = _month_key(now_ms)
    return {
        "second": (second_key, (second_key + 1) * _SECOND_MS),
        "day": (day_key, (day_key + 1) * _DAY_MS),
        "month": (month_key, _month_reset_ms(month_key)),
    }


class QuotaCounter:
    """Quota counters keyed by project_id; all state on the instance.

    One tenant's consumption NEVER touches another's (SPEC §7.3 — the fix
    for Zerion's org-scoped limits with no per-tenant attribution).
    """

    def __init__(self, limits: QuotaLimits, clock: Clock) -> None:
        """Bind the limits and the clock; counters start empty."""
        self._limits = {
            "second": limits.per_second,
            "day": limits.per_day,
            "month": limits.per_month,
        }
        self._clock = clock
        # project_id -> window name -> (window key, count in that window)
        self._counts: dict[str, dict[str, tuple[_WindowKey, int]]] = {}

    def _rolled_counts(
        self,
        project_id: str,
        windows: dict[str, tuple[_WindowKey, int]],
    ) -> dict[str, int]:
        """Effective counts at the given windows; stale windows read as 0."""
        stored = self._counts.get(project_id, {})
        counts: dict[str, int] = {}
        for name, (key, _reset) in windows.items():
            prior = stored.get(name)
            counts[name] = prior[1] if prior is not None and prior[0] == key else 0
        return counts

    def hit(self, project_id: str) -> None:
        """Consume one unit from all three of this project's windows.

        Reads ``clock.now_ms()`` exactly ONCE. Rolls stale windows (their
        count becomes 0). If any window would exceed its limit, raises
        ``auradefi.errors.QuotaExceededError`` with a message naming the
        exceeded window ('second' | 'day' | 'month') and that window's
        ``reset_at_ms`` — WITHOUT consuming from any window. Otherwise
        increments all three.
        """
        now_ms = self._clock.now_ms()
        windows = _current_windows(now_ms)
        counts = self._rolled_counts(project_id, windows)
        for name, (_key, reset_at_ms) in windows.items():
            if counts[name] >= self._limits[name]:
                raise QuotaExceededError(
                    f"quota exceeded in the '{name}' window for {project_id!r}: "
                    f"limit {self._limits[name]}, window resets at {reset_at_ms}"
                )
        self._counts[project_id] = {
            name: (key, counts[name] + 1) for name, (key, _reset) in windows.items()
        }

    def snapshot(self, project_id: str) -> dict[str, WindowSnapshot]:
        """Read-only view; consumes nothing.

        Returns a dict with keys exactly 'second', 'day', 'month'. A
        project with no consumption in a current window shows
        ``remaining == limit``.
        """
        windows = _current_windows(self._clock.now_ms())
        counts = self._rolled_counts(project_id, windows)
        return {
            name: WindowSnapshot(
                limit=self._limits[name],
                remaining=self._limits[name] - counts[name],
                reset_at_ms=reset_at_ms,
            )
            for name, (_key, reset_at_ms) in windows.items()
        }
