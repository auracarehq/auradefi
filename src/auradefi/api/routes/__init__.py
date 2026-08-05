"""Route modules (SPEC §3.2). Each exposes router(deps) -> APIRouter; the
factory in api/app.py is the only composition point, no import-time
registration, no filename magic (SPEC §4.5).
"""
