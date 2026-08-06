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
CassetteMissError. The offline guarantee fails loudly, never by letting a
live call escape.

Hosts embedding auradefi may use this module to test their own integration
without touching a network. :class:`Recorder` is how they get a cassette of
their own address to replay: point it at the live service once, and every
later run is offline.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from auradefi.errors import CassetteError, CassetteMissError

_Key = tuple[str, str, str, tuple[tuple[str, str], ...]]

#: Query parameters dropped from a recorded URL. Two reasons, and both
#: matter: a saved cassette must carry no credential, and a cassette
#: recorded WITH ``apikey`` would only ever match a replay that resent the
#: same key, which defeats the point of an offline fixture. The bundled
#: ``sandbox.json`` has the same shape for the same reason: it was recorded
#: through a keyless client, so no URL in it carries one.
REDACTED_PARAMS: frozenset[str] = frozenset(
    {"apikey", "api_key", "api-key", "access_token"}
)


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


def _capture(response: httpx.Response) -> dict:
    """One live response as a cassette entry.

    Only ``content-type`` is kept from the headers. Everything else a
    service sends back (cookies, tracing ids, an echoed credential) is
    dropped, because replay needs none of it and a cassette is a file
    somebody will commit.
    """
    content_type = response.headers.get("content-type", "")
    spec: dict = {"status": response.status_code}
    if content_type:
        spec["headers"] = {"content-type": content_type}
    if "json" in content_type:
        try:
            spec["json"] = response.json()
        except ValueError:
            spec["text"] = response.text
    else:
        spec["text"] = response.text
    return spec


class Recorder:
    """Wraps a real transport, saving each interaction for later replay.

    The other half of :func:`load`. Point one at a live service once, then
    run offline forever after::

        recorder = Recorder("mywallet.json")
        source = EtherscanSource(recorder.client(), api_key=KEY)
        source.balances("eip155:1", "0x…")
        recorder.save()

        # every run after this one, with no key and no network:
        source = EtherscanSource(load("mywallet.json").client())

    Credentials named in :data:`REDACTED_PARAMS` are stripped from every
    recorded URL, which is also why the replay above needs no key: the
    saved request matches a keyless client. A secret in the path or in a
    header is never written at all, since this format stores neither.

    Response BODIES are saved whole, so read a cassette before committing
    it. Redaction covers what this package puts on the wire, and a service
    that echoes your credential back to you defeats it.

    What :meth:`handle` returns to the caller is built from the entry it
    just saved, so a recording run exercises the same bytes the replay
    will. A response this format cannot represent therefore fails while
    you are recording, in front of you, instead of at replay time.
    """

    def __init__(
        self,
        path: str | Path,
        transport: httpx.BaseTransport | None = None,
        redact: frozenset[str] | set[str] | tuple[str, ...] = REDACTED_PARAMS,
    ) -> None:
        self._path = Path(path)
        self._inner = transport if transport is not None else httpx.HTTPTransport()
        self._redact = frozenset(name.lower() for name in redact)
        self._interactions: list[dict] = []

    def _redacted(self, url: str) -> str:
        parts = urlsplit(url)
        kept = [
            (name, value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if name.lower() not in self._redact
        ]
        return urlunsplit(parts._replace(query=urlencode(kept)))

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Perform ``request`` for real, record it, and answer from the record."""
        live = self._inner.handle_request(request)
        try:
            live.read()
        finally:
            live.close()
        spec = _capture(live)
        self._interactions.append({
            "request": {
                "method": request.method,
                "url": self._redacted(str(request.url)),
            },
            "response": spec,
        })
        return _build_response(spec)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, **kwargs) -> httpx.Client:
        return httpx.Client(transport=self.transport(), **kwargs)

    def save(self) -> Path:
        """Write the cassette and return its path.

        An empty recording is refused. Written out it would load cleanly
        and then miss on every request, which reads as a broken library
        rather than as a session that recorded nothing.
        """
        if not self._interactions:
            raise CassetteError(
                f"nothing was recorded, so {self._path} is not being written: "
                "a cassette with no interactions misses on every request"
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"interactions": self._interactions}, indent=2) + "\n",
            encoding="utf-8",
        )
        return self._path

    def __enter__(self) -> Recorder:
        return self

    def __exit__(self, kind, value, traceback) -> None:
        """Save on a clean exit only; a half-finished cassette is worse."""
        if kind is None:
            self.save()


def load(path: str | Path) -> Cassette:
    """Read a cassette from disk, ready to replay.

    ``load("wallet.json").client()`` is an ``httpx.Client`` that answers
    from the recording and never opens a socket. Pass it to any source in
    place of a live client.

    Validation is EAGER: a missing file, invalid JSON, a missing
    ``interactions`` list, a malformed interaction and a response carrying
    both ``json`` and ``text`` all raise ``CassetteError`` here, rather
    than on the request that happens to reach the bad entry. A fixture is
    either usable or it says so before your test starts.

    :class:`Recorder` is how you make one from a live service.
    """
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
