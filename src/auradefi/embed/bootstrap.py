"""Default port sets, so "bring your own" is a choice and not an entry fee.

``Auradefi`` takes every collaborator as a port, which is the right design
and was, on its own, a bad first experience: because nothing shipped a
default, the shortest working program began with a source adapter, a price
adapter and a store: about forty lines before the first number. Ports are
for the hosts that need them, not a toll on the ones that don't.

This module builds the two default sets:

* :func:`sandbox_ports`: the bundled recording. No keys, no network, no
  configuration; the numbers are constants because they are a recording.
* :func:`env_ports`. Live infrastructure from ``Settings``: Etherscan V2
  for chain data, DefiLlama for prices, in-memory storage.

Both return a plain kwargs mapping for ``Auradefi(...)``, which is what
lets a caller override exactly one port and keep the rest. The documented
"bring your own database" path is ``from_env(ledger=MyLedger())``, and it
is one dict update rather than a special case.

Split out of ``facade.py`` because that file has 26 lines left before the
400-line hard cap and this wiring, documented to house standard, does not
fit in 26 lines. It lives under ``embed/`` so it may compose ``sources``,
``prices`` and ``ledger``; it must never import an HTTP client itself (the
layer contract forbids it here, and client construction belongs to the
source that owns the credential).

**Storage defaults to memory, deliberately.** ``MemoryLedger`` is not
durable and this module will not quietly pick a database for you: the
SQLModel backend takes a session factory rather than a URL because the HOST
owns the engine, the connection pool and the migrations
(``ledger/backends/sqlmodel.py``). Wiring that is three lines the host must
see, so it is documented on the "bring your own" page and passed in as
``ledger=``, never guessed at from an environment variable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from auradefi.clock import FrozenClock
from auradefi.config import Settings
from auradefi.embed.state import MemorySyncState
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.prices.inquirer import Inquirer
from auradefi.prices.oracles.defillama import DefiLlamaOracle
from auradefi.sources import sandbox as sandbox_transport
from auradefi.sources.evm.source import EtherscanSource

if TYPE_CHECKING:  # pragma: no cover - typing only
    from auradefi.embed.facade import Auradefi


def open_sandbox(
    facade: type[Auradefi],
    *,
    connect: bool = True,
    overrides: dict[str, Any] | None = None,
) -> Auradefi:
    """Construct a sandbox facade, optionally pre-connected.

    ``Auradefi.sandbox`` delegates here rather than doing the work itself:
    the facade module has no line budget left, and the connect step needs
    the recording's own constants, which live beside the recording.

    Pre-connecting is what makes the five-line quickstart honest.
    ``holdings()`` over zero connections is an empty tuple, and a first
    example that indexes into it would fail. The connect issues the same
    one-row liveness probe a live connect does, which the recording holds.
    """
    instance = facade(**{**sandbox_ports(), **(overrides or {})})
    if connect:
        instance.user(sandbox_transport.SANDBOX_USER).connect_address(
            sandbox_transport.SANDBOX_CHAIN, sandbox_transport.SANDBOX_ADDRESS
        )
    return instance


def sandbox_ports() -> dict[str, Any]:
    """Ports wired onto the bundled recording (no keys, no network).

    One replay client serves both hosts the recording covers, Etherscan
    for chain data, DefiLlama for prices, exactly as a host would share
    one client between them. The clock is frozen at the instant the
    traffic was recorded, so every derived value (``as_of_ms``, the sync
    throttle, ids hashed over time) is reproducible run to run.

    ``sync_page_size`` matches the recorded page size; a different value
    would ask for a window the recording does not hold and raise
    ``CassetteMissError``.
    """
    page_size = sandbox_transport.SANDBOX_PAGE_SIZE
    source = EtherscanSource(sandbox_transport.client(), page_size=page_size)
    return {
        "ledger": MemoryLedger(),
        "source": source,
        "prices": Inquirer([DefiLlamaOracle(source.client)]),
        "clock": FrozenClock(sandbox_transport.SANDBOX_NOW_MS),
        "settings": Settings(sync_min_interval_s=60),
        "sync_state": MemorySyncState(),
        "sync_page_size": page_size,
    }


def env_ports(settings: Settings | None = None) -> dict[str, Any]:
    """Ports wired onto live infrastructure from ``Settings``.

    ``settings=None`` reads the environment (``Settings.from_env``), which
    is where ``AURADEFI_ETHERSCAN_API_KEY`` and ``AURADEFI_HTTP_TIMEOUT_S``
    are picked up. The key is OPTIONAL: without one, Etherscan's keyless
    tier applies and the ``apikey`` param is omitted entirely.

    Storage is in-memory and not durable. See this module's docstring for
    why a database URL is not read here. Prices come from DefiLlama, which
    is keyless and covers six EVM chains; Bitcoin and Solana have no price
    source in this package at all, so those assets come back unpriced
    rather than guessed at.
    """
    resolved = Settings.from_env() if settings is None else settings
    source = EtherscanSource.from_key(
        resolved.etherscan_api_key, timeout_s=resolved.http_timeout_s
    )
    # ONE client for chain data and prices, shared the way a host should
    # wire it: one connection pool, one timeout, one place to add a proxy.
    return {
        "ledger": MemoryLedger(),
        "source": source,
        "prices": Inquirer([DefiLlamaOracle(source.client)]),
        "settings": resolved,
        "sync_state": MemorySyncState(),
    }
