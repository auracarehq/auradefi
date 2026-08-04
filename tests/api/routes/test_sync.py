"""api/routes/sync.py — Plaid's sync envelope and Allium's batch union.

Offline throughout. The ledger is the real ``MemoryLedger`` and the
holdings provider is a structural stand-in (``api`` may not import
``portfolio``, so neither may its tests pretend otherwise — the stand-in
returns a real ``HoldingsReport``, which is what a host would bind).

Two rules get the most attention: the ledger tenant key is the caller's
``usr_`` id and nothing coarser, and one bad address never fails a batch.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

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
from auradefi.money.fiat import Money
from auradefi.money.quantity import Quantity
from auradefi.portfolio.models import Holding, HoldingsReport
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.keys import ApiKeyStore
from auradefi.tenancy.models import Environment, Scope, end_user_id
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import RevocationSet
from auradefi.webhooks.deliver import WebhookStore

NOW = 1_754_000_000_000
ALL_SCOPES = (Scope.USERS_ADMIN, Scope.ACCOUNTS_READ, Scope.ACCOUNTS_WRITE)
ETH = "eip155:1"
ETH_ASSET = "eip155:1/slip44:60"
USDC = "eip155:1/erc20:0x1111111111111111111111111111111111111111"
ADDRESS = "0x1111111111111111111111111111111111111111"
OTHER_ADDRESS = "0x2222222222222222222222222222222222222222"
UNREGISTERED = "eip155:999999"
PLAID_KEYS = {"added", "modified", "removed", "next_cursor", "has_more"}


class _Provider:
    """A structural HoldingsProvider; records calls, fails on demand."""

    def __init__(self, unpriced_for: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, str]] = []
        self._unpriced_for = unpriced_for

    def holdings(self, chain_id: str, address: str) -> HoldingsReport:
        self.calls.append((chain_id, address))
        holdings = [
            Holding(
                caip19=ETH_ASSET,
                symbol="ETH",
                quantity=Quantity(10**18, 18),
                price=Money(Decimal("2000"), "USD"),
                value=Money(Decimal("2000"), "USD"),
            )
        ]
        if address in self._unpriced_for:
            holdings.append(
                Holding(
                    caip19=USDC,
                    symbol="USDC",
                    quantity=Quantity(5_000_000, 6),
                    price=None,
                    value=None,
                )
            )
        return HoldingsReport.assemble(address, chain_id, holdings, NOW)


def _build(limits: QuotaLimits | None = None, **overrides):
    """(deps, project, clock) over the real Phase 0-7 collaborators."""
    clock = FrozenClock(NOW)
    tenancy = TenancyStore()
    org = tenancy.create_organisation("acme", clock)
    project = tenancy.create_project(org.id, "main", Environment.TEST, clock)
    vault = {project.id: project.signing_secret}
    deps = Deps(
        tenancy=tenancy,
        keys=ApiKeyStore(),
        quota=QuotaCounter(limits or QuotaLimits(500, 5_000, 50_000), clock),
        audit=AuditLog(),
        revocations=RevocationSet(),
        ledger=MemoryLedger(),
        webhooks=WebhookStore(),
        chains=ChainRegistry(),
        clock=clock,
        signing_secret_for=vault.get,
        **overrides,
    )
    return deps, project, clock


def _bearer(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


def _token(client, plaintext, external_user_id="u-1", scopes=None):
    body = {"external_user_id": external_user_id}
    if scopes is not None:
        body["scopes"] = scopes
    response = client.post("/auth/token", json=body, headers=_bearer(plaintext))
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _txn(index: int, account: str = "acct_eth") -> LedgerTransaction:
    tx_hash = "0x" + f"{index:02x}" * 32
    return LedgerTransaction(
        id=transaction_id(ETH, tx_hash, account),
        chain_id=ETH,
        tx_hash=tx_hash,
        account_id=account,
        block_number=18_000_000 + index,
        initiated_at=1_753_000_000_000 + index,
        confirmed_at=1_753_000_000_500 + index,
        entries=(
            Entry(
                asset_id=ETH_ASSET,
                quantity=Quantity(10**18 * index, 18),
                direction=Direction.IN,
            ),
        ),
    )


@pytest.fixture
def wired():
    deps, project, clock = _build()
    _record, plaintext = deps.keys.issue(project.id, Environment.TEST, ALL_SCOPES, clock)
    client = TestClient(create_app(deps))
    tenant = end_user_id(project.id, "u-1")
    deps.ledger.upsert(tenant, [_txn(1), _txn(2), _txn(3)])
    return client, deps, project, plaintext, tenant


# --------------------------------------------------------------------------
# GET /crypto/sync


def test_sync_pages_in_last_modified_order(wired):
    client, _deps, _project, plaintext, _tenant = wired
    token = _token(client, plaintext)

    first = client.get("/crypto/sync?limit=2", headers=_bearer(token))
    assert first.status_code == 200
    page = first.json()
    assert set(page) == PLAID_KEYS
    assert page["has_more"] is True
    assert page["modified"] == []
    assert page["removed"] == []
    assert len(page["added"]) == 2
    assert len(page["next_cursor"]) == 20 and page["next_cursor"].isdigit()
    assert page["next_cursor"] == "00000000000000000002"
    assert [txn["tx_hash"] for txn in page["added"]] == [
        "0x" + "01" * 32,
        "0x" + "02" * 32,
    ]

    second = client.get(
        f"/crypto/sync?limit=2&cursor={page['next_cursor']}", headers=_bearer(token)
    ).json()
    assert second["has_more"] is False
    assert len(second["added"]) == 1
    assert second["next_cursor"] == "00000000000000000003"
    assert second["modified"] == []


def test_sync_defaults_to_the_configured_limit(wired):
    client, _deps, _project, plaintext, _tenant = wired
    token = _token(client, plaintext)
    page = client.get("/crypto/sync", headers=_bearer(token)).json()
    assert len(page["added"]) == 3
    assert page["has_more"] is False


def test_a_removed_transaction_carries_exactly_two_keys(wired):
    client, deps, _project, plaintext, tenant = wired
    token = _token(client, plaintext)
    removed_id = transaction_id(ETH, "0x" + "01" * 32, "acct_eth")
    deps.ledger.mark_removed(tenant, [removed_id])

    page = client.get("/crypto/sync", headers=_bearer(token)).json()

    assert len(page["removed"]) == 1
    assert set(page["removed"][0]) == {"transaction_id", "account_id"}
    assert page["removed"][0]["transaction_id"] == removed_id
    assert page["removed"][0]["account_id"] == "acct_eth"


def test_the_ledger_tenant_key_is_the_callers_end_user_id(wired):
    client, _deps, _project, plaintext, _tenant = wired
    mine = _token(client, plaintext)
    theirs = _token(client, plaintext, external_user_id="u-2")

    assert len(client.get("/crypto/sync", headers=_bearer(mine)).json()["added"]) == 3
    other = client.get("/crypto/sync", headers=_bearer(theirs)).json()
    assert other["added"] == []
    assert other["has_more"] is False


def test_a_malformed_cursor_is_422_not_500(wired):
    client, _deps, _project, plaintext, _tenant = wired
    token = _token(client, plaintext)
    response = client.get("/crypto/sync?cursor=not-a-cursor", headers=_bearer(token))
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "CursorError"


@pytest.mark.parametrize("limit", [0, 501, -1])
def test_a_limit_outside_the_cap_is_422_naming_the_cap(wired, limit):
    client, deps, _project, plaintext, _tenant = wired
    token = _token(client, plaintext)
    response = client.get(f"/crypto/sync?limit={limit}", headers=_bearer(token))
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "ValidationError"
    assert str(deps.sync_limit_max) in response.json()["error"]["message"]


def test_sync_requires_accounts_read(wired):
    client, _deps, _project, plaintext, _tenant = wired
    token = _token(client, plaintext, scopes=["users:admin"])
    assert client.get("/crypto/sync", headers=_bearer(token)).status_code == 403


# --------------------------------------------------------------------------
# POST /batch/holdings


def _batch(client, plaintext, pairs):
    return client.post(
        "/batch/holdings",
        json={"items": [{"chain": chain, "address": address} for chain, address in pairs]},
        headers=_bearer(plaintext),
    )


@pytest.fixture
def batch_wired():
    provider = _Provider(unpriced_for=(OTHER_ADDRESS,))
    deps, project, clock = _build(holdings=provider)
    _record, plaintext = deps.keys.issue(project.id, Environment.TEST, ALL_SCOPES, clock)
    return TestClient(create_app(deps)), deps, plaintext, provider


def test_batch_is_absent_when_the_deployment_has_no_holdings_provider():
    deps, project, clock = _build()
    _record, plaintext = deps.keys.issue(project.id, Environment.TEST, ALL_SCOPES, clock)
    response = _batch(TestClient(create_app(deps)), plaintext, [(ETH, ADDRESS)])
    assert response.status_code == 404


def test_one_bad_chain_never_fails_the_batch(batch_wired):
    client, _deps, plaintext, provider = batch_wired
    response = _batch(
        client,
        plaintext,
        [(ETH, ADDRESS), (UNREGISTERED, ADDRESS), (ETH, OTHER_ADDRESS)],
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "warnings"}
    items = body["items"]
    assert len(items) == 3, "items are never dropped, deduped or reordered"
    assert [item["status"] for item in items] == ["ok", "error", "ok"]
    assert [(item["chain"], item["address"]) for item in items] == [
        (ETH, ADDRESS),
        (UNREGISTERED, ADDRESS),
        (ETH, OTHER_ADDRESS),
    ]
    assert items[1]["error"]["type"] == "UnknownChainError"
    assert "result" not in items[1]
    assert "error" not in items[0]
    assert items[0]["result"]["total_value"] == {"amount": "2000", "currency": "USD"}
    assert items[0]["result"]["unpriced"] == []
    assert provider.calls == [(ETH, ADDRESS), (ETH, OTHER_ADDRESS)]


def test_unpriced_assets_raise_a_warning(batch_wired):
    client, _deps, plaintext, _provider = batch_wired
    body = _batch(client, plaintext, [(ETH, OTHER_ADDRESS)]).json()
    assert body["items"][0]["result"]["unpriced"] == [USDC]
    assert [warning["code"] for warning in body["warnings"]] == ["unpriced_assets"]
    assert body["warnings"][0]["chain"] == ETH
    assert body["warnings"][0]["address"] == OTHER_ADDRESS
    assert set(body["warnings"][0]) == {"code", "message", "chain", "address"}


def test_a_repeated_pair_warns_and_is_still_answered_twice(batch_wired):
    client, _deps, plaintext, provider = batch_wired
    body = _batch(client, plaintext, [(ETH, ADDRESS), (ETH, ADDRESS)]).json()

    assert len(body["items"]) == 2
    assert body["items"][0] == body["items"][1]
    assert [warning["code"] for warning in body["warnings"]] == ["duplicate_pair"]
    assert body["warnings"][0]["chain"] == ETH
    assert provider.calls == [(ETH, ADDRESS), (ETH, ADDRESS)], "billed by work done"


@pytest.mark.parametrize("count", [0, 101])
def test_an_out_of_range_item_count_is_422(batch_wired, count):
    client, deps, plaintext, provider = batch_wired
    response = _batch(client, plaintext, [(ETH, ADDRESS)] * count)
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "ValidationError"
    assert str(deps.batch_max_items) in response.json()["error"]["message"]
    assert provider.calls == [], "the cap is checked before any work"


def test_the_maximum_item_count_is_accepted(batch_wired):
    client, deps, plaintext, _provider = batch_wired
    response = _batch(client, plaintext, [(ETH, ADDRESS)] * deps.batch_max_items)
    assert response.status_code == 200
    assert len(response.json()["items"]) == deps.batch_max_items


def test_quota_exhausted_mid_batch_is_still_200():
    provider = _Provider()
    deps, project, clock = _build(limits=QuotaLimits(2, 500, 5_000), holdings=provider)
    _record, plaintext = deps.keys.issue(project.id, Environment.TEST, ALL_SCOPES, clock)
    client = TestClient(create_app(deps))

    response = _batch(client, plaintext, [(ETH, ADDRESS)] * 3)

    assert response.status_code == 200
    body = response.json()
    assert [item["status"] for item in body["items"]] == ["ok", "ok", "error"]
    assert body["items"][2]["error"]["type"] == "QuotaExceededError"
    codes = [warning["code"] for warning in body["warnings"]]
    assert codes.count("quota_exhausted") == 1, "one warning, not one per refused item"
    assert len(provider.calls) == 2, "no work is done for a refused item"


def test_a_batch_refused_on_its_first_item_is_a_429():
    provider = _Provider()
    deps, project, clock = _build(limits=QuotaLimits(0, 500, 5_000), holdings=provider)
    _record, plaintext = deps.keys.issue(project.id, Environment.TEST, ALL_SCOPES, clock)
    client = TestClient(create_app(deps))

    response = _batch(client, plaintext, [(ETH, ADDRESS)] * 3)

    assert response.status_code == 429
    assert response.json()["error"]["type"] == "QuotaExceededError"
    assert response.headers["Retry-After"] == "1"
    assert provider.calls == []


def test_batch_requires_an_api_key_with_accounts_read():
    provider = _Provider()
    deps, project, clock = _build(holdings=provider)
    _record, plaintext = deps.keys.issue(
        project.id, Environment.TEST, (Scope.USERS_ADMIN,), clock
    )
    client = TestClient(create_app(deps))
    assert _batch(client, plaintext, [(ETH, ADDRESS)]).status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"items": [{"chain": ETH}]},
        {"items": [{"chain": ETH, "address": ADDRESS, "tag": "x"}]},
        {"pairs": [{"chain": ETH, "address": ADDRESS}]},
    ],
)
def test_a_bad_batch_body_is_422(batch_wired, body):
    client, _deps, plaintext, _provider = batch_wired
    response = client.post("/batch/holdings", json=body, headers=_bearer(plaintext))
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "ValidationError"
