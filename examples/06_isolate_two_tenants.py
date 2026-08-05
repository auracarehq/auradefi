"""How do I serve many customers from one deployment without leaking?

    pip install auradefi
    python examples/06_isolate_two_tenants.py

Multi-tenancy here is not a `WHERE` clause you must remember to write. The
hierarchy is organisation -> project -> end user, and the tenant key is
*derived*: `usr_…` is a hash over `project_id | external_user_id`. Two
projects using the identical customer id, "user-1", say, cannot collide,
because the project id is inside the hash.

This file sets up two projects that are as similar as possible, same
customer id, same wallet address, and then attacks the boundary between
them four ways:

    1. replay project A's user token against project B  -> refused (signature)
    2. use a token beyond the scopes it was minted with -> refused
    3. use a token one millisecond after it expires     -> refused
    4. read the other project's audit log               -> empty

Then it shows what a caller legitimately gets: scoped keys, a short-lived
token, and three quota windows they can see the state of.
"""

from __future__ import annotations

from auradefi.clock import FrozenClock
from auradefi.errors import AuthError, QuotaExceededError, ScopeError
from auradefi.tenancy.audit import AuditLog
from auradefi.tenancy.keys import ApiKeyStore
from auradefi.tenancy.models import ConnectionKind, Environment, Scope, end_user_id
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
from auradefi.tenancy.store import TenancyStore
from auradefi.tenancy.tokens import require_scope, verify_token

CUSTOMER = "user-1"                 # the SAME id in both projects
WALLET = "0x1111111111111111111111111111111111111111"

clock = FrozenClock(1_767_225_600_000)
tenancy = TenancyStore()
keys = ApiKeyStore()
audit = AuditLog()

# ------------------------------------------------------ 1. two tenants
organisation = tenancy.create_organisation("Acme", clock)
alpha = tenancy.create_project(organisation.id, "alpha", Environment.LIVE, clock)
beta = tenancy.create_project(organisation.id, "beta", Environment.LIVE, clock)

alpha_user = tenancy.get_or_create_user(alpha.id, CUSTOMER, clock)
beta_user = tenancy.get_or_create_user(beta.id, CUSTOMER, clock)

# Get-or-create really is: the same external id gives the same row back.
assert tenancy.get_or_create_user(alpha.id, CUSTOMER, clock).id == alpha_user.id
# Same customer id, same everything else: different tenant, by derivation.
assert alpha_user.id != beta_user.id
assert alpha_user.id == end_user_id(alpha.id, CUSTOMER)
print(f"customer {CUSTOMER!r} in two projects:")
print(f"  {alpha.id} -> {alpha_user.id}")
print(f"  {beta.id} -> {beta_user.id}")
print("  the project id is INSIDE the hash, so the ids cannot collide")

# Both connect the same wallet. Both connections are real, and distinct.
alpha_connection = tenancy.create_connection(
    alpha.id, alpha_user.id, ConnectionKind.ADDRESS, WALLET, clock)
beta_connection = tenancy.create_connection(
    beta.id, beta_user.id, ConnectionKind.ADDRESS, WALLET, clock)
assert alpha_connection.id != beta_connection.id
print(f"\nthe same wallet connected in both: {alpha_connection.id} vs "
      f"{beta_connection.id}")

# ------------------------------------------ 2. keys are scoped, and per project
alpha_key, alpha_secret = keys.issue(
    alpha.id, Environment.LIVE, (Scope.USERS_ADMIN, Scope.ACCOUNTS_READ), clock)
beta_key, beta_secret = keys.issue(
    beta.id, Environment.LIVE, (Scope.ACCOUNTS_READ,), clock)

# The secret is shown once. Only its hash is stored, so a database dump is
# not a set of working credentials.
assert alpha_secret.startswith("adk_live_") and len(alpha_secret) == 57
assert alpha_secret not in repr(alpha_key)
authenticated = keys.authenticate(alpha_secret, clock)
assert authenticated.project_id == alpha.id
print(f"\nkey {alpha_secret[:13]}… authenticates to {authenticated.project_id} "
      f"with scopes {sorted(scope.value for scope in authenticated.scopes)}")

