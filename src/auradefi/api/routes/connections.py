"""Connections: create, list, read, delete (SPEC §3.1, §7.1).

A Connection is Plaid's Item — one credentialed-or-watched source owned
by one end user inside one project. Everything here is driven by a USER
token, so the caller is the user: ``project_id`` is never echoed back and
never accepted as input, and another user's (or another tenant's)
connection id answers 404, identical to an id that never existed.

The 409 body is not built here. ``TenancyStore.create_connection``
raises :class:`~auradefi.errors.ConflictError` carrying the existing
``conn_`` id and the single handler in ``api/errors.py`` renders both
``existing_id`` and Vezgo's ``existing_connection_id`` — so the reposted
descriptor, differing only in capitalisation, resolves to the SAME
deterministic id and the caller is told which one.

``DELETE`` is mounted only when the host bound ``deps.delete_connection``
(rule #10 on the route surface: never advertise a capability the
deployment cannot perform). When it is unbound the path answers 404 —
never 405, which would confirm the capability exists.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict

from auradefi.api.deps import (
    Deps,
    consume_quota,
    require_user_token,
    resolve_end_user,
)
from auradefi.errors import NotFoundError
from auradefi.tenancy.models import Connection, ConnectionKind, Scope
from auradefi.webhooks.models import EventName


class ConnectionRequest(BaseModel):
    """``POST /connections`` body — exactly ``{kind, descriptor}``.

    ``kind`` is typed as :class:`~auradefi.tenancy.models.ConnectionKind`
    rather than re-listed here, so the vocabulary has one home; anything
    outside it is a 422.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ConnectionKind
    descriptor: str


def _connection_wire(connection: Connection) -> dict[str, Any]:
    """Project one ``Connection``: exactly ``{id, end_user_id, kind,
    descriptor, created_at_ms}``.

    ``project_id`` is deliberately absent — a user-token caller has no
    business learning the tenant id it lives under.
    """
    return {
        "id": connection.id,
        "end_user_id": connection.end_user_id,
        "kind": str(connection.kind),
        "descriptor": connection.descriptor,
        "created_at_ms": connection.created_at,
    }


def _event_data(connection: Connection) -> dict[str, Any]:
    """The webhook payload for a connection event.

    Exactly ``{connection_id, descriptor, end_user_id, kind}`` — the same
    four keys for ``connection.created`` and ``connection.deleted``, so a
    receiver parses one shape.
    """
    return {
        "connection_id": connection.id,
        "descriptor": connection.descriptor,
        "end_user_id": connection.end_user_id,
        "kind": str(connection.kind),
    }


def _owned(
    deps: Deps, project_id: str, end_user_id: str, connection_id: str
) -> Connection:
    """This user's connection, or ``NotFoundError`` with the store's message.

    Two hops, one indistinguishable failure: the store answers 404 for
    another tenant's id, and a wrong ``end_user_id`` inside the same
    project raises the SAME message shape, so neither is a probe.
    """
    connection = deps.tenancy.get_connection(project_id, connection_id)
    if connection.end_user_id != end_user_id:
        raise NotFoundError(f"connection not found: {connection_id!r}")
    return connection


def _mount_delete(api: APIRouter, deps: Deps) -> None:
    """Mount ``DELETE`` iff the host bound a deleter (rule #10).

    Unbound, a schema-invisible stub answers 404 rather than letting
    Starlette answer 405: a "method not allowed" would confirm the path
    exists for someone, which is exactly the capability leak rule #10
    closes. The 404 body still comes from the single error handler.
    """
    deleter = deps.delete_connection
    if deleter is None:

        @api.delete("/connections/{connection_id}", include_in_schema=False)
        def unavailable(connection_id: str) -> None:
            """This deployment cannot delete; say so as 'no such thing'."""
            raise NotFoundError(f"connection not found: {connection_id!r}")

        return

    @api.delete("/connections/{connection_id}", status_code=204)
    def delete_connection(connection_id: str, request: Request) -> Response:
        """Authorise, delete through the injected deleter, emit, 204."""
        claims = require_user_token(deps, request, Scope.ACCOUNTS_WRITE)
        consume_quota(deps, claims.project_id)
        user = resolve_end_user(deps, claims)
        connection = _owned(deps, claims.project_id, user.id, connection_id)
        deleter(claims.project_id, connection.id)
        deps.webhooks.emit(
            claims.project_id,
            EventName.CONNECTION_DELETED,
            _event_data(connection),
            deps.clock,
        )
        return Response(status_code=204)


def router(deps: Deps) -> APIRouter:
    """Build the connections router over ``deps``.

    * ``POST /connections`` — user token + ``accounts:write``, quota,
      get-or-create the user, create, then ONE
      ``connection.created`` emit. 201 with five keys.
    * ``GET /connections`` — user token + ``accounts:read``, this user's
      connections in creation order.
    * ``GET /connections/{connection_id}`` — same, one row, 404 for
      anyone else's.
    * ``DELETE /connections/{connection_id}`` — user token +
      ``accounts:write``, mounted IFF ``deps.delete_connection`` is
      bound: authorise, call the injected deleter, emit
      ``connection.deleted``, answer 204 with an empty body. Unbound, a
      schema-invisible stub answers 404.
    """
    api = APIRouter()

    @api.post("/connections", status_code=201)
    def create_connection(body: ConnectionRequest, request: Request) -> dict[str, Any]:
        """Create one connection and emit exactly one event."""
        claims = require_user_token(deps, request, Scope.ACCOUNTS_WRITE)
        consume_quota(deps, claims.project_id)
        user = resolve_end_user(deps, claims)
        # A duplicate raises ConflictError here — before the emit — so a
        # refused create never queues a webhook.
        connection = deps.tenancy.create_connection(
            claims.project_id, user.id, body.kind, body.descriptor, deps.clock
        )
        deps.webhooks.emit(
            claims.project_id,
            EventName.CONNECTION_CREATED,
            _event_data(connection),
            deps.clock,
        )
        return _connection_wire(connection)

    @api.get("/connections")
    def list_connections(request: Request) -> dict[str, Any]:
        """This user's connections in creation order; never anyone else's."""
        claims = require_user_token(deps, request, Scope.ACCOUNTS_READ)
        consume_quota(deps, claims.project_id)
        user = resolve_end_user(deps, claims)
        rows = [
            _connection_wire(connection)
            for connection in deps.tenancy.connections(claims.project_id, user.id)
        ]
        return {"connections": rows, "count": len(rows)}

    @api.get("/connections/{connection_id}")
    def read_connection(connection_id: str, request: Request) -> dict[str, Any]:
        """One connection the caller owns; 404 for every other id."""
        claims = require_user_token(deps, request, Scope.ACCOUNTS_READ)
        consume_quota(deps, claims.project_id)
        user = resolve_end_user(deps, claims)
        return _connection_wire(_owned(deps, claims.project_id, user.id, connection_id))

    _mount_delete(api, deps)
    return api
