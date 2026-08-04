"""The app factory — the whole composition point (SPEC rule #11).

``create_app(deps)`` and nothing else. The HTTP API is a THIN shell over
an importable core: this module wires an injected :class:`~auradefi.api
.deps.Deps` to four routers and installs two app-level concerns, and it

* defines NO route of its own,
* reads NO environment variable and no config file,
* holds NO module-level state — no ``app = FastAPI()`` at import, no
  singleton ``Deps`` — so two apps in one process never share a tenant
  store (``api/deps.py``'s promise, made structural here),
* starts NO thread, task or scheduler: webhook delivery is host-scheduled
  (SPEC §8, "the host owns scheduling"), so nothing here ticks a
  ``Deliverer``.

``fastapi.Depends`` appears nowhere in ``api/`` — every handler calls
``require_api_key``/``require_user_token`` explicitly on its first line,
which keeps each one a plain function a test can call directly.
"""

from __future__ import annotations

from types import ModuleType

from fastapi import FastAPI

from auradefi import __version__
from auradefi.api.deps import Deps, install_quota_headers
from auradefi.api.errors import install_error_handlers
from auradefi.api.routes import admin, auth, connections, sync

#: The four route modules, in mount order. Each exposes ``router(deps) ->
#: APIRouter``; there is no filename magic and no import-time registration
#: (SPEC §4.5), so adding a surface is an edit to this tuple, in review.
ROUTE_MODULES: tuple[ModuleType, ...] = (auth, connections, sync, admin)


def create_app(deps: Deps) -> FastAPI:
    """Build the ASGI application for one fully-wired ``deps``.

    ``title="auradefi"``, ``version=auradefi.__version__``,
    ``docs_url="/docs"``. Installs the single error handler
    (:func:`~auradefi.api.errors.install_error_handlers`) and then the
    nine quota headers
    (:func:`~auradefi.api.deps.install_quota_headers`), then includes
    exactly the four :data:`ROUTE_MODULES` routers.

    Performs NO I/O: no clock read, no store touch, no network. Calling
    it twice with two different ``deps`` yields two independent apps.
    """
    app = FastAPI(title="auradefi", version=__version__, docs_url="/docs")
    install_error_handlers(app, deps)
    install_quota_headers(app, deps)
    for module in ROUTE_MODULES:
        app.include_router(module.router(deps))
    return app
