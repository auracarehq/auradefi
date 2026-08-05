"""Phase 8 gate: one tenant's whole journey over the HTTP surface.

Offline end to end. The app is driven by ``TestClient`` (in-process ASGI,
no socket) and the webhook deliverer by ``httpx.MockTransport`` (no
socket either), so the autouse guard in tests/conftest.py never fires.

The journey, in one test because the ORDER is the contract:

    issue an adk_ key
      -> POST /auth/token          {token}, one audit row, nine headers
      -> POST /connections         201
      -> repost                    409 + existing_connection_id
      -> upsert 3 ledger rows      under the caller's usr_ id
      -> GET /crypto/sync?limit=2  page until has_more is False
      -> POST /webhooks/endpoints  secret returned exactly once
      -> POST /connections         queues a connection.created
      -> tick (200)                one signed POST, verifiable
      -> tick x6 (500)             the pinned retry schedule, dead letter
      -> GET /webhooks/dead_letter exactly that delivery
      -> POST .../replay           202, ordinal 1
      -> tick (200)                delivered
"""

from __future__ import annotations

import httpx
import pytest

from auradefi.api.app import create_app
from auradefi.api.deps import Deps
from auradefi.chains.registry import ChainRegistry
from auradefi.clock import FrozenClock
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.ledger.models import (
    Direction,
    Entry,
    LedgerTransaction,
    transaction_id,
)
from auradefi.money.quantity import Quantity
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.keys import ApiKeyStore
from auradefi.tenancy.models import Environment, Scope, end_user_id
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import RevocationSet
from auradefi.webhooks.deliver import Deliverer, WebhookStore
from auradefi.webhooks.models import RETRY_SCHEDULE_MS
from auradefi.webhooks.sign import verify_signature
from fastapi.testclient import TestClient

NOW = 1_754_000_000_000
CHAIN = "eip155:1"
ASSET = "eip155:1/slip44:60"
HOOK_URL = "https://hooks.example.com/inbox"
PLAID_KEYS = {"added", "modified", "removed", "next_cursor", "has_more"}
ADDRESSES = (
    "0xAAAAaaaaAAAAaaaaAAAAaaaaAAAAaaaaAAAAaaaa",
    "0xBBBBbbbbBBBBbbbbBBBBbbbbBBBBbbbbBBBBbbbb",
    "0xCCCCccccCCCCccccCCCCccccCCCCccccCCCCcccc",
)


