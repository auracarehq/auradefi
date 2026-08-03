"""SPEC §3.2: tests mirror source exactly; table definitions only in a
models.py.

Mirror rule, both directions:
  src/auradefi/<pkg...>/<m>.py   <->  tests/<pkg...>/test_<m>.py
  src/auradefi/<m>.py            <->  tests/test_<m>.py        (foundation)

Exempt from mirroring: tests/style/**, tests/golden/**, tests/cassettes/**,
tests/contract/**, and every conftest.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "auradefi"
TESTS = REPO / "tests"
EXEMPT_TOP_DIRS = {"style", "golden", "cassettes", "contract"}


def _source_modules() -> list[Path]:
    return [
        path
        for path in sorted(SRC.rglob("*.py"))
        if path.name != "__init__.py"
    ]


def _mirror_of(source: Path) -> Path:
    relative = source.relative_to(SRC)
    return TESTS / relative.parent / f"test_{relative.name}"


def _mirrored_test_files() -> list[Path]:
    files = []
    for path in sorted(TESTS.rglob("test_*.py")):
        relative = path.relative_to(TESTS)
        if relative.parts[0] in EXEMPT_TOP_DIRS:
            continue
        files.append(path)
    return files


def test_every_source_module_has_a_mirrored_test_file():
    missing = [
        f"{source.relative_to(REPO)}  →  {_mirror_of(source).relative_to(REPO)}"
        for source in _source_modules()
        if not _mirror_of(source).exists()
    ]
    assert not missing, "source modules without mirrored tests:\n" + "\n".join(missing)


def test_every_mirrored_test_file_has_a_source_module():
    orphans = []
    for test_file in _mirrored_test_files():
        relative = test_file.relative_to(TESTS)
        source = SRC / relative.parent / relative.name.removeprefix("test_")
        if not source.exists():
            orphans.append(str(test_file.relative_to(REPO)))
    assert not orphans, (
        "test files with no mirrored source module (move under tests/contract/ "
        "or tests/golden/ if deliberately unmirrored):\n" + "\n".join(orphans)
    )


def test_table_definitions_only_in_models_py():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "models.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                has_table_kwarg = any(
                    keyword.arg == "table"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                )
                base_names = {
                    base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                    for base in node.bases
                }
                if has_table_kwarg or base_names & {"DeclarativeBase", "Table"}:
                    offenders.append(
                        f"{path.relative_to(REPO)}:{node.lineno} {node.name}"
                    )
    assert not offenders, (
        "table definitions belong in a models.py:\n" + "\n".join(offenders)
    )
