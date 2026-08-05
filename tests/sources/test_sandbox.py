"""The Sandbox environment: bundled, keyless, offline, and in the wheel.

Sandbox is the first thing a developer touches, so its failure modes are
documentation failures. What this file pins:

1. the recording SHIPS. A wheel missing it makes `pip install auradefi`
   plus five lines fail for the one audience with no way to debug it;
2. it is KEYLESS. The cassette key includes query params, so a recording
   made with a key only replays when that key is passed. A fake key baked
   into a fixture would leak a test artefact into the public API, and the
   symptom would be a `CassetteMissError` that reads like a library bug;
3. it covers BOTH hosts the default ports talk to. Prices come from a
   different origin than chain data, and a recording missing the price leg
   yields holdings with `unpriced` assets rather than a clean failure;
4. it opens no socket, which the autouse guard in `tests/conftest.py`
   turns from a claim into an assertion.
"""

from __future__ import annotations

import json

import pytest

from auradefi.errors import CassetteMissError
from auradefi.sources import sandbox

EXPECTED_HOSTS = {"api.etherscan.io", "coins.llama.fi"}


def _recording() -> dict:
    return json.loads(sandbox.FIXTURE.read_text(encoding="utf-8"))


class TestTheRecordingShips:
    def test_the_fixture_exists_inside_the_package(self):
        assert sandbox.FIXTURE.is_file(), sandbox.FIXTURE
        # Inside the installed package, not the repo's test tree: that is
        # what makes Sandbox work from a wheel with no checkout.
        assert sandbox.FIXTURE.parent.parent.name == "sources"
        assert "tests" not in sandbox.FIXTURE.parts

    def test_the_recording_is_keyless(self):
        urls = [entry["request"]["url"] for entry in _recording()["interactions"]]
        assert urls, "an empty recording would make every sandbox call miss"
        offenders = [url for url in urls if "apikey" in url]
        assert not offenders, (
            "a credential is baked into the bundled recording, so Sandbox "
            f"would need a fake key to replay it: {offenders[:2]}"
        )

    def test_the_recording_covers_chain_data_and_prices(self):
        hosts = {
            entry["request"]["url"].split("/")[2]
            for entry in _recording()["interactions"]
        }
        assert hosts == EXPECTED_HOSTS, hosts


class TestTheClient:
    def test_replays_without_opening_a_socket(self):
        client = sandbox.client()
        response = client.get(
            "https://api.etherscan.io/v2/api",
            params={
                "chainid": "1", "module": "account", "action": "balance",
                "address": sandbox.SANDBOX_ADDRESS, "tag": "latest",
            },
        )
        assert response.status_code == 200
        assert response.json()["result"] == "2000000000000000000"

    def test_kwargs_reach_the_client(self):
        assert sandbox.client(timeout=3.5).timeout.read == 3.5

    def test_an_unrecorded_request_misses_loudly(self):
        """The offline guarantee, and the error a reader will actually meet."""
        with pytest.raises(CassetteMissError) as caught:
            sandbox.client().get("https://api.etherscan.io/v2/api?chainid=999")
        message = str(caught.value)
        # The message must name the recording and enumerate what IS in it, so
        # a reader can tell "I asked for something else" from "the library is
        # broken". Asserted on those two structural parts rather than on a
        # hostname substring, which is the shape of a bad URL check.
        assert sandbox.FIXTURE.name in message
        assert "Recorded interactions" in message
        assert message.count("GET ") > 1


class TestConstantsMatchTheRecording:
    def test_the_address_is_the_one_the_recording_covers(self):
        urls = " ".join(
            entry["request"]["url"] for entry in _recording()["interactions"]
        )
        assert sandbox.SANDBOX_ADDRESS in urls

    def test_the_documented_page_size_is_the_recorded_one(self):
        offsets = {
            entry["request"]["url"].split("offset=")[1].split("&")[0]
            for entry in _recording()["interactions"]
            if "offset=" in entry["request"]["url"] and "action=txlist" in entry["request"]["url"]
        }
        # The one-row liveness probe plus the recorded history page size.
        assert offsets == {"1", str(sandbox.SANDBOX_PAGE_SIZE)}, offsets

    def test_the_chain_is_seeded_in_the_registry(self):
        from auradefi.chains.registry import ChainRegistry

        # connect_address refuses a chain the registry does not hold, so a
        # sandbox chain outside the seed set could never connect.
        assert ChainRegistry().get(sandbox.SANDBOX_CHAIN).caip2 == sandbox.SANDBOX_CHAIN
