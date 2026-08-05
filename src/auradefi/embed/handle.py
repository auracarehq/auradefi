"""``UserHandle``: one host user's tenant-scoped slice of the library.

Split out of ``facade.py`` when that module reached the 400-line hard cap
(the house rule is to split rather than to compress the reasoning out).
The two classes were always separable: ``Auradefi`` binds ports and answers
across every connection a store holds, while this class narrows all of it
to ONE derived tenant: the scope rule of rule #6, expressed as an object
so a caller cannot forget to pass a tenant id.

It reaches into the facade's internals on purpose. ``UserHandle`` owns no
ports and no state beyond the derived ``tenant_id``: every collaborator is
the facade's, so a handle can never diverge from the instance that made it
or outlive its configuration. ``Auradefi.user`` is the only constructor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from auradefi.chains.evm import chain_id_from_caip2, normalize_address
from auradefi.embed.dispatch import run_sync
from auradefi.embed.models import ConnectionRecord, SyncReport, derive_connection_id
from auradefi.errors import ConflictError
from auradefi.portfolio.models import HoldingsReport
from auradefi.project import scalar as scalar_projection

if TYPE_CHECKING:  # pragma: no cover - typing only; embed may import itself
    from auradefi.embed.facade import Auradefi


class UserHandle:
    """One host user's slice of the library (SPEC §7.1, §8).

    Created by :meth:`Auradefi.user`; ``tenant_id`` is derived, never
    supplied. Every operation is scoped to that tenant (rule #6).
    """

    def __init__(
        self, facade: Auradefi, external_user_id: str, tenant_id: str
    ) -> None:
        """Bind the facade and the derived tenant id. No I/O."""
        self._facade = facade
        self.external_user_id = external_user_id
        self.tenant_id = tenant_id

    def connect_address(self, chain: str, address: str) -> ConnectionRecord:
        """Watch one address on one chain: validated NOW, not later.

        In order, and each step before any HTTP can happen:

        1. ``chains.evm.chain_id_from_caip2(chain)``: a vendor name like
           ``"ethereum"`` raises ``auradefi.errors.CaipParseError``.
        2. ChainRegistry MEMBERSHIP: a well-formed CAIP-2 the registry
           does not hold raises ``auradefi.errors.UnknownChainError``
           naming it. The decoder needs that entry, so accepting the
           chain stores a connection every ``sync()`` fails on (§5 #24).
        3. ``chains.evm.normalize_address(address)``. A non-address
           raises ``auradefi.errors.ValidationError``. The normalized
           (lowercased) address is what gets stored.
        4. The connection id is DERIVED
           (``embed.models.derive_connection_id``) and CHAIN-SCOPED, so a
           re-connect of the same (chain, address), in any letter case in
           the 40 hex DIGITS, raises ``auradefi.errors.ConflictError``
           carrying ``existing_id`` with ZERO requests made, while the
           SAME address on ANOTHER chain is a second, independent
           connection (§5 #26). The ``0x`` prefix is NOT
           case-insensitive: ``0X…`` never reaches here, step 3 rejects it.
        5. A liveness probe: EXACTLY one request. Its
           ``auradefi.errors.SourceError`` propagates and NOTHING is
           stored; an EMPTY result is a valid fresh address.
        6. The ``ConnectionRecord`` is stored with
           ``created_at_ms = clock.now_ms()`` and returned.
        """
        facade = self._facade
        chain_id_from_caip2(chain)
        facade._chains.get(chain)
        normalized = normalize_address(address)
        connection_id = derive_connection_id(self.tenant_id, normalized, chain)
        for existing in self.connections():
            if existing.id == connection_id:
                raise ConflictError(
                    f"connection already exists: {connection_id!r}",
                    existing_id=connection_id,
                )
        facade._probe(chain, normalized)
        record = ConnectionRecord(
            id=connection_id,
            chain_id=chain,
            address=normalized,
            created_at_ms=facade._clock.now_ms(),
        )
        facade._sync_state.add_connection(self.tenant_id, record)
        return record

    def connections(self) -> tuple[ConnectionRecord, ...]:
        """This tenant's connections, in creation order."""
        return self._facade._sync_state.connections(self.tenant_id)

    def _pairs(self) -> list[tuple[str, ConnectionRecord]]:
        """This user's ``(tenant_id, connection)`` pairs, creation order."""
        return [(self.tenant_id, connection) for connection in self.connections()]

    def sync(self, budget: int = 5) -> SyncReport:
        """:meth:`Auradefi.sync` restricted to THIS user's connections."""
        return run_sync(
            self._facade._engine,
            self._facade._sync_state,
            self._pairs(),
            budget,
        )

    def holdings(self) -> tuple[HoldingsReport, ...]:
        """:meth:`Auradefi.holdings` for THIS user's connections."""
        return self._facade._reports(self._pairs())

    def scalar_metrics(self) -> tuple[scalar_projection.Metric, ...]:
        """:meth:`Auradefi.scalar_metrics` for THIS user's connections."""
        return self._facade._metrics(self._pairs())
