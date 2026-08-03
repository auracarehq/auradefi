"""THE PHASE 2 GATE: two tenants cannot see each other's data — with a
test that TRIES (SPEC §11 phase 2, §13 contract tests).

Every test here is an attempted leak. The world is built maximally
confusable on purpose: tenant A and tenant B use the IDENTICAL
``external_user_id`` string and the IDENTICAL connection descriptor, so
any scoping mistake — a global dict, a message that echoes the other
project, a shared signing secret, an org-scoped quota — turns a test
red. Deliberately unmirrored (tests/contract/ is mirror-exempt): this
file guards the phase, not one module.

Leak attempts, per the phase work order:
  (a) confusable two-tenant world via injected entropy + FrozenClock
  (b) cryptographic: A's token must never verify under B's secret
  (c) id smuggling: A's ids presented to B look exactly like missing ids
  (d) enumeration: no shared user ids, no cross-project list surface
  (e) audit: mints in A leave B's audit trail empty
  (f) quota: exhausting A's window must not touch B's
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from auradefi.clock import FrozenClock
from auradefi.errors import AuthError, NotFoundError, QuotaExceededError
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.models import ConnectionKind, Environment
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import verify_token

T0 = 1_767_225_600_000  # 2026-01-01T00:00:00Z
TTL_MS = 600_000
IP = "203.0.113.7"
KEY_ID = "key_" + "ef" * 8

SECRET_A = "aa" * 32
SECRET_B = "bb" * 32
HEX_DIGITS = set("0123456789abcdef")

# The SAME strings for both tenants — maximally adversarial.
EXTERNAL_ID = "host-user-1"
DESCRIPTOR = "0xAbCd000000000000000000000000000000000001"

ABSENT_CONN = "conn_0000000000000000"
ABSENT_USER = "usr_0000000000000000"

# The complete public surface of TenancyStore. Every method is scoped by
# project_id; adding ANY name to this set is a reviewed, deliberate act
# measured against this gate.
PUBLIC_SURFACE = {
    "create_organisation",
    "create_project",
    "get_or_create_user",
    "users",
    "create_connection",
    "get_connection",
    "connections",
    "mint_user_token",
}
PROJECT_SCOPED = (
    "get_or_create_user",
    "users",
    "create_connection",
    "get_connection",
    "connections",
    "mint_user_token",
)


def scripted_entropy(ids, secret_hexes):
    """Width-keyed entropy: n=8 pops ``ids``, n=32 pops ``secret_hexes``;
    other widths get deterministic filler of the correct 2n-char shape."""
    id_queue = list(ids)
    secret_queue = list(secret_hexes)

    def entropy(n: int) -> str:
        if n == 8:
            return id_queue.pop(0)
        if n == 32:
            return secret_queue.pop(0)
        return "7" * (2 * n)

    return entropy


def make_world():
    """(a) Two orgs, two projects, one user + one connection each —
    identical external ids and descriptors across the tenant boundary."""
    clock = FrozenClock(T0)
    store = TenancyStore(
        scripted_entropy(
            ids=["11" * 8, "a", "22" * 8, "b"],
            secret_hexes=[SECRET_A, SECRET_B],
        )
    )
    org_a = store.create_organisation("Org A", clock)
    proj_a = store.create_project(org_a.id, "tenant-a", Environment.LIVE, clock)
    org_b = store.create_organisation("Org B", clock)
    proj_b = store.create_project(org_b.id, "tenant-b", Environment.LIVE, clock)
    user_a = store.get_or_create_user(proj_a.id, EXTERNAL_ID, clock)
    user_b = store.get_or_create_user(proj_b.id, EXTERNAL_ID, clock)
    conn_a = store.create_connection(
        proj_a.id, user_a.id, ConnectionKind.ADDRESS, DESCRIPTOR, clock
    )
    conn_b = store.create_connection(
        proj_b.id, user_b.id, ConnectionKind.ADDRESS, DESCRIPTOR, clock
    )
    return SimpleNamespace(
        clock=clock,
        store=store,
        proj_a=proj_a,
        proj_b=proj_b,
        user_a=user_a,
        user_b=user_b,
        conn_a=conn_a,
        conn_b=conn_b,
    )


def mint(world, project_id, jti, external_user_id=EXTERNAL_ID, audit=None, quota=None):
    return world.store.mint_user_token(
        project_id,
        external_user_id,
        ["accounts:read"],
        TTL_MS,
        IP,
        KEY_ID,
        world.clock,
        audit if audit is not None else AuditLog(),
        quota=quota,
        jti=jti,
    )


# --- (a) the world itself: confusable by construction --------------------------


def test_the_two_tenants_are_maximally_confusable_yet_fully_distinct():
    world = make_world()
    # Identical inputs on both sides of the boundary...
    assert world.user_a.external_user_id == world.user_b.external_user_id
    assert world.conn_a.descriptor == world.conn_b.descriptor
    # ...and STILL nothing coincides.
    assert world.proj_a.id != world.proj_b.id
    assert world.user_a.id != world.user_b.id
    assert world.conn_a.id != world.conn_b.id


def test_each_project_has_its_own_64_hex_signing_secret():
    world = make_world()
    for project in (world.proj_a, world.proj_b):
        assert len(project.signing_secret) == 64
        assert set(project.signing_secret) <= HEX_DIGITS
    # THE isolation root: never shared.
    assert world.proj_a.signing_secret != world.proj_b.signing_secret


# --- (b) cryptographic isolation ------------------------------------------------


def test_a_token_minted_for_tenant_a_never_verifies_under_tenant_bs_secret():
    world = make_world()
    token = mint(world, world.proj_a.id, jti="ab" * 16)

    # Control: the token is genuinely valid under its OWN project secret.
    claims = verify_token(
        token, signing_secret=world.proj_a.signing_secret, clock=world.clock
    )
    assert claims.project_id == world.proj_a.id

    # The leak attempt: same token, tenant B's secret.
    with pytest.raises(AuthError) as exc_info:
        verify_token(
            token, signing_secret=world.proj_b.signing_secret, clock=world.clock
        )
    # Plain AuthError — not expired, not revoked: a forged/foreign token
    # must be indistinguishable from garbage.
    assert exc_info.type is AuthError


# --- (c) id smuggling ------------------------------------------------------------


def test_smuggling_as_connection_id_into_b_reads_exactly_like_missing():
    world = make_world()
    with pytest.raises(NotFoundError) as cross:
        world.store.get_connection(world.proj_b.id, world.conn_a.id)
    with pytest.raises(NotFoundError) as missing:
        world.store.get_connection(world.proj_b.id, ABSENT_CONN)
    # Same class — a real-elsewhere id must not be distinguishable from a
    # nonexistent one.
    assert cross.type is missing.type is NotFoundError
    # And the message never mentions tenant A's project.
    assert world.proj_a.id not in str(cross.value)
    assert world.proj_a.signing_secret not in str(cross.value)


def test_smuggling_as_user_id_into_b_reads_exactly_like_missing():
    world = make_world()
    with pytest.raises(NotFoundError) as cross:
        world.store.create_connection(
            world.proj_b.id,
            world.user_a.id,
            ConnectionKind.EXCHANGE,
            "Kraken-Main",
            world.clock,
        )
    with pytest.raises(NotFoundError) as missing:
        world.store.create_connection(
            world.proj_b.id,
            ABSENT_USER,
            ConnectionKind.EXCHANGE,
            "Kraken-Main",
            world.clock,
        )
    assert cross.type is missing.type is NotFoundError
    assert world.proj_a.id not in str(cross.value)


# --- (d) enumeration --------------------------------------------------------------


def test_identical_external_ids_enumerate_to_disjoint_user_sets():
    world = make_world()
    ids_a = {user.id for user in world.store.users(world.proj_a.id)}
    ids_b = {user.id for user in world.store.users(world.proj_b.id)}
    assert ids_a and ids_b
    assert ids_a.isdisjoint(ids_b)


def test_no_public_surface_lists_across_projects():
    world = make_world()
    public = {name for name in dir(world.store) if not name.startswith("_")}
    assert public == PUBLIC_SURFACE, (
        "TenancyStore grew public surface not measured against the "
        f"isolation gate: {sorted(public ^ PUBLIC_SURFACE)}"
    )
    # Every read/write on tenant data leads with project_id.
    for name in PROJECT_SCOPED:
        params = list(inspect.signature(getattr(world.store, name)).parameters)
        assert params[0] == "project_id", f"{name} must be project-scoped first"


# --- (e) audit isolation ------------------------------------------------------------


def test_mints_in_a_leave_bs_audit_trail_empty():
    world = make_world()
    audit = AuditLog()
    mint(world, world.proj_a.id, jti="ab" * 16, audit=audit)
    mint(world, world.proj_a.id, jti="cd" * 16, audit=audit)
    assert audit.entries(world.proj_b.id) == ()
    assert [record.seq for record in audit.entries(world.proj_a.id)] == [1, 2]


# --- (f) quota isolation -------------------------------------------------------------


def test_exhausting_as_quota_window_leaves_b_unthrottled():
    world = make_world()
    audit = AuditLog()
    quota = QuotaCounter(
        QuotaLimits(per_second=2, per_day=100, per_month=100), world.clock
    )
    # Exhaust tenant A's per-second window through the public mint path.
    mint(world, world.proj_a.id, jti="ab" * 16, audit=audit, quota=quota)
    mint(world, world.proj_a.id, jti="cd" * 16, audit=audit, quota=quota)
    with pytest.raises(QuotaExceededError):
        mint(world, world.proj_a.id, jti="ee" * 16, audit=audit, quota=quota)
    # The failed third mint was never audited.
    assert len(audit.entries(world.proj_a.id)) == 2

    # Tenant B is untouched: a bare hit succeeds...
    quota.hit(world.proj_b.id)
    # ...and so does a full mint, valid under B's own secret.
    token = mint(world, world.proj_b.id, jti="ff" * 16, audit=audit, quota=quota)
    claims = verify_token(
        token, signing_secret=world.proj_b.signing_secret, clock=world.clock
    )
    assert claims.project_id == world.proj_b.id
    assert audit.entries(world.proj_b.id)[-1].project_id == world.proj_b.id
