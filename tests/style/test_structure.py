"""SPEC §3.2: foundation modules asserted with ==; every domain is a
package; no directory holds more than 10 non-__init__ modules."""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "auradefi"
FOUNDATION = {"__init__.py", "clock.py", "config.py", "errors.py"}
MAX_DIR_FILES = 10


def _package_dirs() -> list[Path]:
    return [SRC] + [
        path for path in sorted(SRC.rglob("*")) if path.is_dir() and path.name != "__pycache__"
    ]


def test_flat_foundation_is_exactly_init_clock_config_errors():
    flat = {path.name for path in SRC.glob("*.py")}
    assert flat == FOUNDATION, (
        "only the foundation may be flat under src/auradefi/ — "
        f"unexpected: {sorted(flat - FOUNDATION)}, missing: {sorted(FOUNDATION - flat)}"
    )


def test_every_domain_directory_is_a_package():
    missing = [
        str(path.relative_to(SRC.parent))
        for path in _package_dirs()
        if path != SRC and not (path / "__init__.py").exists()
    ]
    assert not missing, "directories without __init__.py:\n" + "\n".join(missing)


def test_no_directory_exceeds_the_module_cap():
    offenders = []
    for directory in _package_dirs():
        modules = [p for p in directory.glob("*.py") if p.name != "__init__.py"]
        if len(modules) > MAX_DIR_FILES:
            offenders.append(
                f"{directory.relative_to(SRC.parent)}: {len(modules)} modules "
                f"(cap {MAX_DIR_FILES} — grow a subfolder)"
            )
    assert not offenders, "\n".join(offenders)


def test_domain_init_files_are_docstring_only():
    offenders = []
    for directory in _package_dirs():
        if directory == SRC:
            continue
        init = directory / "__init__.py"
        if not init.exists():
            continue
        import ast

        body = ast.parse(init.read_text(encoding="utf-8")).body
        real = [
            node
            for node in body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        ]
        if real:
            offenders.append(str(init.relative_to(SRC.parent)))
    assert not offenders, (
        "package __init__.py files are docstring-only (no re-exports — the "
        "concurrency and import-poisoning rule):\n" + "\n".join(offenders)
    )
