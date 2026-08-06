"""The cassette harness is itself under test: replay, ordering, misses,
malformed files, and recording: all offline (the autouse socket guard is
active here, so every Recorder below is given a MockTransport to wrap)."""

from __future__ import annotations

import json

import httpx
import pytest

from auradefi.errors import CassetteError, CassetteMissError
from auradefi.testing.cassettes import REDACTED_PARAMS, Recorder, load


def _service(status: int = 200, headers=None, body=None, text: str | None = None):
    """A stand-in upstream, as a transport a Recorder can wrap."""

    def handle(request: httpx.Request) -> httpx.Response:
        merged = {"content-type": "application/json"}
        merged.update(headers or {})
        if text is not None:
            return httpx.Response(status, headers=merged, text=text)
        return httpx.Response(status, headers=merged, json=body or {"ok": True})

    return httpx.MockTransport(handle)


def test_replays_recorded_json_body(cassette):
    client = cassette("demo_balances").client()
    response = client.get(
        "https://api.demo.invalid/v1/balances",
        params={"address": "0xd8da6bf26964af9d7eed9e03e53415d37aa96045", "chain": "eip155:1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["balances"][0]["raw"] == "1234567890123456789"
    assert isinstance(body["balances"][0]["raw"], str)


def test_query_order_does_not_matter(cassette):
    client = cassette("demo_balances").client()
    response = client.get(
        "https://api.demo.invalid/v1/balances",
        params={"chain": "eip155:1", "address": "0xd8da6bf26964af9d7eed9e03e53415d37aa96045"},
    )
    assert response.status_code == 200


def test_repeated_requests_replay_in_order_then_repeat_last(cassette):
    client = cassette("demo_balances").client()
    url = "https://api.demo.invalid/v1/health"
    assert client.get(url).status_code == 503
    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 200  # final interaction repeats


def test_unrecorded_request_raises_miss_not_live_call(cassette):
    client = cassette("demo_balances").client()
    with pytest.raises(CassetteMissError):
        client.get("https://api.demo.invalid/v1/unrecorded")


def test_missing_cassette_file_raises(tmp_path):
    with pytest.raises(CassetteError):
        load(tmp_path / "absent.json")


def test_malformed_cassette_raises_at_load_time(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"interactions": [{"request": {"method": "GET"}}]}')
    with pytest.raises(CassetteError):
        load(bad)


def test_transport_is_plain_httpx_mock_transport(cassette):
    assert isinstance(cassette("demo_balances").transport(), httpx.MockTransport)


class TestRecorder:
    """Recording is the other half of replay: a host records its own address
    once against the live service, then runs offline forever after."""

    def test_what_it_records_replays(self, tmp_path):
        path = tmp_path / "wallet.json"
        recorder = Recorder(path, transport=_service(body={"balance": "12"}))
        live = recorder.client().get("https://api.demo.invalid/v1/balance")
        recorder.save()

        replayed = load(path).client().get("https://api.demo.invalid/v1/balance")
        assert replayed.status_code == live.status_code == 200
        assert replayed.json() == live.json() == {"balance": "12"}

    # pins: a recorded URL carrying apikey= would leak the credential into a
    #       file somebody commits, AND would only ever match a replay that
    #       resent the same key, which is not an offline fixture at all.
    def test_a_credential_never_reaches_the_cassette(self, tmp_path):
        path = tmp_path / "wallet.json"
        recorder = Recorder(path, transport=_service())
        recorder.client().get(
            "https://api.demo.invalid/v1/balance",
            params={"address": "0xabc", "apikey": "SUPERSECRET"},
        )
        recorder.save()

        assert "SUPERSECRET" not in path.read_text(encoding="utf-8")
        recorded = json.loads(path.read_text(encoding="utf-8"))
        assert "apikey" not in recorded["interactions"][0]["request"]["url"]

    # pins: redaction is what makes the replay keyless. Recording with a key
    #       and replaying without one has to hit the same cassette entry.
    def test_a_keyed_recording_replays_from_a_keyless_client(self, tmp_path):
        path = tmp_path / "wallet.json"
        recorder = Recorder(path, transport=_service(body={"n": 1}))
        recorder.client().get(
            "https://api.demo.invalid/v1/balance",
            params={"address": "0xabc", "apikey": "KEY"},
        )
        recorder.save()

        keyless = load(path).client().get(
            "https://api.demo.invalid/v1/balance", params={"address": "0xabc"}
        )
        assert keyless.json() == {"n": 1}

    def test_every_documented_parameter_name_is_redacted(self, tmp_path):
        for index, name in enumerate(sorted(REDACTED_PARAMS)):
            path = tmp_path / f"{index}.json"
            recorder = Recorder(path, transport=_service())
            recorder.client().get(
                "https://api.demo.invalid/v1/x", params={name: "SECRET"}
            )
            recorder.save()
            assert "SECRET" not in path.read_text(encoding="utf-8"), name

    def test_a_caller_may_name_its_own_parameters(self, tmp_path):
        path = tmp_path / "wallet.json"
        recorder = Recorder(path, transport=_service(), redact={"session"})
        recorder.client().get(
            "https://api.demo.invalid/v1/x", params={"session": "SECRET"}
        )
        recorder.save()
        assert "SECRET" not in path.read_text(encoding="utf-8")

    # pins: a cassette is a file somebody commits, so a Set-Cookie or a
    #       tracing header recorded from a live service is stored for nothing.
    def test_only_the_content_type_survives_from_the_response_headers(self, tmp_path):
        path = tmp_path / "wallet.json"
        recorder = Recorder(
            path,
            transport=_service(headers={"set-cookie": "session=SECRET", "x-trace": "9"}),
        )
        recorder.client().get("https://api.demo.invalid/v1/x")
        recorder.save()

        recorded = json.loads(path.read_text(encoding="utf-8"))
        assert recorded["interactions"][0]["response"]["headers"] == {
            "content-type": "application/json"
        }
        assert "SECRET" not in path.read_text(encoding="utf-8")

    def test_a_non_json_body_is_recorded_as_text(self, tmp_path):
        path = tmp_path / "wallet.json"
        recorder = Recorder(
            path,
            transport=_service(headers={"content-type": "text/plain"}, text="plain"),
        )
        recorder.client().get("https://api.demo.invalid/v1/x")
        recorder.save()

        recorded = json.loads(path.read_text(encoding="utf-8"))
        assert recorded["interactions"][0]["response"]["text"] == "plain"
        assert "json" not in recorded["interactions"][0]["response"]

    def test_a_json_content_type_with_an_unparseable_body_falls_back_to_text(
        self, tmp_path
    ):
        path = tmp_path / "wallet.json"
        recorder = Recorder(
            path, transport=_service(text="{not json", headers={"content-type": "application/json"})
        )
        recorder.client().get("https://api.demo.invalid/v1/x")
        recorder.save()

        recorded = json.loads(path.read_text(encoding="utf-8"))
        assert recorded["interactions"][0]["response"]["text"] == "{not json"

    def test_a_recorded_error_status_replays_as_that_status(self, tmp_path):
        path = tmp_path / "wallet.json"
        recorder = Recorder(path, transport=_service(status=429, body={"m": "slow down"}))
        recorder.client().get("https://api.demo.invalid/v1/x")
        recorder.save()

        assert load(path).client().get("https://api.demo.invalid/v1/x").status_code == 429

    # pins: an empty cassette loads cleanly and then misses on every request,
    #       which reads as a broken library instead of an empty session.
    def test_saving_nothing_is_refused(self, tmp_path):
        recorder = Recorder(tmp_path / "empty.json", transport=_service())
        with pytest.raises(CassetteError):
            recorder.save()
        assert not (tmp_path / "empty.json").exists()

    def test_the_context_manager_saves_on_a_clean_exit(self, tmp_path):
        path = tmp_path / "wallet.json"
        with Recorder(path, transport=_service()) as recorder:
            recorder.client().get("https://api.demo.invalid/v1/x")
        assert path.exists()

    # pins: saving a half-finished cassette on the way out of a failure gives
    #       the next run a fixture that is missing whatever came after the raise.
    def test_the_context_manager_writes_nothing_when_the_body_raises(self, tmp_path):
        path = tmp_path / "wallet.json"
        with pytest.raises(RuntimeError):
            with Recorder(path, transport=_service()) as recorder:
                recorder.client().get("https://api.demo.invalid/v1/x")
                raise RuntimeError("mid-session")
        assert not path.exists()

    def test_it_creates_the_directory_it_is_pointed_at(self, tmp_path):
        path = tmp_path / "fixtures" / "nested" / "wallet.json"
        recorder = Recorder(path, transport=_service())
        recorder.client().get("https://api.demo.invalid/v1/x")
        assert recorder.save() == path
        assert path.exists()

    # pins: the default has to be a REAL transport. A mock default would make
    #       every recording session silently record nothing from the network.
    def test_the_default_transport_is_a_live_one(self, tmp_path):
        recorder = Recorder(tmp_path / "wallet.json")
        assert isinstance(recorder._inner, httpx.HTTPTransport)
        assert not isinstance(recorder._inner, httpx.MockTransport)
