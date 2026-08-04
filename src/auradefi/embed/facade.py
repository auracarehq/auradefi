"""The embedding entry point: ``from auradefi import Auradefi`` (SPEC §8).

Import, don't call. A host with its own Python backend adopts auradefi as
a library — no HTTP hop, no serialisation, no separate service:

.. code-block:: python

    auradefi = Auradefi(ledger=SqlModelLedger(host_session_factory),
                        source=host_source, prices=host_prices)
    user = auradefi.user("opaque-host-user-id")   # get-or-create
    conn = user.connect_address("eip155:1", "0x…")
    report = auradefi.sync()                      # budgeted, resumable

Everything the host owns is a port: storage (``ledger``), sync-state
(``sync_state``), transport (``source``), prices, time (``clock``) and
the row format (``decoder``). We never open a connection the host did
not hand us.

Validate at CONNECT time, not at sync time (SPEC §8): a bad chain or
address is rejected before any HTTP, a duplicate before any HTTP, and a
live source failure surfaces from ``connect_address`` — a connector that
accepts anything and fails silently on a background tick hours later is
the worst failure mode for an embedding host.

Phase 5 is single-tenant and ingests the NATIVE txlist stream only; the
tenant id derives deterministically from the host's opaque
``external_user_id`` (SPEC §7.1 get-or-create) and tokentx rides in later
behind the same decoder seam. No web framework, no ORM, no ``httpx``
anywhere in this module — the layering gate enforces it.
"""

from __future__ import annotations

from collections.abc import Sequence

from auradefi.chains.evm import chain_id_from_caip2, normalize_address
from auradefi.clock import Clock, SystemClock
from auradefi.config import Settings
from auradefi.embed.models import (
    ConnectionRecord,
    ConnectionSyncReport,
    SyncReport,
    derive_connection_id,
    derive_tenant_id,
)
from auradefi.embed.state import MemorySyncState, SyncStatePort
from auradefi.embed.sync import HEAD_BLOCK, Decoder, PageFetcher, SyncEngine
from auradefi.errors import ConflictError, ValidationError
from auradefi.ledger.models import LedgerTransaction, SyncEventKind
from auradefi.ledger.port import LedgerPort
from auradefi.portfolio.holdings import BalanceSource, HoldingsService
from auradefi.portfolio.models import HoldingsReport
from auradefi.prices.inquirer import PriceOracle
from auradefi.project import scalar as scalar_projection


