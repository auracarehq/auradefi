"""SPEC §3.2: files target 300 lines, 400 hard — no allowlist, ever.

The 400 gate has no escape hatch by design: LlamaFolio and Zapper Studio
both died of unreviewable growth. Split the module instead.
"""

from __future__ import annotations

import warnings
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "auradefi"
SOFT_CAP = 300
HARD_CAP = 400


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_no_source_file_exceeds_the_400_line_hard_cap():
    offenders = sorted(
        f"{path.relative_to(SRC.parent)}: {count} lines"
        for path in SRC.rglob("*.py")
        if (count := _line_count(path)) > HARD_CAP
    )
    assert not offenders, (
        f"files exceed the {HARD_CAP}-line hard cap (no allowlist — split them):\n"
        + "\n".join(offenders)
    )


def test_soft_cap_overruns_are_reported():
    over_soft = sorted(
        f"{path.relative_to(SRC.parent)}: {count} lines"
        for path in SRC.rglob("*.py")
        if SOFT_CAP < (count := _line_count(path)) <= HARD_CAP
    )
    if over_soft:
        warnings.warn(
            UserWarning(
                f"files over the {SOFT_CAP}-line soft target (hard cap {HARD_CAP}):\n"
                + "\n".join(over_soft)
            )
        )
