"""auradefi quickstart — runs fully offline against the installed package.

Executed by CI and by `docker run` as a smoke test; grows with each phase.
Currently demonstrates the Phase 0 foundation: settings, the clock port,
the exception taxonomy, and the cassette replay harness.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import auradefi
from auradefi.clock import FrozenClock, SystemClock
from auradefi.config import Settings
from auradefi.errors import AuradefiError, CassetteMissError
from auradefi.testing.cassettes import load

print(f"auradefi {auradefi.__version__}")

# --- configuration: nothing is required; no API keys, ever, for tests ----
settings = Settings.from_env(env={})
print(f"settings: timeout={settings.http_timeout_s}s (no keys set — fine)")

# --- time is a port; tests freeze it ------------------------------------
clock = FrozenClock(1_754_000_000_000)
clock.advance(2_500)
assert clock.now_ms() == 1_754_000_002_500
print(f"frozen clock: {clock.now_ms()} ms; system clock: {SystemClock().now_ms()} ms")

# --- cassette replay: recorded HTTP, no sockets --------------------------
cassette_doc = {
    "interactions": [
        {
            "request": {"method": "GET", "url": "https://api.demo.invalid/v1/ping"},
            "response": {"status": 200, "json": {"pong": True, "raw": "1000000000000000000"}},
        }
    ]
}
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "demo.json"
    path.write_text(json.dumps(cassette_doc))
    client = load(path).client()
    body = client.get("https://api.demo.invalid/v1/ping").json()
    assert body["pong"] is True
    assert isinstance(body["raw"], str), "raw amounts are strings, never JSON numbers"
    print(f"cassette replay: pong={body['pong']}, raw={body['raw']!r} (a string — rule #2)")

    try:
        client.get("https://api.demo.invalid/v1/not-recorded")
    except CassetteMissError:
        print("unrecorded request refused (offline guarantee holds)")

# --- one exception type at the boundary ----------------------------------
try:
    Settings(http_timeout_s=-1)
except AuradefiError as exc:
    print(f"config validation: {exc}")

print("quickstart OK")
