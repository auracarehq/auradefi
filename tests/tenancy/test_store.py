"""TenancyStore: tenant-scoped store + audited mint flow (SPEC §3.1, §7.1, §7.2).

Golden literals below were derived INDEPENDENTLY of the code under test,
via ``python3 -c`` over the algorithms pinned in docs/DECISIONS.md:

    end_user_id   = "usr_"  + sha256(f"{project_id}|{external_user_id}".encode()).hexdigest()[:16]
    connection_id = "conn_" + sha256(f"{project_id}|{end_user_id}|{kind}|{normalized}".encode()).hexdigest()[:16]
    JWT           = pinned HS256 wire form ("JWT wire form"), ms-epoch iat/exp

A stability contract is a hardcoded literal, not a call to the function
under test. The ``usr_``/``conn_`` vectors cross-check byte-for-byte with
tests/tenancy/test_models.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from auradefi.clock import FrozenClock
from auradefi.errors import (
    ConflictError,
    NotFoundError,
    QuotaExceededError,
    ValidationError,
)
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.models import ConnectionKind, EndUser, Environment
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import verify_token

STORE_PY = (
    Path(__file__).resolve().parents[2] / "src" / "auradefi" / "tenancy" / "store.py"
)

T0 = 1_767_225_600_000  # 2026-01-01T00:00:00Z — matches the quota golden era
TTL_MS = 600_000
DAY_MS = 86_400_000
JTI = "0123456789abcdef0123456789abcdef"
JTI_2 = "fedcba9876543210fedcba9876543210"
KEY_ID = "key_" + "ef" * 8
IP = "203.0.113.7"

SECRET_A = "aa" * 32  # entropy(32) → 64 hex chars — THE isolation root
SECRET_B = "bb" * 32
HEX_DIGITS = set("0123456789abcdef")

# Deterministic-id goldens (derived independently; see module docstring).
USR_A = "usr_2b67bf34d2444625"  # proj_a | host-user-1
USR_B = "usr_2414ea20b3d09c41"  # proj_b | host-user-1
USR_A2 = "usr_5e61442cb9be6a17"  # proj_a | host-user-2

MIXED_CASE_ADDRESS = "0xAbCd000000000000000000000000000000000001"
LOWERED_ADDRESS = "0xabcd000000000000000000000000000000000001"

CONN_A = "conn_eaa947a5814c21e3"  # proj_a | USR_A | address | lowered
CONN_A_X = "conn_cb1ec0d0bd5623ac"  # proj_a | USR_A | exchange | Kraken-Main
CONN_ABSENT = "conn_0000000000000000"

# mint_user_token golden: secret "aa"*32, proj_a / host-user-1,
# scopes ["accounts:read"], iat = T0, exp = T0 + 600_000, jti = JTI.
# Derived independently from the pinned JWT wire form.
GOLDEN_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJleHAiOjE3NjcyMjYyMDAwMDAsImV4dGVybmFsX3VzZXJfaWQiOiJob3N0LXVzZXItMSIs"
    "ImlhdCI6MTc2NzIyNTYwMDAwMCwianRpIjoiMDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlh"
    "YmNkZWYiLCJwcm9qZWN0X2lkIjoicHJval9hIiwic2NvcGVzIjpbImFjY291bnRzOnJlYWQi"
    "XX0.7yR_fOX9CBIlluirAMy9aM99kxkko8zvHX8o2_V-xe8"
)


def scripted_entropy(ids=(), secret_hexes=()):
    """Width-keyed entropy: n=8 draws pop ``ids``, n=32 draws pop
    ``secret_hexes`` — independent queues, so tests never depend on the
    draw order inside one store call. Other widths get deterministic
    filler of the correct 2n-hex-chars shape."""
    id_queue = list(ids)
    secret_queue = list(secret_hexes)

    def entropy(n: int) -> str:
        if n == 8:
            return id_queue.pop(0)
        if n == 32:
            return secret_queue.pop(0)
        return "7" * (2 * n)

    return entropy


def make_store(now_ms=T0):
    """One org, one project with id 'proj_a' and signing secret SECRET_A."""
    clock = FrozenClock(now_ms)
    store = TenancyStore(scripted_entropy(ids=["11" * 8, "a"], secret_hexes=[SECRET_A]))
    org = store.create_organisation("Aura Care", clock)
    project = store.create_project(org.id, "portfolio-widget", Environment.LIVE, clock)
    return store, org, project, clock


def make_two_project_store(now_ms=T0):
    """One org, two projects: 'proj_a' (SECRET_A) and 'proj_b' (SECRET_B)."""
    clock = FrozenClock(now_ms)
    store = TenancyStore(
        scripted_entropy(ids=["11" * 8, "a", "b"], secret_hexes=[SECRET_A, SECRET_B])
    )
    org = store.create_organisation("Aura Care", clock)
    proj_a = store.create_project(org.id, "widget-a", Environment.LIVE, clock)
    proj_b = store.create_project(org.id, "widget-b", Environment.LIVE, clock)
    return store, proj_a, proj_b, clock


def mint(
    store,
    clock,
    audit,
    project_id="proj_a",
    external_user_id="host-user-1",
    scopes=("accounts:read",),
    quota=None,
    jti=JTI,
):
    return store.mint_user_token(
        project_id,
        external_user_id,
        list(scopes),
        TTL_MS,
        IP,
        KEY_ID,
        clock,
        audit,
        quota=quota,
        jti=jti,
    )


class TestCreateOrganisation:
    def test_id_from_entropy_and_created_at_from_clock(self):
        clock = FrozenClock(T0)
        store = TenancyStore(scripted_entropy(ids=["11" * 8]))
        org = store.create_organisation("Aura Care", clock)
        assert org.id == "org_1111111111111111"
        assert org.name == "Aura Care"
        assert org.created_at == T0

    def test_two_organisations_get_distinct_ids(self):
        clock = FrozenClock(T0)
        store = TenancyStore(scripted_entropy(ids=["11" * 8, "22" * 8]))
        first = store.create_organisation("One", clock)
        second = store.create_organisation("Two", clock)
        assert first.id != second.id


class TestCreateProject:
    def test_golden_fields(self):
        _store, org, project, _clock = make_store()
        assert project.id == "proj_a"
        assert project.org_id == org.id
        assert project.name == "portfolio-widget"
        assert project.environment is Environment.LIVE
        assert project.signing_secret == SECRET_A
        assert project.created_at == T0

    def test_default_entropy_secret_is_64_hex_unique_per_project(self):
        clock = FrozenClock(T0)
        store = TenancyStore()
        org = store.create_organisation("Aura Care", clock)
        first = store.create_project(org.id, "one", Environment.LIVE, clock)
        second = store.create_project(org.id, "two", Environment.TEST, clock)
        for project in (first, second):
            assert len(project.signing_secret) == 64
            assert set(project.signing_secret) <= HEX_DIGITS
            assert project.id.startswith("proj_")
        # THE isolation root: never shared between projects.
        assert first.signing_secret != second.signing_secret
        assert first.id != second.id

    def test_unknown_org_raises_not_found(self):
        clock = FrozenClock(T0)
        store = TenancyStore(scripted_entropy(ids=["aa" * 8], secret_hexes=[SECRET_A]))
        with pytest.raises(NotFoundError):
            store.create_project("org_0000000000000000", "x", Environment.LIVE, clock)


class TestGetOrCreateUser:
    def test_pinned_golden_vector(self):
        store, _org, _project, clock = make_store()
        user = store.get_or_create_user("proj_a", "host-user-1", clock)
        assert user == EndUser(
            id=USR_A,
            project_id="proj_a",
            external_user_id="host-user-1",
            created_at=T0,
        )

    def test_idempotent_at_later_clock_keeps_original_created_at(self):
        store, _org, _project, clock = make_store()
        first = store.get_or_create_user("proj_a", "host-user-1", clock)
        clock.advance(DAY_MS)
        second = store.get_or_create_user("proj_a", "host-user-1", clock)
        assert second == first
        assert second.created_at == T0  # ORIGINAL time, not the later clock
        assert len(store.users("proj_a")) == 1

    def test_rejects_email_shaped_and_creates_nothing(self):
        store, _org, _project, clock = make_store()
        with pytest.raises(ValidationError):
            store.get_or_create_user("proj_a", "user@example.dev", clock)
        assert store.users("proj_a") == ()

    def test_unknown_project_raises_not_found(self):
        store, _org, _project, clock = make_store()
        with pytest.raises(NotFoundError):
            store.get_or_create_user("proj_nope", "host-user-1", clock)

    def test_distinct_external_ids_create_distinct_users(self):
        store, _org, _project, clock = make_store()
        store.get_or_create_user("proj_a", "host-user-1", clock)
        other = store.get_or_create_user("proj_a", "host-user-2", clock)
        assert other.id == USR_A2
        assert len(store.users("proj_a")) == 2


class TestUsers:
    def test_scoped_per_project_same_external_id_different_ids(self):
        store, proj_a, proj_b, clock = make_two_project_store()
        store.get_or_create_user(proj_a.id, "host-user-1", clock)
        store.get_or_create_user(proj_b.id, "host-user-1", clock)
        assert tuple(u.id for u in store.users(proj_a.id)) == (USR_A,)
        assert tuple(u.id for u in store.users(proj_b.id)) == (USR_B,)

    def test_returns_tuple_in_creation_order(self):
        store, _org, _project, clock = make_store()
        store.get_or_create_user("proj_a", "host-user-1", clock)
        store.get_or_create_user("proj_a", "host-user-2", clock)
        listing = store.users("proj_a")
        assert isinstance(listing, tuple)
        assert tuple(u.id for u in listing) == (USR_A, USR_A2)

    def test_unknown_project_raises_not_found(self):
        store, _org, _project, _clock = make_store()
        with pytest.raises(NotFoundError):
            store.users("proj_nope")

    def test_two_stores_share_no_state(self):
        store_one, _org, _project, clock = make_store()
        store_two, _org2, _project2, _clock2 = make_store()
        store_one.get_or_create_user("proj_a", "host-user-1", clock)
        assert store_two.users("proj_a") == ()


class TestCreateConnection:
    def test_pinned_golden_vector_descriptor_stored_normalized(self):
        store, _org, _project, clock = make_store()
        store.get_or_create_user("proj_a", "host-user-1", clock)
        conn = store.create_connection(
            "proj_a", USR_A, ConnectionKind.ADDRESS, MIXED_CASE_ADDRESS, clock
        )
        assert conn.id == CONN_A
        assert conn.project_id == "proj_a"
        assert conn.end_user_id == USR_A
        assert conn.kind is ConnectionKind.ADDRESS
        assert conn.descriptor == LOWERED_ADDRESS  # normalized at rest
        assert conn.created_at == T0

    def test_duplicate_case_differing_descriptor_conflicts_with_existing_id(self):
        store, _org, _project, clock = make_store()
        store.get_or_create_user("proj_a", "host-user-1", clock)
        store.create_connection(
            "proj_a", USR_A, ConnectionKind.ADDRESS, LOWERED_ADDRESS, clock
        )
        with pytest.raises(ConflictError) as exc_info:
            store.create_connection(
                "proj_a", USR_A, ConnectionKind.ADDRESS, MIXED_CASE_ADDRESS, clock
            )
        assert exc_info.value.existing_id == CONN_A
        # The conflict changed nothing.
        assert tuple(c.id for c in store.connections("proj_a", USR_A)) == (CONN_A,)

    def test_exact_duplicate_conflicts_too(self):
        store, _org, _project, clock = make_store()
        store.get_or_create_user("proj_a", "host-user-1", clock)
        store.create_connection(
            "proj_a", USR_A, ConnectionKind.ADDRESS, LOWERED_ADDRESS, clock
        )
        with pytest.raises(ConflictError) as exc_info:
            store.create_connection(
                "proj_a", USR_A, ConnectionKind.ADDRESS, LOWERED_ADDRESS, clock
            )
        assert exc_info.value.existing_id == CONN_A

    def test_different_kind_is_not_a_duplicate(self):
        store, _org, _project, clock = make_store()
        store.get_or_create_user("proj_a", "host-user-1", clock)
        store.create_connection(
            "proj_a", USR_A, ConnectionKind.ADDRESS, LOWERED_ADDRESS, clock
        )
        exchange = store.create_connection(
            "proj_a", USR_A, ConnectionKind.EXCHANGE, "Kraken-Main", clock
        )
        assert exchange.id == CONN_A_X
        assert exchange.descriptor == "Kraken-Main"  # never lowercased

    def test_user_not_in_project_raises_not_found(self):
        store, _org, _project, clock = make_store()
        with pytest.raises(NotFoundError):
            store.create_connection(
                "proj_a",
                "usr_0000000000000000",
                ConnectionKind.ADDRESS,
                LOWERED_ADDRESS,
                clock,
            )

    def test_unknown_project_raises_not_found(self):
        store, _org, _project, clock = make_store()
        with pytest.raises(NotFoundError):
            store.create_connection(
                "proj_nope", USR_A, ConnectionKind.ADDRESS, LOWERED_ADDRESS, clock
            )


class TestGetConnectionAndListing:
    def test_get_connection_round_trips(self):
        store, _org, _project, clock = make_store()
        store.get_or_create_user("proj_a", "host-user-1", clock)
        created = store.create_connection(
            "proj_a", USR_A, ConnectionKind.ADDRESS, MIXED_CASE_ADDRESS, clock
        )
        assert store.get_connection("proj_a", CONN_A) == created

    def test_absent_id_raises_not_found(self):
        store, _org, _project, _clock = make_store()
        with pytest.raises(NotFoundError):
            store.get_connection("proj_a", CONN_ABSENT)

    def test_connections_lists_in_creation_order(self):
        store, _org, _project, clock = make_store()
        store.get_or_create_user("proj_a", "host-user-1", clock)
        store.create_connection(
            "proj_a", USR_A, ConnectionKind.ADDRESS, LOWERED_ADDRESS, clock
        )
        store.create_connection(
            "proj_a", USR_A, ConnectionKind.EXCHANGE, "Kraken-Main", clock
        )
        listing = store.connections("proj_a", USR_A)
        assert isinstance(listing, tuple)
        assert tuple(c.id for c in listing) == (CONN_A, CONN_A_X)

    def test_user_with_no_connections_yields_empty_tuple(self):
        store, _org, _project, clock = make_store()
        store.get_or_create_user("proj_a", "host-user-1", clock)
        assert store.connections("proj_a", USR_A) == ()


class TestMintUserToken:
    def test_golden_token_byte_for_byte(self):
        store, _org, _project, clock = make_store()
        token = mint(store, clock, AuditLog())
        assert token == GOLDEN_TOKEN

    def test_round_trips_under_the_projects_signing_secret(self):
        store, _org, project, clock = make_store()
        token = mint(store, clock, AuditLog())
        claims = verify_token(token, signing_secret=project.signing_secret, clock=clock)
        assert claims.project_id == project.id
        assert claims.external_user_id == "host-user-1"
        assert claims.scopes == ("accounts:read",)
        assert claims.iat == T0
        assert claims.exp == T0 + TTL_MS
        assert claims.jti == JTI

    def test_scopes_sorted_and_deduplicated_on_the_wire(self):
        store, _org, project, clock = make_store()
        token = mint(
            store,
            clock,
            AuditLog(),
            scopes=["sync:trigger", "accounts:read", "accounts:read"],
        )
        claims = verify_token(token, signing_secret=project.signing_secret, clock=clock)
        assert claims.scopes == ("accounts:read", "sync:trigger")

    def test_user_exists_as_a_side_effect_of_minting(self):
        store, _org, _project, clock = make_store()
        assert store.users("proj_a") == ()
        mint(store, clock, AuditLog())
        users = store.users("proj_a")
        assert tuple(u.id for u in users) == (USR_A,)
        assert users[0].created_at == T0

    def test_repeat_mint_does_not_duplicate_the_user(self):
        store, _org, _project, clock = make_store()
        audit = AuditLog()
        mint(store, clock, audit)
        clock.advance(DAY_MS)
        mint(store, clock, audit, jti=JTI_2)
        users = store.users("proj_a")
        assert len(users) == 1
        assert users[0].created_at == T0  # original creation time survives

    def test_every_successful_mint_appends_exactly_one_audit_record(self):
        store, _org, _project, clock = make_store()
        audit = AuditLog()
        mint(store, clock, audit)
        clock.advance(5_000)
        mint(store, clock, audit, jti=JTI_2)
        records = audit.entries("proj_a")
        assert len(records) == 2
        first, second = records
        assert first.seq == 1
        assert first.event == "token.minted"
        assert first.project_id == "proj_a"
        assert first.external_user_id == "host-user-1"
        assert first.key_id == KEY_ID
        assert first.ip == IP
        assert first.at_ms == T0  # mint-time clock, exactly
        assert second.seq == 2
        assert second.at_ms == T0 + 5_000

    def test_quota_limit_zero_blocks_with_no_side_effects(self):
        store, _org, _project, clock = make_store()
        audit = AuditLog()
        quota = QuotaCounter(
            QuotaLimits(per_second=0, per_day=10, per_month=10), clock
        )
        with pytest.raises(QuotaExceededError):
            mint(store, clock, audit, quota=quota)
        assert store.users("proj_a") == ()  # nothing minted, no side-effect user
        assert audit.entries("proj_a") == ()  # failures are never audited

    def test_quota_exhaustion_blocks_the_second_user_cleanly(self):
        store, _org, _project, clock = make_store()
        audit = AuditLog()
        quota = QuotaCounter(
            QuotaLimits(per_second=1, per_day=10, per_month=10), clock
        )
        mint(store, clock, audit, quota=quota)
        with pytest.raises(QuotaExceededError):
            mint(store, clock, audit, external_user_id="host-user-2", quota=quota)
        assert tuple(u.id for u in store.users("proj_a")) == (USR_A,)
        assert len(audit.entries("proj_a")) == 1

    def test_quota_is_checked_before_validation(self):
        # Pinned order: (1) quota.hit FIRST, (2) get_or_create_user.
        store, _org, _project, clock = make_store()
        quota = QuotaCounter(
            QuotaLimits(per_second=0, per_day=10, per_month=10), clock
        )
        with pytest.raises(QuotaExceededError):
            mint(store, clock, AuditLog(), external_user_id="user@example.dev", quota=quota)

    def test_email_shaped_external_id_mints_nothing(self):
        store, _org, _project, clock = make_store()
        audit = AuditLog()
        with pytest.raises(ValidationError):
            mint(store, clock, audit, external_user_id="user@example.dev")
        assert store.users("proj_a") == ()
        assert audit.entries("proj_a") == ()

    def test_unknown_project_raises_not_found_and_audits_nothing(self):
        store, _org, _project, clock = make_store()
        audit = AuditLog()
        with pytest.raises(NotFoundError):
            mint(store, clock, audit, project_id="proj_nope")
        assert audit.entries("proj_nope") == ()


class TestSourceHygiene:
    def test_store_module_never_imports_tenancy_keys(self):
        # key_id is a passed-in datum; Phase 8 wires key auth to mint.
        tree = ast.parse(STORE_PY.read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [
                    alias.name
                    for alias in node.names
                    if "keys" in alias.name.split(".")
                ]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "keys" in module.split("."):
                    offenders.append(module)
                elif module.endswith("tenancy"):
                    offenders += [
                        f"{module}.{alias.name}"
                        for alias in node.names
                        if alias.name == "keys"
                    ]
        assert not offenders, f"store.py must not import tenancy.keys: {offenders}"
