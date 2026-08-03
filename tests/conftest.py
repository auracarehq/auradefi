"""Root fixtures: offline guard, frozen clock, cassette loader.

The suite must pass on a fresh clone with no API keys (SPEC §13), so the
offline guarantee is enforced here as a hard failure on any socket connect,
not left to timeouts.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
CASSETTE_DIR = REPO_ROOT / "tests" / "cassettes"

# A fresh clone without an installed package still runs: src layout on path.
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _blocked(self, *args, **kwargs):
        raise RuntimeError(
            "network access attempted during tests — the suite must run offline "
            "with no API keys (SPEC §13); record a cassette instead"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)


@pytest.fixture
def frozen_clock():
    from auradefi.clock import FrozenClock

    return FrozenClock(1_754_000_000_000)


@pytest.fixture
def cassette():
    from auradefi.testing.cassettes import load

    def _load(name: str):
        return load(CASSETTE_DIR / f"{name}.json")

    return _load
