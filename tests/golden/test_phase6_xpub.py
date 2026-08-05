"""THE PHASE 6 GATE (SPEC rule #5; SPEC §11 Phase 6; SPEC §10 "derive
locally with BIP32, NEVER send an extended key off-box").

Wires the REAL ``Esplora`` over tests/cassettes/phase6_xpub.json to the
REAL ``xpub.derive_addresses`` through nothing but
``functools.partial(derive_addresses, XPUB, 'p2wpkh')``: the residual
``derive(chain, start, count)`` callable ``scan`` takes. The scanner
never holds the extended key; the cassette proves it, because every
recorded URL carries a bc1 address and the string ``xpub`` does not
appear in the file at all.

The cassette holds EXACTLY 44 interactions: external indices 0..22 and
change indices 0..20 of the BIP32 vector-1 master xpub. That count IS
the gap-20 stop rule. Chain 0: indices 0, 1, 2 are used, so the unused
run only starts at 3 and reaches 20 at index 22. 23 Requests. Chain 1:
index 0 is used, the run reaches 20 at index 20: 21 requests. One
request more than the rule allows and the next URL is unrecorded, so
``CassetteMissError`` fires instead of a silently wrong balance.

Golden balances, hand-derived from the cassette bodies as
``funded_txo_sum - spent_txo_sum`` (chain_stats only, mempool IGNORED):

    external 0   150000000 -   50000000 =  100000000 sats,  3 tx
    external 1       25000 -          0 =      25000 sats,  1 tx  (+7777
                                                       mempool, ignored)
    external 2      600000 -     600000 =          0 sats,  4 tx  (used
                                             but swept: still reported)
    change   0  4999000000 - 4000000000 =  999000000 sats, 12 tx
    TOTAL                                 1099025000 sats = 10.99025 BTC

The addresses themselves were derived by an independent scratch BIP32
implementation validated against published BIP32 test vectors, and are
pinned again in tests/sources/bitcoin/test_xpub.py.
"""

from __future__ import annotations

import ast
import functools
import json
from pathlib import Path

import httpx
import pytest

from auradefi.errors import CassetteMissError
from auradefi.sources.bitcoin.esplora import Esplora, scan
from auradefi.sources.bitcoin.utxo import AddressBalance
from auradefi.sources.bitcoin.xpub import derive_addresses

# BIP32 test vector 1 master public key.
XPUB = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoC"
    "u1Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8"
)
CASSETTE_PATH = Path(__file__).resolve().parents[1] / "cassettes" / "phase6_xpub.json"
BASE = "https://blockstream.info/api/address/"

EXTERNAL_0 = "bc1qp5wfcq48h6d63wyy9qz0awtpfqwwv4sma86mhz"
EXTERNAL_1 = "bc1qrfxr69jqnhwufxgkqgcdep9prq4j4vuw2wyg0v"
EXTERNAL_2 = "bc1qhvd6suvqzjcu9pxjhrwhtrlj85ny3n2mqql5w4"
CHANGE_0 = "bc1q7zwtzcqsm3k43ha0ac7nl8cz0hqrhckywf6sew"

EXPECTED_ROWS = (
    AddressBalance(
        address=EXTERNAL_0, chain=0, index=0, balance_sats=100_000_000, tx_count=3
    ),
    AddressBalance(
        address=EXTERNAL_1, chain=0, index=1, balance_sats=25_000, tx_count=1
    ),
    AddressBalance(address=EXTERNAL_2, chain=0, index=2, balance_sats=0, tx_count=4),
    AddressBalance(
        address=CHANGE_0, chain=1, index=0, balance_sats=999_000_000, tx_count=12
    ),
)
TOTAL_SATS = 1_099_025_000
BTC_CAIP19 = "bip122:000000000019d6689c085ae165831e93/slip44:0"


