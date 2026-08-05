"""How do I run this inside my own backend, with my own database?

    pip install auradefi
    python examples/02_embed_in_your_backend.py

`Auradefi.from_env()` gives you working defaults; every collaborator is
still a port you can replace one at a time. That is the whole shape:

    aura = Auradefi.from_env()                      # defaults
    aura = Auradefi.from_env(ledger=MyLedger())     # your database
    aura = Auradefi.from_env(prices=MyPrices())     # your price feed
    aura = Auradefi(ledger=…, source=…, prices=…)   # nothing of ours

This file runs in Sandbox so it needs no keys, and demonstrates the five
things a host actually has to get right:

1. **validation at CONNECT time**, not on a background tick hours later;
2. a **budgeted** sync you call on your own schedule, and its throttle;
3. a **restart**: a new process over the same stores resumes stored work;
4. one dead chain **failing on its own row** instead of failing the tick;
5. **your database**: the three lines, and why it is not an env var.
"""

from __future__ import annotations

from auradefi import Auradefi
from auradefi.clock import FrozenClock
from auradefi.embed import bootstrap
from auradefi.errors import SourceError, UnknownChainError, ValidationError
from auradefi.sources import sandbox as recording

# The default port set, as a plain dict. This is what `sandbox()` and
# `from_env()` hand to the constructor, and what makes an override a
# one-keyword change rather than a fork of the wiring.
clock = FrozenClock(recording.SANDBOX_NOW_MS)
ports = {**bootstrap.sandbox_ports(), "clock": clock}
aura = Auradefi(**ports)
print("ports:", ", ".join(sorted(ports)))

# ------------------------------------------- 1. validate at connect time
user = aura.user("your-opaque-user-id")
connection = user.connect_address(recording.SANDBOX_CHAIN, recording.SANDBOX_ADDRESS)
print(f"\nconnected {connection.id}")

for chain, address, expected in (
    ("eip155:99999", recording.SANDBOX_ADDRESS, UnknownChainError),
    (recording.SANDBOX_CHAIN, "0xnope", ValidationError),
    (recording.SANDBOX_CHAIN, recording.SANDBOX_ADDRESS, Exception),  # duplicate
):
    try:
        user.connect_address(chain, address)
    except expected as exc:
        print(f"  refused now, not later: {type(exc).__name__}: {str(exc)[:58]}")

# --------------------------------------------------- 2. sync on your tick
# `budget` is the maximum source pages one call may spend. The cursor makes
# the next call resume, so a tick is bounded and never loses its place.
first = aura.sync(budget=2)
print(f"\nsync(budget=2): {first.pages_fetched} pages, "
      f"{first.transactions_ingested} transactions, no_op={first.no_op}")
print(f"  same instant again: no_op={aura.sync(budget=2).no_op} "
      "(throttled by settings.sync_min_interval_s: zero requests)")

# ----------------------------------------------------- 3. restart resume
# A new process binds fresh objects over the SAME stores. Connections come
# from the state port, not from process memory, so a restarted worker
# resumes stored work instead of reporting a cheerful no-op forever.
clock.advance(60_000)
restarted = Auradefi(**ports)
resumed = restarted.sync(budget=2)
assert [row.connection_id for row in resumed.connections] == [connection.id]
print(f"\nafter restart: enumerated {len(resumed.connections)} stored connection(s), "
      f"ingested {resumed.transactions_ingested} more")

# --------------------------------------------- 4. one failure, one row
class DeadChain:
    """Your source, but the RPC is down. Wrap, do not rewrite."""

    def __init__(self, real: object) -> None:
        self._real = real

    def balances(self, chain_id: str, address: str):
        return self._real.balances(chain_id, address)

    def fetch_txlist(self, chain_id, address, **window):
        # Raise SourceError (any AuradefiError) for an upstream failure and
        # the library contains it to this ONE connection. Anything else, 
        # a KeyError in your adapter. Propagates, because that is your bug.
        raise SourceError(f"{chain_id} RPC did not answer")


clock.advance(60_000)
degraded = Auradefi(**{**ports, "source": DeadChain(ports["source"])}).sync(budget=2)
assert degraded.failed_connections == (connection.id,)
print(f"\nRPC down: failed_connections={degraded.failed_connections}")
print("  branch on report.failed_connections every tick: a partial failure")
print("  can never hide behind an aggregate that reads like a clean one")

# ------------------------------------------------------ 5. your database
# Storage defaults to memory and says so. There is no AURADEFI_DATABASE_URL
# on purpose: the SQL ledger takes a SESSION FACTORY because your
# application owns the engine, the pool and the migrations: auradefi never
# opens a connection you did not hand it, and never emits DDL.
#
#     from sqlalchemy import create_engine
#     from sqlmodel import Session
#     from auradefi.ledger.backends.models import metadata
#     from auradefi.ledger.backends.sqlmodel import SqlModelLedger
#
#     engine = create_engine("postgresql+psycopg://user@host/db")
#     metadata.create_all(engine)            # your migration, run once
#     aura = Auradefi.from_env(
#         ledger=SqlModelLedger(session_factory=lambda: Session(engine)))
#
# See 04_persist_to_your_database.py for that running end to end, and
# 03_write_a_source_adapter.py to replace the transport instead.
print("\nOK: defaults to start with, ports to replace, your tick throughout.")
