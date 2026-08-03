"""Foundation: the exception taxonomy and its single-module invariant."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from auradefi import errors
from auradefi.errors import (
    AuradefiError,
    AuthError,
    CassetteError,
    CassetteMissError,
    ConflictError,
    CursorError,
    LedgerError,
    ScopeError,
    TenantIsolationError,
    TokenExpiredError,
    TokenRevokedError,
)

SRC = Path(__file__).resolve().parent.parent / "src" / "auradefi"


def _exception_classes() -> list[type[BaseException]]:
    return [
        obj
        for _, obj in inspect.getmembers(errors, inspect.isclass)
        if issubclass(obj, BaseException) and obj.__module__ == "auradefi.errors"
    ]


def test_every_error_derives_from_auradefi_error():
    classes = _exception_classes()
    assert classes, "taxonomy is not empty"
    for cls in classes:
        assert issubclass(cls, AuradefiError), cls.__name__


def test_hierarchy_relationships():
    assert issubclass(CassetteMissError, CassetteError)
    assert issubclass(CursorError, LedgerError)
    assert issubclass(TenantIsolationError, LedgerError)
    assert issubclass(TokenExpiredError, AuthError)
    assert issubclass(TokenRevokedError, AuthError)
    assert issubclass(ScopeError, AuthError)


def test_conflict_error_carries_existing_id():
    err = ConflictError("already connected", existing_id="conn_123")
    assert err.existing_id == "conn_123"
    assert ConflictError("no id").existing_id is None


def test_no_exception_classes_defined_outside_errors_py():
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name == "errors.py" and path.parent == SRC:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                base_names = {
                    base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                    for base in node.bases
                }
                if any(name.endswith(("Error", "Exception")) for name in base_names):
                    offenders.append(f"{path.relative_to(SRC.parent)}:{node.lineno} {node.name}")
    assert not offenders, (
        "exception classes must live in auradefi/errors.py only:\n" + "\n".join(offenders)
    )


def test_catching_the_base_class_catches_everything():
    for cls in _exception_classes():
        if cls is ConflictError:
            instance = cls("boom", existing_id=None)
        else:
            instance = cls("boom")
        with pytest.raises(AuradefiError):
            raise instance
