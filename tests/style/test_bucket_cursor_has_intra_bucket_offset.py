"""A resume cursor derived from a page aggregate needs an intra-bucket offset.

Motivating finding (0.1.1-wave2, blocker, ``embed/sync.py`` backfill):
``backfill_cursor = min(page_blocks)`` makes the resume position a BLOCK
NUMBER, while the pagination unit is a ROW. A block number cannot express
"the middle of a block", so both boundary choices lose:

* EXCLUSIVE (``[0, cursor - 1]``) silently drops the rows of the boundary
  block that the page cut in half, and still reports the phase complete;
* INCLUSIVE (``[0, cursor]``) can never advance once one block holds more
  rows than the budgeted window can fetch. Page 1 is re-requested every
  tick forever, older history is never reached, and there is no log and
  no raise.

The same trap exists for any coarse bucket key: a timestamp truncated to
the second, a day bucket, a group id. The only fix is to persist WHERE
INSIDE the bucket the walk stopped (a within-window page number, or the
count of boundary-bucket rows already ingested) next to the cursor.

So the gate: if a module assigns a persisted ``*_cursor`` /
``*_watermark`` / ``*_checkpoint`` field from ``min(...)`` or ``max(...)``:
i.e. derives the resume position from a page's own values, then every
dataclass declaring that field must ALSO declare an intra-bucket
position field. No aggregate-derived cursor, no requirement: this gate is
silent on cursors that are per-row sequences (``last_modified_seq`` in
the ledger backends), which are already at pagination granularity.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "auradefi"

# A resume position that is persisted and re-read on the next call.
CURSOR_NAME = re.compile(r"(cursor|watermark|checkpoint|high_water)$")
# A position INSIDE one bucket: which page of the window, or how many of
# the boundary bucket's rows are already done.
INTRA_BUCKET_NAME = re.compile(
    r"(^|_)(page|pages|offset|index|position|"
    r"rows_(done|seen|ingested|consumed))(_|$)"
)
AGGREGATES = frozenset({"min", "max"})


def _modules(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _derives_from_aggregate(value: ast.expr) -> bool:
    """True if ``min(...)``/``max(...)`` appears anywhere in the expression.

    The whole subtree, not just the top call: ``max(blocks) if blocks
    else 0`` and ``min(blocks) - 1`` are the same derivation wearing a
    hat.
    """
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in AGGREGATES
        for node in ast.walk(value)
    )


def _aggregate_derived_cursors(root: Path) -> dict[str, list[str]]:
    """``{cursor field name: ["<file>:<line>", ...]}`` for ``= min/max(...)``.

    Only ATTRIBUTE targets count (``obj.foo_cursor = max(...)``): a bare
    local is scratch, not persisted state.
    """
    hits: dict[str, list[str]] = {}
    for path in _modules(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
                if isinstance(node, ast.AnnAssign)
                else []
            )
            if not targets:
                continue
            value = node.value
            if value is None or not _derives_from_aggregate(value):
                continue
            for target in targets:
                if not isinstance(target, ast.Attribute):
                    continue
                if not CURSOR_NAME.search(target.attr):
                    continue
                where = f"{path.relative_to(REPO)}:{node.lineno}"
                hits.setdefault(target.attr, []).append(where)
    return hits


def _classes_declaring(root: Path, field: str) -> list[tuple[str, list[str]]]:
    """``[(\"<file>:<line> <ClassName>\", [field names]), ...]`` declaring ``field``."""
    found: list[tuple[str, list[str]]] = []
    for path in _modules(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            fields = [
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
            ]
            if field in fields:
                where = f"{path.relative_to(REPO)}:{node.lineno} {node.name}"
                found.append((where, fields))
    return found


def offenders(root: Path) -> list[str]:
    """Every aggregate-derived cursor whose state carries no intra-bucket position."""
    problems: list[str] = []
    for field, sites in sorted(_aggregate_derived_cursors(root).items()):
        for where, fields in _classes_declaring(root, field):
            if any(INTRA_BUCKET_NAME.search(name) for name in fields):
                continue
            problems.append(
                f"{where}: {field!r} is derived from a page aggregate at "
                f"{', '.join(sites)} but the state declares no "
                f"within-bucket position (fields: {', '.join(fields)}): "
                f"the walk cannot resume inside a bucket, so one oversized "
                f"bucket either loses rows or never advances"
            )
    return problems


def test_aggregate_derived_cursor_has_an_intra_bucket_position():
    problems = offenders(SRC)
    assert not problems, (
        "resume cursor coarser than the pagination unit:\n" + "\n".join(problems)
    )
