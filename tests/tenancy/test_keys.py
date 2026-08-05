"""Scoped API keys: issue, hash-at-rest, authenticate, rotate, revoke
(SPEC §7.2; format pinned in docs/DECISIONS.md "API key format").

The golden literals below were derived INDEPENDENTLY via ``python3 -c``
implementing the pinned algorithm: plaintext ``f"adk_{env}_{body}"`` with
``body = entropy(24)`` (48 lowercase hex chars, total length 57), stored
``prefix = plaintext[:17]``, stored ``secret_hash =
sha256(plaintext.encode("utf-8")).hexdigest()``. They are hardcoded on
purpose: a stability contract is a literal, not a call to the function
under test.

RELEASE_0.1.1 §4 adds the regression sections at the bottom of this file
(#25a/b/c, #35 store half). #25c makes ``revoke``/``rotate`` tenant-gated,
so both take a ``project_id``; every call here passes it BY KEYWORD, which
leaves the parameter order to the implementer and pins only the name.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from auradefi.clock import FrozenClock
from auradefi.errors import (
    AuradefiError,
    AuthError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from auradefi.tenancy.keys import ApiKeyStore, has_scope
from auradefi.tenancy.models import ApiKey, Environment, Scope

T0 = 1_767_225_600_000
HOUR_MS = 3_600_000
# Shaped like a real key id (models.new_key_id: "key_" + 16 hex) but issued to
# nobody — the control for #25c's "no tenant-existence probe".
ABSENT_KEY_ID = "key_0000000000000000"

# entropy = lambda n: "ab" * n  →  body = "ab" * 24, id body = "ab" * 8.
AB_BODY = "ab" * 24
GOLDEN_PLAINTEXT = "adk_live_" + AB_BODY  # len 57
GOLDEN_PREFIX = "adk_live_abababab"
# sha256("adk_live_" + "ab"*24) — derived independently, see docstring.
GOLDEN_HASH = "e999e9581210f205589589b9dafd81107a717c70d9d704940f2f02e037566dd1"
GOLDEN_KEY_ID = "key_" + "ab" * 8

GOLDEN_TEST_PLAINTEXT = "adk_test_" + AB_BODY
GOLDEN_TEST_PREFIX = "adk_test_abababab"
# sha256("adk_test_" + "ab"*24) — derived independently.
GOLDEN_TEST_HASH = "1db06c2d577187f1bb5ec54cf95cfe2353280e5053de4f635dac043debfd87db"


def _ab_entropy(n: int) -> str:
    return "ab" * n


def _issue(
    store: ApiKeyStore | None = None,
    project_id: str = "proj_a",
    environment: Environment = Environment.LIVE,
    scopes: tuple[Scope, ...] = (Scope.ACCOUNTS_READ,),
    now_ms: int = T0,
) -> tuple[ApiKeyStore, ApiKey, str]:
    if store is None:
        store = ApiKeyStore()
    record, plaintext = store.issue(
        project_id, environment, list(scopes), FrozenClock(now_ms)
    )
    return store, record, plaintext


def _record_by_id(store: ApiKeyStore, project_id: str, key_id: str) -> ApiKey:
    matches = [key for key in store.keys_for(project_id) if key.id == key_id]
    assert len(matches) == 1
    return matches[0]


def _rotate(
    store: ApiKeyStore,
    key_id: str,
    *,
    project_id: str = "proj_a",
    overlap_ms: int = HOUR_MS,
    now_ms: int = T0,
) -> tuple[ApiKey, str]:
    """``rotate``, tenant-gated (#25c), bound BY KEYWORD — never by position.

    Every argument is passed by name so this file pins the parameter
    *names* and leaves their order to the implementer.
    """
    return store.rotate(
        project_id=project_id,
        key_id=key_id,
        overlap_ms=overlap_ms,
        clock=FrozenClock(now_ms),
    )


# --- issue: the pinned wire format (golden vectors) ----------------------------


def test_issue_golden_plaintext_byte_for_byte():
    _, _, plaintext = _issue(ApiKeyStore(entropy=_ab_entropy))
    assert plaintext == GOLDEN_PLAINTEXT
    assert len(plaintext) == 57


def test_issue_golden_record_fields():
    _, record, plaintext = _issue(ApiKeyStore(entropy=_ab_entropy))
    assert record.id == GOLDEN_KEY_ID
    assert record.project_id == "proj_a"
    assert record.environment is Environment.LIVE
    assert record.prefix == GOLDEN_PREFIX
    assert record.prefix == plaintext[:17]
    assert record.secret_hash == GOLDEN_HASH
    assert record.scopes == frozenset({Scope.ACCOUNTS_READ})
    assert isinstance(record.scopes, frozenset)
    assert record.created_at == T0
    assert record.expires_at is None
    assert record.revoked_at is None


def test_issue_golden_test_environment_key():
    _, record, plaintext = _issue(
        ApiKeyStore(entropy=_ab_entropy), environment=Environment.TEST
    )
    assert plaintext == GOLDEN_TEST_PLAINTEXT
    assert len(plaintext) == 57
    assert record.prefix == GOLDEN_TEST_PREFIX
    assert record.secret_hash == GOLDEN_TEST_HASH
    assert record.environment is Environment.TEST


def test_default_entropy_issues_the_pinned_shape():
    _, record, plaintext = _issue()
    assert re.fullmatch(r"adk_live_[0-9a-f]{48}", plaintext)
    assert len(plaintext) == 57
    assert re.fullmatch(r"key_[0-9a-f]{16}", record.id)
    assert record.prefix == plaintext[:17]
    assert re.fullmatch(r"[0-9a-f]{64}", record.secret_hash)


def test_two_issues_yield_distinct_ids_and_plaintexts():
    store, first_record, first_plaintext = _issue()
    _, second_record, second_plaintext = _issue(store)
    assert first_record.id != second_record.id
    assert first_plaintext != second_plaintext


# --- hashed at rest: the plaintext is never stored ------------------------------


def test_no_stored_attribute_equals_the_plaintext():
    _, record, plaintext = _issue(ApiKeyStore(entropy=_ab_entropy))
    for field in dataclasses.fields(record):
        assert getattr(record, field.name) != plaintext, field.name


def test_repr_does_not_contain_the_48_char_body():
    _, record, _ = _issue(ApiKeyStore(entropy=_ab_entropy))
    assert AB_BODY not in repr(record)


# --- authenticate: happy path and indistinguishable rejection -------------------


def test_authenticate_returns_the_issued_record():
    store, record, plaintext = _issue()
    assert store.authenticate(plaintext, FrozenClock(T0)) == record


def test_wrong_last_char_raises_plain_autherror():
    store, _, plaintext = _issue(ApiKeyStore(entropy=_ab_entropy))
    with pytest.raises(AuthError) as excinfo:
        store.authenticate(plaintext[:-1] + "c", FrozenClock(T0))
    assert type(excinfo.value) is AuthError


def test_unknown_key_with_valid_shape_raises_plain_autherror():
    store, _, _ = _issue(ApiKeyStore(entropy=_ab_entropy))
    with pytest.raises(AuthError) as excinfo:
        store.authenticate("adk_live_" + "00" * 24, FrozenClock(T0))
    assert type(excinfo.value) is AuthError


@pytest.mark.parametrize(
    "junk",
    [
        "garbage",
        "",
        "adk_live_",
        GOLDEN_PLAINTEXT + "ab",
        GOLDEN_PLAINTEXT.upper(),
    ],
)
def test_garbage_raises_plain_autherror(junk):
    store, _, _ = _issue(ApiKeyStore(entropy=_ab_entropy))
    with pytest.raises(AuthError) as excinfo:
        store.authenticate(junk, FrozenClock(T0))
    assert type(excinfo.value) is AuthError


# --- revoke: immediate, idempotent ----------------------------------------------


def test_revoked_key_fails_authentication_immediately():
    store, record, plaintext = _issue()
    store.revoke(project_id="proj_a", key_id=record.id, clock=FrozenClock(T0))
    with pytest.raises(AuthError) as excinfo:
        store.authenticate(plaintext, FrozenClock(T0))
    assert type(excinfo.value) is AuthError


def test_revoke_sets_revoked_at_from_the_clock():
    store, record, _ = _issue()
    store.revoke(project_id="proj_a", key_id=record.id, clock=FrozenClock(T0 + 5_000))
    assert _record_by_id(store, "proj_a", record.id).revoked_at == T0 + 5_000


def test_second_revoke_is_a_noop():
    store, record, _ = _issue()
    store.revoke(project_id="proj_a", key_id=record.id, clock=FrozenClock(T0 + 5_000))
    store.revoke(project_id="proj_a", key_id=record.id, clock=FrozenClock(T0 + 9_000))
    assert _record_by_id(store, "proj_a", record.id).revoked_at == T0 + 5_000


# --- rotate: overlap window, clock-driven ----------------------------------------


def test_rotate_sets_old_expiry_to_now_plus_overlap():
    store, old_record, _ = _issue()
    _rotate(store, old_record.id)
    assert _record_by_id(store, "proj_a", old_record.id).expires_at == T0 + HOUR_MS


def test_both_plaintexts_authenticate_during_the_overlap():
    store, old_record, old_plaintext = _issue()
    _, new_plaintext = _rotate(store, old_record.id)
    last_live_ms = FrozenClock(T0 + HOUR_MS - 1)
    assert store.authenticate(old_plaintext, last_live_ms).id == old_record.id
    assert store.authenticate(new_plaintext, last_live_ms) is not None


def test_after_the_overlap_only_the_new_key_authenticates():
    store, old_record, old_plaintext = _issue()
    new_record, new_plaintext = _rotate(store, old_record.id)
    at_expiry = FrozenClock(T0 + HOUR_MS)  # now_ms >= expires_at: exclusive
    with pytest.raises(AuthError) as excinfo:
        store.authenticate(old_plaintext, at_expiry)
    assert type(excinfo.value) is AuthError
    assert store.authenticate(new_plaintext, at_expiry).id == new_record.id


def test_rotated_key_shares_project_env_scopes_with_new_id_and_plaintext():
    store, old_record, old_plaintext = _issue(
        scopes=(Scope.ACCOUNTS_READ, Scope.SYNC_TRIGGER)
    )
    new_record, new_plaintext = _rotate(store, old_record.id, now_ms=T0 + 1_000)
    assert new_record.project_id == old_record.project_id
    assert new_record.environment is old_record.environment
    assert new_record.scopes == old_record.scopes
    assert new_record.id != old_record.id
    assert new_plaintext != old_plaintext
    assert new_record.created_at == T0 + 1_000
    assert new_record.expires_at is None
    assert new_record.revoked_at is None


def test_zero_overlap_expires_the_old_key_at_rotation_time():
    store, old_record, old_plaintext = _issue()
    _, new_plaintext = _rotate(store, old_record.id, overlap_ms=0)
    with pytest.raises(AuthError):
        store.authenticate(old_plaintext, FrozenClock(T0))
    assert store.authenticate(new_plaintext, FrozenClock(T0)) is not None


def test_rotate_unknown_key_id_raises_not_found():
    store, _, _ = _issue()
    with pytest.raises(NotFoundError):
        _rotate(store, "key_missing")


def test_rotation_keeps_both_records_listed_for_the_project():
    store, old_record, _ = _issue()
    new_record, _ = _rotate(store, old_record.id)
    listed_ids = {key.id for key in store.keys_for("proj_a")}
    assert listed_ids == {old_record.id, new_record.id}


# --- keys_for: project-scoped ----------------------------------------------------


def test_keys_for_excludes_the_other_projects_keys():
    store, record_a, _ = _issue(project_id="proj_a")
    _, record_b, _ = _issue(store, project_id="proj_b")
    listed = store.keys_for("proj_a")
    assert isinstance(listed, tuple)
    assert [key.id for key in listed] == [record_a.id]
    assert [key.id for key in store.keys_for("proj_b")] == [record_b.id]


def test_keys_for_unknown_project_is_an_empty_tuple():
    store, _, _ = _issue(project_id="proj_a")
    assert store.keys_for("proj_nobody") == ()


# --- has_scope --------------------------------------------------------------------


def test_has_scope_true_for_a_granted_scope():
    _, record, _ = _issue(scopes=(Scope.ACCOUNTS_READ,))
    assert has_scope(record, Scope.ACCOUNTS_READ) is True


def test_has_scope_false_for_an_ungranted_scope():
    _, record, _ = _issue(scopes=(Scope.ACCOUNTS_READ,))
    assert has_scope(record, Scope.USERS_ADMIN) is False


# ==============================================================================
# RELEASE_0.1.1 §4 #25a — rotate() must never revive a dead key
#
# The shipped rotate() looks the id up and mints unconditionally, so rotating a
# key an operator DELIBERATELY REVOKED hands back a live key carrying the dead
# key's project, environment and FULL SCOPE SET. A bulk rotation job silently
# re-privileges it. Refusal is ``errors.ConflictError``: the key id is real and
# owned by the caller (so not NotFoundError) and the caller's own credential
# authenticated fine (so not AuthError) — what fails is a PRECONDITION on
# existing state, which is what ConflictError names, and which api/errors.py
# already renders as 409.
# ==============================================================================


# pins: rotating a revoked key mints NO replacement — the revoked key's scopes
#       are never carried onto a fresh live key.
def test_rotating_a_revoked_key_mints_no_replacement():
    store, record, _ = _issue(scopes=(Scope.ACCOUNTS_READ, Scope.USERS_ADMIN))
    store.revoke(project_id="proj_a", key_id=record.id, clock=FrozenClock(T0))
    revived: ApiKey | None = None
    try:
        revived, _ = _rotate(store, record.id, now_ms=T0 + 1_000)
    except AuradefiError:
        pass  # the class is pinned by the next test, not this one
    assert revived is None, (
        f"rotate revived revoked key {record.id} as live key "
        f"{revived.id} carrying {sorted(revived.scopes)}"
    )
    assert {key.id for key in store.keys_for("proj_a")} == {record.id}


# pins: the refusal to rotate a REVOKED key is errors.ConflictError.
def test_rotating_a_revoked_key_raises_conflict_error():
    store, record, _ = _issue()
    store.revoke(project_id="proj_a", key_id=record.id, clock=FrozenClock(T0))
    with pytest.raises(ConflictError) as excinfo:
        _rotate(store, record.id)
    assert type(excinfo.value) is ConflictError


# pins: an EXPIRED key cannot be rotated either — this fixture reaches the
#       expiry branch, NOT the revoked one (revoked_at is asserted None).
def test_rotating_an_expired_key_raises_conflict_error():
    store, record, _ = _issue()
    _rotate(store, record.id)  # the old key now expires at T0 + HOUR_MS
    assert _record_by_id(store, "proj_a", record.id).revoked_at is None
    assert _record_by_id(store, "proj_a", record.id).expires_at == T0 + HOUR_MS
    with pytest.raises(ConflictError) as excinfo:
        _rotate(store, record.id, now_ms=T0 + HOUR_MS)
    assert type(excinfo.value) is ConflictError


# ==============================================================================
# RELEASE_0.1.1 §4 #25b — an expiry is only ever SHORTENED, never extended
# ==============================================================================


# pins: rotating a key that already expires sooner than now+overlap leaves the
#       earlier expiry standing — rotation never buys a dying key more time.
def test_rotate_does_not_extend_the_rotated_out_keys_expiry():
    store, record, _ = _issue()
    _rotate(store, record.id, overlap_ms=60_000)  # expires at T0 + 60_000
    _rotate(store, record.id, overlap_ms=HOUR_MS, now_ms=T0 + 1_000)
    actual = _record_by_id(store, "proj_a", record.id).expires_at
    assert actual == T0 + 60_000, (
        f"rotate extended the expiry from {T0 + 60_000} to {actual} "
        f"(+{actual - (T0 + 60_000)} ms)"
    )


# pins: the fresh key inherits the window it was rotated out of — asking for
#       thirty days of overlap cannot outlive the parent key's expiry.
def test_rotate_hands_the_fresh_key_the_expiry_it_inherited():
    store, record, _ = _issue()
    _rotate(store, record.id)  # the old key now expires at T0 + HOUR_MS
    fresh, _ = _rotate(
        store,
        record.id,
        overlap_ms=30 * 86_400_000,  # ask for thirty days
        now_ms=T0 + HOUR_MS - 1,  # still live: now_ms >= expires_at is exclusive
    )
    assert fresh.expires_at == T0 + HOUR_MS, (
        f"the fresh key expires at {fresh.expires_at}, but the window it "
        f"inherited ended at {T0 + HOUR_MS}"
    )


# ==============================================================================
# RELEASE_0.1.1 §4 #25c — revoke/rotate are tenant-gated, and the gate is not
# a tenant-existence probe: another project's key id answers EXACTLY as an
# id that exists nowhere (errors.NotFoundError, byte-identical message), the
# idiom tenancy/store.py already states for every tenant-scoped lookup.
# ==============================================================================


def _two_project_store() -> tuple[ApiKeyStore, ApiKey, ApiKey, str]:
    store, key_a, _ = _issue(project_id="proj_a")
    _, key_b, plaintext_b = _issue(store, project_id="proj_b")
    return store, key_a, key_b, plaintext_b


# pins: project A revoking project B's key id leaves B's key untouched and
#       still authenticating.
def test_revoke_ignores_another_projects_key():
    store, _, key_b, plaintext_b = _two_project_store()
    try:
        store.revoke(project_id="proj_a", key_id=key_b.id, clock=FrozenClock(T0))
    except AuradefiError:
        pass  # the class is pinned by the indistinguishability test below
    victim = _record_by_id(store, "proj_b", key_b.id)
    assert victim.revoked_at is None, (
        f"proj_a revoked proj_b's key {key_b.id} at {victim.revoked_at}"
    )
    assert store.authenticate(plaintext_b, FrozenClock(T0)).id == key_b.id


# pins: project A rotating project B's key id neither expires B's key nor
#       mints a replacement anywhere.
def test_rotate_ignores_another_projects_key():
    store, key_a, key_b, plaintext_b = _two_project_store()
    try:
        _rotate(store, key_b.id, project_id="proj_a")
    except AuradefiError:
        pass  # the class is pinned by the indistinguishability test below
    victim = _record_by_id(store, "proj_b", key_b.id)
    assert victim.expires_at is None, (
        f"proj_a expired proj_b's key {key_b.id} at {victim.expires_at}"
    )
    assert store.authenticate(plaintext_b, FrozenClock(T0)).id == key_b.id
    assert {key.id for key in store.keys_for("proj_a")} == {key_a.id}
    assert {key.id for key in store.keys_for("proj_b")} == {key_b.id}


# pins: revoke answers a cross-tenant key id with the SAME class and the SAME
#       message as an id that exists nowhere — no tenant-existence probe.
def test_revoke_cross_tenant_is_indistinguishable_from_an_unknown_id():
    store, _, key_b, _ = _two_project_store()
    with pytest.raises(NotFoundError) as smuggled:
        store.revoke(project_id="proj_a", key_id=key_b.id, clock=FrozenClock(T0))
    with pytest.raises(NotFoundError) as absent:
        store.revoke(
            project_id="proj_a", key_id=ABSENT_KEY_ID, clock=FrozenClock(T0)
        )
    assert type(smuggled.value) is type(absent.value)
    assert str(smuggled.value) == str(absent.value), (
        f"cross-tenant says {str(smuggled.value)!r} but an unknown id says "
        f"{str(absent.value)!r} — that difference IS the probe"
    )


# pins: rotate answers a cross-tenant key id with the SAME class and the SAME
#       message as an id that exists nowhere — no tenant-existence probe.
def test_rotate_cross_tenant_is_indistinguishable_from_an_unknown_id():
    store, _, key_b, _ = _two_project_store()
    with pytest.raises(NotFoundError) as smuggled:
        _rotate(store, key_b.id, project_id="proj_a")
    with pytest.raises(NotFoundError) as absent:
        _rotate(store, ABSENT_KEY_ID, project_id="proj_a")
    assert type(smuggled.value) is type(absent.value)
    assert str(smuggled.value) == str(absent.value), (
        f"cross-tenant says {str(smuggled.value)!r} but an unknown id says "
        f"{str(absent.value)!r} — that difference IS the probe"
    )


# ==============================================================================
# RELEASE_0.1.1 §4 #35 (store half) — issue() coerces scopes at the boundary
#
# ``frozenset(scopes)`` keeps whatever it was handed. Because Scope is a
# StrEnum, a plain "accounts:read" satisfies ``scope in key.scopes``, so such a
# key authenticates everywhere and then breaks POST /auth/token with an
# AttributeError → unformatted 500. NOTE for anyone editing these tests:
# ``record.scopes == frozenset({Scope.ACCOUNTS_READ})`` PASSES uncoerced (a
# StrEnum member hashes and compares as its value), so the pin MUST be
# isinstance — an equality assertion here would be vacuous.
# ==============================================================================


# pins: every scope a stored key holds is a Scope member, whatever form the
#       caller (or a JSON/SQL rehydration) handed the store.
@pytest.mark.parametrize(
    "given",
    [
        pytest.param(("accounts:read", "users:admin"), id="all-wire-strings"),
        pytest.param((Scope.ACCOUNTS_READ, "sync:trigger"), id="mixed"),
        pytest.param(("accounts:read", Scope.ACCOUNTS_READ), id="duplicate-forms"),
    ],
)
def test_issue_coerces_wire_string_scopes_to_scope_members(given):
    _, record, _ = _issue(scopes=given)
    offenders = sorted(
        f"{scope!r} ({type(scope).__name__})"
        for scope in record.scopes
        if not isinstance(scope, Scope)
    )
    assert not offenders, f"issue stored uncoerced scopes: {offenders}"


# pins: a coerced key answers has_scope for the Scope member matching the wire
#       string it was issued with.
def test_wire_string_issued_key_answers_has_scope():
    _, record, _ = _issue(scopes=("accounts:read",))
    assert has_scope(record, Scope.ACCOUNTS_READ) is True
    assert has_scope(record, Scope.USERS_ADMIN) is False


# pins: an unrecognised scope string is REFUSED with errors.ValidationError —
#       the store never invents a privilege it cannot name.
@pytest.mark.parametrize(
    "unknown",
    [
        pytest.param("not:a:real:scope", id="invented"),
        pytest.param("ACCOUNTS:READ", id="wrong-case"),
        pytest.param("accounts:read ", id="trailing-space"),
        pytest.param("accounts:*", id="wildcard"),
        pytest.param("accounts", id="prefix-only"),
    ],
)
def test_issue_refuses_an_unknown_scope_string(unknown):
    with pytest.raises(ValidationError) as excinfo:
        _issue(scopes=(unknown,))
    assert type(excinfo.value) is ValidationError


# pins: a refused scope leaves NO key behind — the store is not written before
#       the scopes are validated.
def test_issue_stores_nothing_when_a_scope_is_unknown():
    store = ApiKeyStore()
    kept: ApiKey | None = None
    try:
        _, kept, _ = _issue(store, scopes=(Scope.ACCOUNTS_READ, "not:a:real:scope"))
    except AuradefiError:
        pass
    assert kept is None, f"issue kept an unknown scope: {sorted(kept.scopes)}"
    assert store.keys_for("proj_a") == ()


# ==============================================================================
# RELEASE_0.1.1 §4 #25b needs a key that ALREADY expires sooner than the
# rotation window it is handed. The acceptance gate builds one through
# ``issue``, so ``issue`` takes an optional ms-epoch ``expires_at``; without it
# the gate cannot construct its fixture at all.
# ==============================================================================


# pins: issue honours an explicit ms-epoch expires_at — the key authenticates
#       up to the last live millisecond and not at it.
def test_issue_honours_an_explicit_expires_at():
    store = ApiKeyStore()
    record, plaintext = store.issue(
        "proj_a",
        Environment.LIVE,
        [Scope.ACCOUNTS_READ],
        FrozenClock(T0),
        expires_at=T0 + 60_000,
    )
    assert record.expires_at == T0 + 60_000
    assert store.authenticate(plaintext, FrozenClock(T0 + 59_999)).id == record.id
    with pytest.raises(AuthError) as excinfo:
        store.authenticate(plaintext, FrozenClock(T0 + 60_000))
    assert type(excinfo.value) is AuthError


# ==============================================================================
# The millisecond-integer boundary on issue/rotate — project rule "All
# timestamps are millisecond-epoch integers", enforced at the store boundary.
#
# The two ms arguments this store accepts (``issue(expires_at=...)`` and
# ``rotate(overlap_ms=...)``) are the only untyped-at-runtime numbers it stores.
# A ``float`` or ``str`` that gets past the boundary is stored happily and fails
# LATER, on the authentication hot path, where ``clock.now_ms() >= expires_at``
# raises ``builtins.TypeError`` — an undeclared exception class and exactly the
# unformatted 500 RELEASE_0.1.1 exists to remove. An ``expires_at`` at or before
# ``now_ms`` is refused for the mirror-image reason: ``authenticate`` treats
# ``now_ms >= expires_at`` as dead, so storing one issues a credential born
# unable to authenticate that nevertheless shows up in ``keys_for``.
#
# Every test below asserts the EXACT class and that the store is unchanged: a
# refusal must leave no key and no half-written state, which holds only while
# validation runs before any entropy is spent or anything is written.
# ==============================================================================


def _issue_with(
    store: ApiKeyStore,
    expires_at: object,
    *,
    project_id: str = "proj_a",
    now_ms: int = T0,
) -> tuple[ApiKey, str]:
    """``issue`` with an explicit — deliberately ill-typed — ``expires_at``."""
    return store.issue(
        project_id,
        Environment.LIVE,
        [Scope.ACCOUNTS_READ],
        FrozenClock(now_ms),
        expires_at=expires_at,  # type: ignore[arg-type]
    )


# pins: a non-int expires_at is REFUSED at issue with ValidationError, never
#       stored for authenticate's ``now_ms >= expires_at`` to trip over.
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("soon", id="str"),
        pytest.param(str(T0 + 60_000), id="str-of-digits"),
        pytest.param(float(T0 + 60_000), id="whole-float"),
        pytest.param(T0 + 60_000.5, id="fractional-float"),
    ],
)
def test_issue_refuses_a_non_int_expires_at(bad):
    store, existing, _ = _issue()
    with pytest.raises(ValidationError) as excinfo:
        _issue_with(store, bad)
    assert type(excinfo.value) is ValidationError
    assert store.keys_for("proj_a") == (existing,), (
        f"issue stored expires_at={bad!r} "
        f"{[key.expires_at for key in store.keys_for('proj_a')]!r}"
    )


# pins: a bool expires_at is refused as a TYPE error, not as an ordering one —
#       bool satisfies isinstance(_, int), and True/False are 1/0, so an
#       int-only check hands the caller "must be after now" and hides the fact
#       that a bool is never an instant. The message is asserted because the
#       class alone cannot discriminate the two branches here.
@pytest.mark.parametrize(
    "bad", [pytest.param(True, id="true"), pytest.param(False, id="false")]
)
def test_issue_refuses_a_bool_expires_at(bad):
    store, existing, _ = _issue()
    with pytest.raises(ValidationError) as excinfo:
        _issue_with(store, bad)
    assert type(excinfo.value) is ValidationError
    assert "integer of milliseconds" in str(excinfo.value), (
        f"expires_at={bad!r} was refused as an ordering problem, not a type "
        f"one: {str(excinfo.value)!r}"
    )
    assert store.keys_for("proj_a") == (existing,), (
        f"issue stored expires_at={bad!r} "
        f"{[key.expires_at for key in store.keys_for('proj_a')]!r}"
    )


# pins: an expires_at EXACTLY equal to now_ms is refused — authenticate reads
#       ``now_ms >= expires_at`` as dead, so that instant is not one live
#       millisecond, it is zero.
def test_issue_refuses_an_expires_at_equal_to_now():
    store, existing, _ = _issue()
    with pytest.raises(ValidationError) as excinfo:
        _issue_with(store, T0)
    assert type(excinfo.value) is ValidationError
    assert store.keys_for("proj_a") == (existing,), (
        f"issue stored a key that expires at its own created_at "
        f"{[key.expires_at for key in store.keys_for('proj_a')]!r}"
    )


# pins: an expires_at BEFORE now_ms is refused rather than issuing a credential
#       that can never authenticate yet still shows up in keys_for.
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(T0 - 1, id="one-ms-early"),
        pytest.param(T0 - HOUR_MS, id="an-hour-early"),
        pytest.param(0, id="epoch"),
        pytest.param(-1, id="before-epoch"),
    ],
)
def test_issue_refuses_an_expires_at_before_now(bad):
    store, existing, _ = _issue()
    with pytest.raises(ValidationError) as excinfo:
        _issue_with(store, bad)
    assert type(excinfo.value) is ValidationError
    assert store.keys_for("proj_a") == (existing,), (
        f"issue stored a dead-on-arrival key: expires_at={bad} < now_ms={T0}"
    )


# pins: the FIRST millisecond after now_ms is accepted — the guard refuses only
#       instants at or before now, and never widens into the live range.
def test_issue_accepts_an_expires_at_one_ms_after_now():
    store = ApiKeyStore()
    record, plaintext = _issue_with(store, T0 + 1)
    assert record.expires_at == T0 + 1
    assert store.authenticate(plaintext, FrozenClock(T0)).id == record.id
    with pytest.raises(AuthError):
        store.authenticate(plaintext, FrozenClock(T0 + 1))


# pins: a non-int overlap_ms is REFUSED at rotate with ValidationError — no
#       float or str is ever written into the ms-int expires_at field.
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(1.5, id="fractional-float"),
        pytest.param(float(HOUR_MS), id="whole-float"),
        pytest.param(str(HOUR_MS), id="str-of-digits"),
        pytest.param("an hour", id="str"),
        pytest.param(None, id="none"),
    ],
)
def test_rotate_refuses_a_non_int_overlap_ms(bad):
    store, record, _ = _issue()
    with pytest.raises(ValidationError) as excinfo:
        _rotate(store, record.id, overlap_ms=bad)
    assert type(excinfo.value) is ValidationError
    survivor = _record_by_id(store, "proj_a", record.id)
    assert survivor.expires_at is None, (
        f"rotate wrote overlap_ms={bad!r} through: expires_at="
        f"{survivor.expires_at!r} ({type(survivor.expires_at).__name__})"
    )
    assert {key.id for key in store.keys_for("proj_a")} == {record.id}


# pins: a bool overlap_ms is refused — True satisfies isinstance(_, int) and is
#       never a duration, so an int-only check would rotate with a 1 ms window.
@pytest.mark.parametrize(
    "bad", [pytest.param(True, id="true"), pytest.param(False, id="false")]
)
def test_rotate_refuses_a_bool_overlap_ms(bad):
    store, record, _ = _issue()
    with pytest.raises(ValidationError) as excinfo:
        _rotate(store, record.id, overlap_ms=bad)
    assert type(excinfo.value) is ValidationError
    survivor = _record_by_id(store, "proj_a", record.id)
    assert survivor.expires_at is None, (
        f"rotate accepted overlap_ms={bad!r}: expires_at={survivor.expires_at!r}"
    )
    assert {key.id for key in store.keys_for("proj_a")} == {record.id}


# pins: a NEGATIVE overlap_ms is refused — it would back-date the rotated-out
#       key's expiry to before its own created_at, and mint a replacement while
#       doing it.
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(-1, id="one-ms"),
        pytest.param(-HOUR_MS, id="an-hour"),
    ],
)
def test_rotate_refuses_a_negative_overlap_ms(bad):
    store, record, _ = _issue()
    with pytest.raises(ValidationError) as excinfo:
        _rotate(store, record.id, overlap_ms=bad)
    assert type(excinfo.value) is ValidationError
    survivor = _record_by_id(store, "proj_a", record.id)
    assert survivor.expires_at is None, (
        f"rotate back-dated the expiry to {survivor.expires_at} — "
        f"{T0 - survivor.expires_at if survivor.expires_at else 0} ms before now"
    )
    assert {key.id for key in store.keys_for("proj_a")} == {record.id}, (
        "rotate minted a replacement for a refused overlap_ms"
    )


# pins: an invalid overlap_ms answers IDENTICALLY for an owned, a cross-tenant
#       and an absent key id — the argument check runs BEFORE the tenant gate,
#       so refusing it cannot be used to probe for another project's key id.
@pytest.mark.parametrize(
    "bad", [pytest.param(-1, id="negative"), pytest.param(1.5, id="float")]
)
def test_rotate_refuses_overlap_ms_before_consulting_the_tenant_gate(bad):
    store, key_a, key_b, _ = _two_project_store()
    answers: dict[str, tuple[type, str]] = {}
    for label, key_id in (
        ("owned", key_a.id),
        ("cross-tenant", key_b.id),
        ("absent", ABSENT_KEY_ID),
    ):
        with pytest.raises(ValidationError) as excinfo:
            _rotate(store, key_id, project_id="proj_a", overlap_ms=bad)
        answers[label] = (type(excinfo.value), str(excinfo.value))
    assert len(set(answers.values())) == 1, (
        f"the overlap_ms refusal differs by key id: {answers} — that difference "
        f"IS the probe"
    )