def _recording_client(cas) -> tuple[httpx.Client, list[str]]:
    """A cassette-backed client that records every URL it is asked for."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return cas.handle(request)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def _derive():
    """Exactly what a host wires: the xpub is bound, never passed on."""
    return functools.partial(derive_addresses, XPUB, "p2wpkh")


def _cassette_urls() -> list[str]:
    document = json.loads(CASSETTE_PATH.read_text(encoding="utf-8"))
    return [entry["request"]["url"] for entry in document["interactions"]]


class TestPhase6Gate:
    """The acceptance scenario: xpub in, four balances out, 44 requests."""

    def test_scan_returns_the_four_golden_rows_in_order(self, cassette):
        result = scan(
            Esplora(cassette("phase6_xpub").client()), _derive(), gap=20
        )
        assert result.addresses == EXPECTED_ROWS

    def test_totals(self, cassette):
        result = scan(
            Esplora(cassette("phase6_xpub").client()), _derive(), gap=20
        )
        assert result.total_sats == TOTAL_SATS
        assert str(result.total) == "10.99025"
        assert result.caip19 == BTC_CAIP19

    def test_mempool_never_moves_a_confirmed_balance(self, cassette):
        result = scan(
            Esplora(cassette("phase6_xpub").client()), _derive(), gap=20
        )
        external_1 = result.addresses[1]
        assert external_1.address == EXTERNAL_1
        # the cassette carries mempool funded 7777 on this address
        assert external_1.balance_sats == 25_000
        assert external_1.balance_sats + 7777 != 25_000

    def test_swept_address_is_reported_with_zero_balance(self, cassette):
        result = scan(
            Esplora(cassette("phase6_xpub").client()), _derive(), gap=20
        )
        swept = result.addresses[2]
        assert (swept.address, swept.balance_sats, swept.tx_count) == (
            EXTERNAL_2,
            0,
            4,
        )

    def test_exactly_44_requests_in_derivation_order(self, cassette):
        client, seen = _recording_client(cassette("phase6_xpub"))
        scan(Esplora(client), _derive(), gap=20)
        assert len(seen) == 44
        expected = [BASE + a for a in derive_addresses(XPUB, "p2wpkh", 0, 0, 23)]
        expected += [BASE + a for a in derive_addresses(XPUB, "p2wpkh", 1, 0, 21)]
        assert seen == expected

    def test_one_extra_request_would_miss_the_cassette(self, cassette):
        # gap=21 pushes the external chain to index 23, which is unrecorded:
        # the 44-interaction count is a hard proof of the gap-20 stop rule.
        with pytest.raises(CassetteMissError):
            scan(Esplora(cassette("phase6_xpub").client()), _derive(), gap=21)


class TestExtendedKeyNeverLeavesTheBox:
    """SPEC §10, enforced against the recorded wire traffic and the source."""

    def test_every_recorded_url_is_a_bc1_address_lookup(self):
        urls = _cassette_urls()
        assert len(urls) == 44
        assert len(set(urls)) == 44
        for url in urls:
            assert url.startswith(BASE), url
            address = url[len(BASE) :]
            assert address.startswith("bc1"), url
            assert "/" not in address, url

    def test_the_xpub_appears_nowhere_in_the_cassette(self):
        raw = CASSETTE_PATH.read_text(encoding="utf-8")
        assert XPUB not in raw
        assert "xpub" not in raw

    def test_xpub_module_imports_no_httpx_and_no_esplora(self):
        import auradefi.sources.bitcoin.xpub as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert not any("httpx" in name for name in imported), sorted(imported)
        assert not any("esplora" in name for name in imported), sorted(imported)
        # Stricter than the import list: no IDENTIFIER anywhere in the module
        # may name httpx or esplora. Only prose (docstrings) may mention them.
        identifiers: set[str] = set()
        for node in ast.walk(tree):
            for attribute in ("id", "attr", "name", "module", "arg"):
                value = getattr(node, attribute, None)
                if isinstance(value, str):
                    identifiers.add(value)
        offenders = {
            name
            for name in identifiers
            if "httpx" in name.lower() or "esplora" in name.lower()
        }
        assert not offenders, sorted(offenders)
