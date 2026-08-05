"""Transaction sync and batch holdings (SPEC §6.4, §7.3).

``GET /crypto/sync`` is Plaid's envelope, unchanged, over the injected
:class:`~auradefi.ledger.port.LedgerPort`. The ledger tenant key is
PINNED as the caller's ``usr_`` id: it already hashes
``project_id | external_user_id``, so a cross-project collision is
arithmetically impossible and no route can widen the scope by passing
something coarser.

``POST /batch/holdings`` is Allium's union — ``items[]`` of
``Result | Error``, same length and same order as the request, one quota
unit per item ("billing by work done", SPEC §7.3). One bad address
NEVER fails the batch. It is mounted only when the host bound
``deps.holdings`` (rule #10: never advertise a capability the deployment
cannot perform); unbound, the path does not exist and answers 404.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from auradefi.api.deps import (
    Deps,
    consume_quota,
    require_api_key,
    require_user_token,
    resolve_end_user,
)
from auradefi.api.wire import (
    batch_envelope,
    batch_error,
    batch_result,
    batch_warning,
    holdings_wire,
    sync_envelope,
)
from auradefi.errors import AuradefiError, QuotaExceededError, ValidationError
from auradefi.ledger.cursors import decode_cursor
from auradefi.tenancy.models import Scope


class BatchItem(BaseModel):
    """One requested pair — exactly ``{chain, address}``, extras forbidden."""

    model_config = ConfigDict(extra="forbid")

    chain: str
    address: str


class BatchRequest(BaseModel):
    """``POST /batch/holdings`` body — exactly ``{items: [BatchItem]}``."""

    model_config = ConfigDict(extra="forbid")

    items: list[BatchItem]


def _resolved_limit(deps: Deps, limit: int | None) -> int:
    """``limit`` or ``deps.sync_limit_default``, bounded by the pinned cap.

    Raises :class:`~auradefi.errors.ValidationError` NAMING
    ``deps.sync_limit_max`` for anything outside ``1 <= limit <= max`` —
    a caller must be told the cap, not silently clamped to it.
    """
    value = deps.sync_limit_default if limit is None else limit
    if not 1 <= value <= deps.sync_limit_max:
        raise ValidationError(
            f"limit must be between 1 and {deps.sync_limit_max}, got {value}"
        )
    return value


def _checked_size(deps: Deps, items: list[BatchItem]) -> list[BatchItem]:
    """``items`` unchanged, or ``ValidationError`` naming the cap.

    Checked BEFORE any quota is consumed and any source is called: an
    over-sized batch costs the caller nothing.
    """
    if not 1 <= len(items) <= deps.batch_max_items:
        raise ValidationError(
            f"items must hold between 1 and {deps.batch_max_items} entries, "
            f"got {len(items)}"
        )
    return items


def _priced_item(
    deps: Deps, provider: Any, item: BatchItem, warnings: list[Any]
) -> dict[str, Any]:
    """One item's ``batch_result``, or its ``batch_error``.

    Every :class:`~auradefi.errors.AuradefiError` from the registry or
    the provider becomes an error ITEM — one unknown chain or one dead
    source never fails the other 99. ``warnings`` is appended to in
    place so a report's ``unpriced`` lands in request order.
    """
    try:
        chain = deps.chains.get(item.chain)
        report = provider.holdings(chain.caip2, item.address)
    except AuradefiError as exc:
        return batch_error(item.chain, item.address, exc)
    unpriced = list(report.unpriced)
    if unpriced:
        warnings.append(
            batch_warning(
                "unpriced_assets",
                f"no price for {len(unpriced)} asset(s): {', '.join(unpriced)}",
                item.chain,
                item.address,
            )
        )
    return batch_result(item.chain, item.address, holdings_wire(report))


def _run_batch(
    deps: Deps, provider: Any, project_id: str, items: list[BatchItem]
) -> dict[str, Any]:
    """The pinned partial-success loop: one item in, one item out.

    Items are never deduped and never reordered, so index ``i`` of the
    response always answers index ``i`` of the request. A
    ``QuotaExceededError`` on the FIRST item propagates (zero work done →
    the whole request is a 429); after that it turns the remaining items
    into error entries under ONE ``quota_exhausted`` warning, and the
    response stays 200 for the work already paid for.
    """
    results: list[Any] = []
    warnings: list[Any] = []
    seen: set[tuple[str, str]] = set()
    exhausted: QuotaExceededError | None = None
    for index, item in enumerate(items):
        pair = (item.chain, item.address)
        if pair in seen:
            warnings.append(
                batch_warning(
                    "duplicate_pair",
                    "this (chain, address) appears more than once; every "
                    "occurrence is billed and answered",
                    item.chain,
                    item.address,
                )
            )
        seen.add(pair)
        if exhausted is None:
            try:
                deps.quota.hit(project_id)
            except QuotaExceededError as exc:
                if index == 0:
                    raise
                exhausted = exc
                warnings.append(batch_warning("quota_exhausted", str(exc)))
        if exhausted is not None:
            results.append(batch_error(item.chain, item.address, exhausted))
            continue
        results.append(_priced_item(deps, provider, item, warnings))
    return batch_envelope(results, warnings)


def router(deps: Deps) -> APIRouter:
    """Build the sync/batch router over ``deps``.

    * ``GET /crypto/sync?cursor=&limit=`` — user token +
      ``accounts:read``, quota, then
      ``deps.ledger.sync(end_user.id, cursor, limit)`` projected by
      :func:`~auradefi.api.wire.sync_envelope`. A malformed cursor
      surfaces :class:`~auradefi.errors.CursorError` → 422, never a 500.
    * ``POST /batch/holdings`` — api key + ``accounts:read``, mounted IFF
      ``deps.holdings`` is bound. Empty or over ``deps.batch_max_items``
      is a 422 before any work. Then, per item IN ORDER:
      ``deps.quota.hit`` → ``deps.chains.get`` → ``deps.holdings
      .holdings``. Any :class:`~auradefi.errors.AuradefiError` from the
      last two becomes a ``batch_error`` item and never fails the
      request. A ``QuotaExceededError`` at item ``k > 0`` turns item
      ``k`` and every later item into ``batch_error`` entries plus ONE
      ``quota_exhausted`` warning, still 200 — but a refusal on the
      FIRST item (zero work done) propagates the 429 with
      ``Retry-After``. Warnings also carry ``duplicate_pair`` for a
      repeated ``(chain, address)`` — items are never deduped or
      reordered — and ``unpriced_assets`` when a report's ``unpriced``
      is non-empty.
    """
    api = APIRouter()

    @api.get("/crypto/sync")
    def crypto_sync(
        request: Request, cursor: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        """One page of Plaid's envelope for the calling end user."""
        claims = require_user_token(deps, request, Scope.ACCOUNTS_READ)
        # VALIDATE BEFORE CHARGING (RELEASE_0.1.1 §5 #32), the rule
        # POST /batch/holdings already states in `_checked_size`: "an
        # over-sized batch costs the caller nothing". Both of these are
        # caller-controlled and both can only ever 422, so charging first
        # meant a client with a hard-coded bad limit drained the PROJECT's
        # per-day window on requests it could never succeed at — and then
        # 429'd every other user of that project. The cursor is decoded
        # here rather than left to `ledger.sync` for the same reason: its
        # CursorError is also a 422 the caller pays for otherwise.
        resolved_limit = _resolved_limit(deps, limit)
        decode_cursor(cursor)
        consume_quota(deps, claims.project_id)
        # PINNED: the ledger tenant key is the usr_ id, which already
        # hashes project_id | external_user_id.
        tenant_id = resolve_end_user(deps, claims).id
        return sync_envelope(deps.ledger.sync(tenant_id, cursor, resolved_limit))

    provider = deps.holdings
    if provider is not None:

        @api.post("/batch/holdings")
        def batch_holdings(body: BatchRequest, request: Request) -> dict[str, Any]:
            """Allium's union: partial success, one quota unit per item."""
            key = require_api_key(deps, request, Scope.ACCOUNTS_READ)
            items = _checked_size(deps, body.items)
            return _run_batch(deps, provider, key.project_id, items)

    return api
