"""The cassette harness is itself under test: replay, ordering, misses,
malformed files: all offline (the autouse socket guard is active here)."""

from __future__ import annotations

import httpx
import pytest

from auradefi.errors import CassetteError, CassetteMissError
from auradefi.testing.cassettes import load


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
