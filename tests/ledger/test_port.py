"""LedgerPort Protocol contract (SPEC §6.4; rules #6 and #12).

The port is structural: a backend satisfies it by shape alone, checked at
runtime via ``isinstance``. These tests pin the method set, the
signatures (names, defaults), tenant-first scoping, and the documented
sync semantics.
"""

from __future__ import annotations

import inspect
from typing import Protocol, runtime_checkable

import pytest

from auradefi.ledger.port import LedgerPort

METHOD_NAMES = ("upsert", "sync", "get", "mark_removed")


class _CompleteBackend:
    """Structurally complete, never inherits from LedgerPort (rule #12)."""

    def upsert(self, tenant_id, txns):
        raise NotImplementedError

    def sync(self, tenant_id, cursor=None, limit=100):
        raise NotImplementedError

    def get(self, tenant_id, txn_id):
        raise NotImplementedError

    def mark_removed(self, tenant_id, txn_ids):
        raise NotImplementedError


class _MissingMarkRemoved:
    """Deliberately incomplete: no mark_removed."""

    def upsert(self, tenant_id, txns):
        raise NotImplementedError

    def sync(self, tenant_id, cursor=None, limit=100):
        raise NotImplementedError

    def get(self, tenant_id, txn_id):
        raise NotImplementedError


class TestRuntimeCheckable:
    def test_complete_backend_passes_isinstance_without_inheritance(self):
        backend = _CompleteBackend()
        assert isinstance(backend, LedgerPort)
        # Structural, not nominal: the fake never inherits from the port.
        assert LedgerPort not in type(backend).__mro__

    def test_incomplete_backend_fails_isinstance(self):
        assert not isinstance(_MissingMarkRemoved(), LedgerPort)

    def test_unrelated_object_fails_isinstance(self):
        assert not isinstance(object(), LedgerPort)

    def test_is_a_protocol_and_not_instantiable(self):
        assert issubclass(LedgerPort, Protocol)
        with pytest.raises(TypeError):
            LedgerPort()

    def test_runtime_checkable_marker_is_set(self):
        # isinstance() against a non-runtime_checkable Protocol raises
        # TypeError; reaching here without one proves the decorator, but
        # assert the flag explicitly for a readable failure.
        assert getattr(LedgerPort, "_is_runtime_protocol", False) is True


class TestSignatures:
    def test_exact_method_set(self):
        for name in METHOD_NAMES:
            assert callable(getattr(LedgerPort, name)), name

    @pytest.mark.parametrize("name", METHOD_NAMES)
    def test_tenant_id_is_the_first_parameter_everywhere(self, name):
        # Rule #6: every method tenant-scoped, tenant first.
        params = list(inspect.signature(getattr(LedgerPort, name)).parameters)
        assert params[0] == "self"
        assert params[1] == "tenant_id", f"{name} must take tenant_id first"

    def test_upsert_signature(self):
        params = inspect.signature(LedgerPort.upsert).parameters
        assert list(params) == ["self", "tenant_id", "txns"]

    def test_sync_signature_and_defaults(self):
        params = inspect.signature(LedgerPort.sync).parameters
        assert list(params) == ["self", "tenant_id", "cursor", "limit"]
        assert params["cursor"].default is None
        assert params["limit"].default == 100

    def test_get_signature(self):
        params = inspect.signature(LedgerPort.get).parameters
        assert list(params) == ["self", "tenant_id", "txn_id"]

    def test_mark_removed_signature(self):
        params = inspect.signature(LedgerPort.mark_removed).parameters
        assert list(params) == ["self", "tenant_id", "txn_ids"]


class TestDocumentedContract:
    """The docstrings ARE the contract for embedding hosts (rule #12)."""

    @pytest.mark.parametrize("name", METHOD_NAMES)
    def test_every_method_documents_tenant_scoping(self, name):
        doc = inspect.getdoc(getattr(LedgerPort, name)) or ""
        assert "tenant" in doc.lower(), f"{name} docstring must state tenant scoping"

    def test_sync_documents_last_modified_ordering_not_tx_date(self):
        doc = inspect.getdoc(LedgerPort.sync) or ""
        assert "last-modified" in doc.lower()
        assert "not transaction date" in doc.lower()

    def test_sync_documents_paging_until_has_more_false(self):
        doc = inspect.getdoc(LedgerPort.sync) or ""
        assert "has_more" in doc
        assert "false" in doc.lower()

    def test_event_returning_methods_document_ordering(self):
        for name in ("upsert", "mark_removed"):
            doc = inspect.getdoc(getattr(LedgerPort, name)) or ""
            assert "ascending last-modified seq" in doc.lower(), name
