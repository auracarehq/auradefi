"""Recorded-HTTP cassette replay for offline tests.

SPEC §13: pytest must pass on a fresh clone with no API keys, cassettes
committed. A cassette is a JSON file:

    {"interactions": [
        {"request":  {"method": "GET",
                      "url": "https://api.example.com/v1/balances?address=0xabc"},
         "response": {"status": 200,
                      "headers": {"content-type": "application/json"},
                      "json": {"ok": true}}}
    ]}

``response`` carries exactly one of ``json`` or ``text``. Matching is by
method + host + path + sorted query string; repeated identical requests
replay their recorded interactions in order, and the final one repeats so
idempotent polling works. Any request with no recorded match raises
CassetteMissError — the offline guarantee fails loudly, never by letting a
live call escape.

Hosts embedding auradefi may use this module to test their own integration
without touching a network.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx

from auradefi.errors import CassetteError, CassetteMissError

_Key = tuple[str, str, str, tuple[tuple[str, str], ...]]


def _canonical_key(method: str, url: str) -> _Key:
    parts = urlsplit(str(url))
    query = tuple(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return (method.upper(), parts.netloc.lower(), parts.path or "/", query)


def _build_response(spec: dict) -> httpx.Response:
    status = spec.get("status", 200)
    headers = spec.get("headers", {})
    if "json" in spec and "text" in spec:
        raise CassetteError("response carries both 'json' and 'text'; pick one")
    if "json" in spec:
        return httpx.Response(status, headers=headers, json=spec["json"])
    return httpx.Response(status, headers=headers, text=spec.get("text", ""))


class Cassette:
    """A loaded cassette; ``transport()`` yields the replaying transport."""

    def __init__(self, path: Path, interactions: list[dict]) -> None:
        self._path = path
        self._recorded: dict[_Key, list[dict]] = {}
        self._served: dict[_Key, int] = {}
        for index, interaction in enumerate(interactions):
            try:
                request = interaction["request"]
                key = _canonical_key(request["method"], request["url"])
                response = interaction["response"]
            except (KeyError, TypeError) as exc:
                raise CassetteError(
                    f"{path}: interaction {index} is malformed: {exc!r}"
                ) from exc
            self._recorded.setdefault(key, []).append(response)
        for responses in self._recorded.values():
            for response in responses:
                _build_response(response)  # validate eagerly, fail at load time

    def handle(self, request: httpx.Request) -> httpx.Response:
        key = _canonical_key(request.method, str(request.url))
        responses = self._recorded.get(key)
        if not responses:
            recorded = "\n  ".join(
                f"{method} {host}{path}" for method, host, path, _ in self._recorded
            )
            raise CassetteMissError(
                f"{request.method} {request.url} is not recorded in {self._path.name}."
                f" Recorded interactions:\n  {recorded or '(none)'}"
            )
        index = min(self._served.get(key, 0), len(responses) - 1)
        self._served[key] = index + 1
        return _build_response(responses[index])

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, **kwargs) -> httpx.Client:
        return httpx.Client(transport=self.transport(), **kwargs)


def load(path: str | Path) -> Cassette:
    path = Path(path)
    if not path.exists():
        raise CassetteError(f"cassette not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CassetteError(f"{path}: invalid JSON: {exc}") from exc
    interactions = document.get("interactions")
    if not isinstance(interactions, list):
        raise CassetteError(f"{path}: top-level 'interactions' list is required")
    return Cassette(path, interactions)
