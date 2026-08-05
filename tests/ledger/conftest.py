"""Shared ledger test factories (reused by the wave-3 ledger-engine order).

``make_entry`` and ``make_txn`` are factory fixtures: each yields a callable
whose keyword overrides replace the defaults below. Tests request the
fixture, never import from another test module.

Defaults match the pinned golden vectors in ``test_models.py``: the id is
``transaction_id("eip155:1", "0xabc", "acct_1")`` derived independently via
``python3 -c``, and timestamps sit in the repo's frozen-clock era.
"""

from __future__ import annotations

import pytest

from auradefi.ledger.models import Direction, Entry, LedgerTransaction
from auradefi.money.quantity import Quantity

#: ms-epoch, matches the repo's frozen clock era.
MS = 1_754_000_000_000

#: eip155:1 | 0xabc | acct_1: derived independently; NEVER regenerate
#: from the implementation.
DEFAULT_TXN_ID = "txn_8960436486a11960"


@pytest.fixture
def make_entry():
    """Factory for a valid ``Entry``; keyword overrides replace defaults."""

    def _make_entry(**overrides) -> Entry:
        fields = {
            "asset_id": "eip155:1/slip44:60",
            "quantity": Quantity(15 * 10**17, 18),
            "direction": Direction.IN,
        }
        fields.update(overrides)
        return Entry(**fields)

    return _make_entry


@pytest.fixture
def make_txn(make_entry):
    """Factory for a valid ``LedgerTransaction``; overrides replace defaults.

    The default id matches the default (chain_id, tx_hash, account_id)
    triple, so identity-sensitive tests stay coherent unless a test
    overrides them together.
    """

    def _make_txn(**overrides) -> LedgerTransaction:
        fields = {
            "id": DEFAULT_TXN_ID,
            "chain_id": "eip155:1",
            "tx_hash": "0xabc",
            "account_id": "acct_1",
            "block_number": 19_000_000,
            "initiated_at": MS,
            "confirmed_at": MS + 12_000,
            "entries": (make_entry(),),
        }
        fields.update(overrides)
        return LedgerTransaction(**fields)

    return _make_txn