class _Recorder:
    """A MockTransport handler: records every request, answers one status."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code)


def _deliverer(store: WebhookStore, status_code: int) -> tuple[Deliverer, _Recorder]:
    recorder = _Recorder(status_code)
    client = httpx.Client(transport=httpx.MockTransport(recorder))
    return Deliverer(store, client), recorder


def _txn(index: int, account: str) -> LedgerTransaction:
    tx_hash = "0x" + f"{index:02x}" * 32
    return LedgerTransaction(
        id=transaction_id(CHAIN, tx_hash, account),
        chain_id=CHAIN,
        tx_hash=tx_hash,
        account_id=account,
        block_number=18_000_000 + index,
        initiated_at=1_753_000_000_000 + index,
        confirmed_at=1_753_000_000_500 + index,
        entries=(
            Entry(
                asset_id=ASSET,
                quantity=Quantity(index * 10**17, 18),
                direction=Direction.IN,
            ),
        ),
    )


@pytest.fixture
def wired():
    clock = FrozenClock(NOW)
    tenancy = TenancyStore()
    org = tenancy.create_organisation("acme", clock)
    project = tenancy.create_project(org.id, "main", Environment.LIVE, clock)
    vault = {project.id: project.signing_secret}
    webhooks = WebhookStore()
    deps = Deps(
        tenancy=tenancy,
        keys=ApiKeyStore(),
        quota=QuotaCounter(QuotaLimits(1_000, 10_000, 100_000), clock),
        audit=AuditLog(),
        revocations=RevocationSet(),
        ledger=MemoryLedger(),
        webhooks=webhooks,
        chains=ChainRegistry(),
        clock=clock,
        signing_secret_for=vault.get,
    )
    return deps, project, clock, webhooks


def test_phase8_http_api_end_to_end(wired):
    deps, project, clock, webhooks = wired
    client = TestClient(create_app(deps))

    # --- an adk_ key, scoped ------------------------------------------
    record, plaintext = deps.keys.issue(
        project.id,
        Environment.LIVE,
        (Scope.USERS_ADMIN, Scope.ACCOUNTS_READ, Scope.ACCOUNTS_WRITE),
        clock,
    )
    assert plaintext.startswith("adk_live_") and len(plaintext) == 57
    key_headers = {"Authorization": f"Bearer {plaintext}"}

    # --- POST /auth/token ---------------------------------------------
    minted = client.post(
        "/auth/token",
        json={"external_user_id": "host-user-7"},
        headers={**key_headers, "X-Forwarded-For": "198.51.100.9"},
    )
    assert minted.status_code == 200
    assert list(minted.json()) == ["token"]
    token = minted.json()["token"]
    token_headers = {"Authorization": f"Bearer {token}"}

    entries = deps.audit.entries(project.id)
    assert len(entries) == 1
    assert (entries[0].seq, entries[0].event) == (1, "token.minted")
    assert entries[0].key_id == record.id
    # pins: the end-to-end mint audits the socket peer, not the
    #       X-Forwarded-For the request carries. Was `== "198.51.100.9"`
    #       (the header value), which pinned RELEASE_0.1.1 §4 #30 — a
    #       caller-chosen, permanent audit attribution — as contract.
    assert entries[0].ip == "testclient"
    assert entries[0].ip_source == "peer"

    for window, limit in (("Second", 1_000), ("Day", 10_000), ("Month", 100_000)):
        assert minted.headers[f"X-RateLimit-Limit-{window}"] == str(limit)
        assert minted.headers[f"X-RateLimit-Remaining-{window}"] == str(limit - 1)
        assert minted.headers[f"X-RateLimit-Reset-{window}"].isdigit()

    # --- POST /connections, then the same descriptor again ------------
    created = client.post(
        "/connections",
        json={"kind": "address", "descriptor": ADDRESSES[0]},
        headers=token_headers,
    )
    assert created.status_code == 201
    connection_id = created.json()["id"]
    assert connection_id.startswith("conn_")

    conflict = client.post(
        "/connections",
        json={"kind": "address", "descriptor": ADDRESSES[0].lower()},
        headers=token_headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["existing_id"] == connection_id
    assert conflict.json()["error"]["existing_connection_id"] == connection_id

    # --- three ledger rows under the caller's usr_ id -----------------
    tenant_id = end_user_id(project.id, "host-user-7")
    assert client.get("/users/me", headers=token_headers).json()["id"] == tenant_id
    deps.ledger.upsert(tenant_id, [_txn(index, "acct_eth") for index in (1, 2, 3)])

    # --- GET /crypto/sync, paged to exhaustion ------------------------
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        query = "/crypto/sync?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = client.get(query, headers=token_headers).json()
        pages += 1
        assert set(page) == PLAID_KEYS
        assert page["modified"] == []
        assert len(page["next_cursor"]) == 20
        seen.extend(txn["transaction_id"] for txn in page["added"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
        assert pages < 10, "paging did not converge"

    assert pages == 2
    assert seen == [_txn(index, "acct_eth").id for index in (1, 2, 3)]
    assert cursor == "00000000000000000003"

    # --- POST /webhooks/endpoints -------------------------------------
    registered = client.post(
        "/webhooks/endpoints", json={"url": HOOK_URL}, headers=key_headers
    )
    assert registered.status_code == 201
    secret = registered.json()["secret"]
    assert len(secret) == 64
    assert "secret" not in client.get(
        "/webhooks/endpoints", headers=key_headers
    ).json()["endpoints"][0]

    # --- a second connection queues connection.created ----------------
    clock.advance(1_000)
    queued_at = clock.now_ms()
    second = client.post(
        "/connections",
        json={"kind": "address", "descriptor": ADDRESSES[1]},
        headers=token_headers,
    )
    assert second.status_code == 201

    deliveries = client.get("/webhooks/deliveries", headers=key_headers).json()
    assert deliveries["count"] == 1
    assert deliveries["deliveries"][0]["event_name"] == "connection.created"
    assert deliveries["deliveries"][0]["status"] == "pending"
    assert deliveries["deliveries"][0]["next_attempt_at_ms"] == queued_at

    # --- one tick against a 200 receiver ------------------------------
    deliverer, recorder = _deliverer(webhooks, 200)
    updated = deliverer.tick(queued_at)

    assert len(recorder.requests) == 1, "exactly one POST"
    sent = recorder.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == HOOK_URL
    assert sent.headers["X-Auradefi-Timestamp"] == str(queued_at)
    verify_signature(
        secret,
        queued_at,
        sent.content.decode("utf-8"),
        sent.headers["X-Auradefi-Signature"],
        queued_at,
    )
    assert updated[0].status.value == "delivered"
    assert deliverer.tick(queued_at) == (), "a delivered row is never re-sent"

    # --- a fresh event against a 500 receiver: the pinned schedule ----
    clock.advance(1_000)
    born_at = clock.now_ms()
    third = client.post(
        "/connections",
        json={"kind": "address", "descriptor": ADDRESSES[2]},
        headers=token_headers,
    )
    assert third.status_code == 201

    failing, failed_recorder = _deliverer(webhooks, 500)
    expected_next = [born_at + offset for offset in RETRY_SCHEDULE_MS[1:]] + [None]
    for attempt, offset in enumerate(RETRY_SCHEDULE_MS):
        (row,) = failing.tick(born_at + offset)
        assert row.attempts == attempt + 1
        assert row.next_attempt_at_ms == expected_next[attempt], f"attempt {attempt}"
        assert row.last_status_code == 500
    assert len(failed_recorder.requests) == 6
    assert row.status.value == "dead_letter"

    letters = client.get("/webhooks/dead_letter", headers=key_headers).json()
    assert letters["count"] == 1
    assert letters["deliveries"][0]["id"] == row.id
    assert letters["deliveries"][0]["attempts"] == 6
    assert letters["deliveries"][0]["next_attempt_at_ms"] is None

    # --- replay, then one more tick against a 200 receiver ------------
    clock.advance(1_000)
    replayed_at = clock.now_ms()
    replayed = client.post(
        f"/webhooks/deliveries/{row.id}/replay", headers=key_headers
    )
    assert replayed.status_code == 202
    assert replayed.json()["id"].startswith("dlv_")
    assert replayed.json()["id"] != row.id
    assert replayed.json()["replay_ordinal"] == 1
    assert replayed.json()["status"] == "pending"

    healthy, healthy_recorder = _deliverer(webhooks, 200)
    (settled,) = healthy.tick(replayed_at)

    assert len(healthy_recorder.requests) == 1
    assert settled.id == replayed.json()["id"]
    assert settled.status.value == "delivered"
    assert settled.delivered_at_ms == replayed_at

    final = client.get("/webhooks/deliveries", headers=key_headers).json()
    assert [row["status"] for row in final["deliveries"]] == [
        "delivered",
        "dead_letter",
        "delivered",
    ]
