"""The one exception handler: the pinned error-status table (SPEC §7).

docs/internal/DECISIONS.md ("HTTP error table") is the whole contract, verbatim:
first hit walking ``type(exc).__mro__`` over the ordered table below; body
``{"error": {"type", "message", "status"}}`` plus ``existing_id`` (and
``existing_connection_id`` when it starts ``conn_``) on 409, plus header
``Retry-After`` (whole seconds, >= 1) on 429.

Two deliberate non-features:

* ``CursorError`` is 422 rather than inheriting ``LedgerError``'s 500 — a
  mistyped ``?cursor=`` is the client's fault. ``DecodeError`` and
  ``TenantIsolationError`` are NOT in the table and fall through their MRO
  to 500: they are our bug, not a documented client contract.
* Non-``AuradefiError`` exceptions are unhandled, on purpose. A
  ``ValueError`` escaping a route is a defect, and dressing it as an API
  response hides it.

``existing_connection_id`` is SPEC §7.1's Vezgo-verbatim field, emitted by
this handler from ``ConflictError.existing_id`` — never by route-level
magic — so every 409 in the surface carries it for free. Both keys live
INSIDE the ``error`` object, beside ``type``/``message``/``status``.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from auradefi.api.deps import Deps, retry_after_seconds
from auradefi.errors import (
    AuradefiError,
    AuthError,
    CaipParseError,
    ConflictError,
    CursorError,
    NotFoundError,
    QuotaExceededError,
    ScopeError,
    SourceError,
    TokenExpiredError,
    TokenRevokedError,
    ValidationError,
)

#: Status for an ``AuradefiError`` whose MRO hits nothing (unreachable —
#: ``AuradefiError`` itself is the table's last entry).
DEFAULT_STATUS = 500

#: The reshaped message for pydantic's ``RequestValidationError``.
VALIDATION_MESSAGE = "request validation failed"

#: The pinned table, ORDERED (DECISIONS "HTTP error table"). Subclasses
#: precede their bases — ``ScopeError``/``TokenExpiredError``/
#: ``TokenRevokedError`` before ``AuthError``, ``CaipParseError`` before
#: ``ValidationError`` — so a table walk and an MRO walk agree.
STATUS_TABLE: dict[type[AuradefiError], int] = {
    ValidationError: 422,
    CaipParseError: 422,
    CursorError: 422,
    ScopeError: 403,
    TokenExpiredError: 401,
    TokenRevokedError: 401,
    AuthError: 401,
    NotFoundError: 404,
    ConflictError: 409,
    QuotaExceededError: 429,
    SourceError: 502,
    AuradefiError: 500,
}


def status_for(exc: AuradefiError) -> int:
    """HTTP status for ``exc``: first :data:`STATUS_TABLE` hit in its MRO.

    ``ScopeError`` is 403 though it subclasses ``AuthError`` (401), and
    ``CursorError`` is 422 though it subclasses ``LedgerError``.
    ``DecodeError`` and ``TenantIsolationError`` reach ``AuradefiError``
    and are 500. Anything with no hit is :data:`DEFAULT_STATUS`.
    """
    for ancestor in type(exc).__mro__:
        status = STATUS_TABLE.get(ancestor)
        if status is not None:
            return status
    return DEFAULT_STATUS


def error_body(exc: AuradefiError) -> dict[str, object]:
    """The JSON body for ``exc`` — exactly one top-level key, ``"error"``.

    ``{"error": {"type": type(exc).__name__, "message": str(exc),
    "status": status_for(exc)}}``, plus — for a ``ConflictError`` with a
    non-``None`` ``existing_id`` — ``existing_id``, plus
    ``existing_connection_id`` with the SAME value when it starts
    ``"conn_"`` (SPEC §7.1). Both extra keys sit inside ``error``. A
    ``ConflictError`` carrying ``"whe_abc"`` renders ``existing_id``
    only; one carrying ``None`` renders neither.
    """
    error: dict[str, object] = {
        "type": type(exc).__name__,
        "message": str(exc),
        "status": status_for(exc),
    }
    existing_id = exc.existing_id if isinstance(exc, ConflictError) else None
    if existing_id is not None:
        error["existing_id"] = existing_id
        if existing_id.startswith("conn_"):
            error["existing_connection_id"] = existing_id
    return {"error": error}


def install_error_handlers(app: FastAPI, deps: Deps) -> None:
    """Register EXACTLY ONE ``AuradefiError`` handler, plus 422 reshaping.

    The ``AuradefiError`` handler answers :func:`status_for` with
    :func:`error_body`, and on 429 adds ``Retry-After`` (whole seconds,
    from ``deps.quota.snapshot`` through
    ``auradefi.api.deps.retry_after_seconds``) when
    ``request.state.project_id`` is bound.

    The second handler is for ``fastapi.exceptions.RequestValidationError``
    only: pydantic's ``{"detail": [...]}`` is reshaped into the same body
    with ``error.type == "ValidationError"``, ``error.status == 422``,
    ``error.message == VALIDATION_MESSAGE`` and ``error.details`` holding
    pydantic's JSON-safe error list.

    No handler is registered for ``Exception`` — a non-``AuradefiError``
    propagates untouched.
    """

    @app.exception_handler(AuradefiError)
    async def _auradefi_error(request: Request, exc: AuradefiError) -> JSONResponse:
        status = status_for(exc)
        headers: dict[str, str] = {}
        project_id = getattr(request.state, "project_id", None)
        if status == STATUS_TABLE[QuotaExceededError] and project_id is not None:
            headers["Retry-After"] = str(
                retry_after_seconds(
                    deps.quota.snapshot(project_id), deps.clock.now_ms()
                )
            )
        return JSONResponse(
            error_body(exc), status_code=status, headers=headers or None
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        status = STATUS_TABLE[ValidationError]
        return JSONResponse(
            {
                "error": {
                    "type": ValidationError.__name__,
                    "message": VALIDATION_MESSAGE,
                    "status": status,
                    "details": jsonable_encoder(exc.errors()),
                }
            },
            status_code=status,
        )