class Auradefi:
    """The library's public surface (SPEC §8).

    ``source`` must structurally satisfy BOTH
    ``portfolio.holdings.BalanceSource`` (``balances``, for holdings) and
    ``embed.sync.PageFetcher`` (``fetch_txlist``, for history) — one
    object, two seams, so a host writes one adapter.
    """

    def __init__(
        self,
        ledger: LedgerPort,
        source: BalanceSource | PageFetcher,
        prices: PriceOracle,
        clock: Clock | None = None,
        settings: Settings | None = None,
        *,
        sync_state: SyncStatePort | None = None,
        decoder: Decoder | None = None,
        sync_page_size: int = 1000,
    ) -> None:
        """Bind the host's ports. ZERO I/O happens here.

        ``clock=None`` means ``SystemClock()``, ``settings=None`` means
        ``Settings()``, ``sync_state=None`` means ``MemorySyncState()``
        (in-process; a host that wants durable cursors binds its own).

        ``decoder=None`` binds the default composition LAZILY, at first
        use — ``sources.evm.txlist.parse_normal_row`` per row ->
        ``decode.pipeline.decode_account`` -> ``ledger.bridge`` — so
        importing this module stays cheap and dependency-light.

        Raises ``auradefi.errors.ValidationError`` when ``source`` does
        not satisfy both seams: the failure belongs at bind time, not at
        the first background tick.
        """
        seams = ((BalanceSource, "balances"), (PageFetcher, "fetch_txlist"))
        for protocol, seam in seams:
            if not isinstance(source, protocol):
                raise ValidationError(
                    f"source must satisfy {protocol.__name__} — it has no "
                    f"{seam!r} method"
                )
        self._ledger = ledger
        self._source = source
        self._prices = prices
        self._clock = clock if clock is not None else SystemClock()
        self._settings = settings if settings is not None else Settings()
        self._sync_state = (
            sync_state if sync_state is not None else MemorySyncState()
        )
        self._tenants: list[str] = []
        self._engine = SyncEngine(
            ledger,
            self._sync_state,
            source,
            decoder if decoder is not None else self._decode_page,
            self._clock,
            self._settings.sync_min_interval_s * 1000,
            sync_page_size,
        )

    def user(self, external_user_id: str) -> UserHandle:
        """Get-or-create the handle for one opaque host user id.

        Pure: no I/O and no persistence — the tenant id is DERIVED from
        the id (``embed.models.derive_tenant_id``), so the same string
        always resolves to the same tenant. Raises
        ``auradefi.errors.ValidationError`` for anything outside the
        pinned opaque-id charset (an email is guessable and this value is
        bearer-equivalent, so ``@`` cannot appear).
        """
        tenant_id = derive_tenant_id(external_user_id)
        if tenant_id not in self._tenants:
            self._tenants.append(tenant_id)
        return UserHandle(self, external_user_id, tenant_id)

    def sync(self, budget: int = 5) -> SyncReport:
        """One tick across every known connection, in creation order.

        ONE shared budget of page requests is spent connection by
        connection until it runs out; connections beyond that point are
        not visited this tick. ``budget < 1`` raises
        ``auradefi.errors.ValidationError``. The aggregate sums the
        per-connection counts, and ``no_op`` is True exactly when every
        VISITED connection was a no-op — vacuously True with zero
        connections. Self-throttling comes from
        ``settings.sync_min_interval_s``.
        """
        return self._run_sync(self._pairs(), budget)

    def holdings(self) -> tuple[HoldingsReport, ...]:
        """One priced ``HoldingsReport`` per connection, creation order."""
        return self._reports(self._pairs())

    def scalar_metrics(self) -> tuple[scalar_projection.Metric, ...]:
        """``(name, ms, float)`` triples per connection, concatenated.

        Each connection contributes ``project.scalar.scalar_metrics``
        over its own holdings report and its own transactions — the
        non-removed ADDED-kind ledger rows whose ``account_id`` is that
        connection's id, read by paging ``ledger.sync`` until
        ``has_more`` is False.
        """
        return self._metrics(self._pairs())

    def _pairs(self) -> list[tuple[str, ConnectionRecord]]:
        """Every ``(tenant_id, connection)`` known, in creation order."""
        return [
            (tenant_id, connection)
            for tenant_id in self._tenants
            for connection in self._sync_state.connections(tenant_id)
        ]

    def _run_sync(
        self, pairs: Sequence[tuple[str, ConnectionRecord]], budget: int
    ) -> SyncReport:
        """Spend one shared budget over ``pairs``; the pinned algorithm.

        Connections are visited in order until the budget runs out; a
        throttled connection spends nothing, so the next one still gets
        the full remainder. Connections past the exhausted budget are
        not visited and contribute no row.
        """
        if budget < 1:
            raise ValidationError(f"budget must be >= 1, got {budget}")
        remaining = budget
        rows: list[ConnectionSyncReport] = []
        for tenant_id, connection in pairs:
            if remaining < 1:
                break
            row = self._engine.sync_connection(tenant_id, connection, remaining)
            remaining -= row.pages_fetched
            rows.append(row)
        return SyncReport.assemble(rows)

    def _reports(
        self, pairs: Sequence[tuple[str, ConnectionRecord]]
    ) -> tuple[HoldingsReport, ...]:
        """Holdings for ``pairs`` via one ``HoldingsService``, in order."""
        service = HoldingsService(self._source, self._prices, self._clock)
        return tuple(
            service.holdings(connection.chain_id, connection.address)
            for _, connection in pairs
        )

    def _metrics(
        self, pairs: Sequence[tuple[str, ConnectionRecord]]
    ) -> tuple[scalar_projection.Metric, ...]:
        """Scalar projection of each pair's report + own transactions."""
        metrics: list[scalar_projection.Metric] = []
        for (tenant_id, connection), report in zip(
            pairs, self._reports(pairs), strict=True
        ):
            metrics.extend(
                scalar_projection.scalar_metrics(
                    report, self._account_transactions(tenant_id, connection.id)
                )
            )
        return tuple(metrics)

    def _probe(self, chain_id: str, address: str) -> None:
        """The connect-time liveness probe: EXACTLY one cheap request.

        ``fetch_txlist(page=1, offset=1, sort='desc')`` — the smallest
        window the transport can answer. An empty result is a VALID fresh
        address; ``auradefi.errors.SourceError`` propagates untouched.
        """
        self._source.fetch_txlist(
            chain_id,
            address,
            start_block=0,
            end_block=HEAD_BLOCK,
            page=1,
            offset=1,
            sort="desc",
        )

    def _account_transactions(
        self, tenant_id: str, account_id: str
    ) -> tuple[LedgerTransaction, ...]:
        """This account's live rows, paging ``ledger.sync`` to the end."""
        found: list[LedgerTransaction] = []
        cursor: str | None = None
        while True:
            page = self._ledger.sync(tenant_id, cursor)
            found.extend(
                event.transaction
                for event in page.events
                if event.kind is SyncEventKind.ADDED
                and not event.transaction.removed
                and event.transaction.account_id == account_id
            )
            if not page.has_more:
                return tuple(found)
            cursor = page.next_cursor

    def _decode_page(
        self,
        chain_id: str,
        address: str,
        account_id: str,
        rows: Sequence[dict],
    ) -> list[LedgerTransaction]:
        """The default decoder, bound lazily (imports live INSIDE).

        Raw Etherscan txlist rows -> ``parse_normal_row`` each ->
        ``decode_account(..., tokens=())`` -> ``to_ledger_transaction``
        each. Phase 5 ingests the NATIVE stream only; a malformed row's
        ``auradefi.errors.SourceError`` propagates.
        """
        from auradefi.decode.pipeline import decode_account
        from auradefi.ledger.bridge import to_ledger_transaction
        from auradefi.sources.evm.txlist import parse_normal_row

        records = [parse_normal_row(row) for row in rows]
        return [
            to_ledger_transaction(rich)
            for rich in decode_account(
                chain_id,
                account_id=account_id,
                address=address,
                normal=records,
                tokens=(),
            )
        ]


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
        """Watch one address on one chain — validated NOW, not later.

        In order, and each step before any HTTP can happen:

        1. ``chains.evm.chain_id_from_caip2(chain)`` — a vendor name like
           ``"ethereum"`` raises ``auradefi.errors.CaipParseError``; then
           ``chains.evm.normalize_address(address)`` — a non-address
           raises ``auradefi.errors.ValidationError``. The normalized
           (lowercased) address is what gets stored.
        2. The connection id is DERIVED
           (``embed.models.derive_connection_id``), so a re-connect of
           the same (chain, address) — in any letter case — raises
           ``auradefi.errors.ConflictError`` carrying ``existing_id``
           with ZERO requests made.
        3. A liveness probe: EXACTLY one request. Its
           ``auradefi.errors.SourceError`` propagates and NOTHING is
           stored; an EMPTY result is a valid fresh address.
        4. The ``ConnectionRecord`` is stored with
           ``created_at_ms = clock.now_ms()`` and returned.
        """
        facade = self._facade
        chain_id_from_caip2(chain)
        normalized = normalize_address(address)
        connection_id = derive_connection_id(self.tenant_id, normalized)
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
        return self._facade._run_sync(self._pairs(), budget)

    def holdings(self) -> tuple[HoldingsReport, ...]:
        """:meth:`Auradefi.holdings` for THIS user's connections."""
        return self._facade._reports(self._pairs())

    def scalar_metrics(self) -> tuple[scalar_projection.Metric, ...]:
        """:meth:`Auradefi.scalar_metrics` for THIS user's connections."""
        return self._facade._metrics(self._pairs())