# Beta's key was never granted users:admin, so it cannot mint tokens even
# for its OWN users. Scope is checked, not assumed from possession.
beta_authenticated = keys.authenticate(beta_secret, clock)
assert Scope.USERS_ADMIN not in beta_authenticated.scopes
print(f"  beta's key holds {sorted(s.value for s in beta_authenticated.scopes)}: "
      "it cannot mint a user token at all")

# ------------------------------------------------ 3. tokens are project-signed
token = tenancy.mint_user_token(
    alpha.id, CUSTOMER, ["accounts:read"], ttl_ms=600_000,
    ip="203.0.113.7", key_id=alpha_key.id, clock=clock, audit=audit,
    ip_source="socket",
)
claims = verify_token(token, signing_secret=alpha.signing_secret, clock=clock)
assert (claims.project_id, claims.external_user_id) == (alpha.id, CUSTOMER)
print(f"\nalpha minted a token for {claims.external_user_id}: "
      f"scopes {claims.scopes}, ttl {(claims.exp - claims.iat) // 1000}s")

# ATTACK 1: replay alpha's token against beta's secret.
try:
    verify_token(token, signing_secret=beta.signing_secret, clock=clock)
    raise AssertionError("a cross-project token must never verify")
except AuthError as exc:
    print(f"  replayed at beta: {type(exc).__name__}: {exc}")

# ATTACK 2: use it beyond its scope.
try:
    require_scope(claims, "accounts:write")
    raise AssertionError("a scope not granted must never pass")
except ScopeError as exc:
    print(f"  used to write: {type(exc).__name__}: {exc}")

# ATTACK 3: use it after it expires. Time is a port, so this is testable.
expired_clock = FrozenClock(claims.exp + 1)
try:
    verify_token(token, signing_secret=alpha.signing_secret, clock=expired_clock)
    raise AssertionError("an expired token must never verify")
except AuthError as exc:
    print(f"  used 1 ms late: {type(exc).__name__}: {exc}")

# ATTACK 4: read the other project's audit trail. Every mint is recorded,
# under the project that did it, with the IP the SERVER observed: a caller
# cannot choose the address its own permanent audit row records.
(entry,) = audit.entries(alpha.id)
assert audit.entries(beta.id) == ()
assert (entry.event, entry.key_id, entry.ip) == ("token.minted", alpha_key.id, "203.0.113.7")
print(f"\naudit: alpha has {len(audit.entries(alpha.id))} entry "
      f"({entry.event} by {entry.key_id} from {entry.ip}), beta has "
      f"{len(audit.entries(beta.id))}")

# ------------------------------------------------------ 4. quota, per project
# Three windows at once. A project that burns its second does not touch its
# day, and beta is not slowed down by alpha at all.
quota = QuotaCounter(QuotaLimits(per_second=2, per_day=1_000, per_month=10_000), clock)
quota.hit(alpha.id)
quota.hit(alpha.id)
try:
    quota.hit(alpha.id)
    raise AssertionError("the third hit in one second must be refused")
except QuotaExceededError as exc:
    print(f"\nalpha's 3rd request this second: {type(exc).__name__}: {exc}")

quota.hit(beta.id)      # beta is unaffected by alpha's burst
snapshot = quota.snapshot(alpha.id)
print("  alpha's windows: " + ", ".join(
    f"{name} {window.remaining}/{window.limit} left"
    for name, window in sorted(snapshot.items())))
print(f"  beta's second:   {quota.snapshot(beta.id)['second'].remaining}/2 left: "
      "one tenant cannot spend another's budget")

clock.advance(1_000)    # a new second
quota.hit(alpha.id)
print(f"  one second later alpha is servable again: "
      f"{quota.snapshot(alpha.id)['second'].remaining}/2 left")

print("\nOK: derived tenant ids, project-signed tokens, scoped keys, "
      "per-project quota.")
