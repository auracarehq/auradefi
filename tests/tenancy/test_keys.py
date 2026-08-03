"""Scoped API keys: issue, hash-at-rest, authenticate, rotate, revoke
(SPEC §7.2; format pinned in docs/DECISIONS.md "API key format").

The golden literals below were derived INDEPENDENTLY via ``python3 -c``
implementing the pinned algorithm: plaintext ``f"adk_{env}_{body}"`` with
``body = entropy(24)`` (48 lowercase hex chars, total length 57), stored
``prefix = plaintext[:17]``, stored ``secret_hash =
sha256(plaintext.encode("utf-8")).hexdigest()``. They are hardcoded on
purpose: a stability contract is a literal, not a call to the function
under test.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from auradefi.clock import FrozenClock
from auradefi.errors import AuthError, NotFoundError
from auradefi.tenancy.keys import ApiKeyStore, has_scope
from auradefi.tenancy.models import ApiKey, Environment, Scope

T0 = 1_767_225_600_000
HOUR_MS = 3_600_000

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
    store.revoke(record.id, FrozenClock(T0))
    with pytest.raises(AuthError) as excinfo:
        store.authenticate(plaintext, FrozenClock(T0))
    assert type(excinfo.value) is AuthError


def test_revoke_sets_revoked_at_from_the_clock():
    store, record, _ = _issue()
    store.revoke(record.id, FrozenClock(T0 + 5_000))
    assert _record_by_id(store, "proj_a", record.id).revoked_at == T0 + 5_000


def test_second_revoke_is_a_noop():
    store, record, _ = _issue()
    store.revoke(record.id, FrozenClock(T0 + 5_000))
    store.revoke(record.id, FrozenClock(T0 + 9_000))
    assert _record_by_id(store, "proj_a", record.id).revoked_at == T0 + 5_000


# --- rotate: overlap window, clock-driven ----------------------------------------


def test_rotate_sets_old_expiry_to_now_plus_overlap():
    store, old_record, _ = _issue()
    store.rotate(old_record.id, HOUR_MS, FrozenClock(T0))
    assert _record_by_id(store, "proj_a", old_record.id).expires_at == T0 + HOUR_MS


def test_both_plaintexts_authenticate_during_the_overlap():
    store, old_record, old_plaintext = _issue()
    _, new_plaintext = store.rotate(old_record.id, HOUR_MS, FrozenClock(T0))
    last_live_ms = FrozenClock(T0 + HOUR_MS - 1)
    assert store.authenticate(old_plaintext, last_live_ms).id == old_record.id
    assert store.authenticate(new_plaintext, last_live_ms) is not None


def test_after_the_overlap_only_the_new_key_authenticates():
    store, old_record, old_plaintext = _issue()
    new_record, new_plaintext = store.rotate(old_record.id, HOUR_MS, FrozenClock(T0))
    at_expiry = FrozenClock(T0 + HOUR_MS)  # now_ms >= expires_at: exclusive
    with pytest.raises(AuthError) as excinfo:
        store.authenticate(old_plaintext, at_expiry)
    assert type(excinfo.value) is AuthError
    assert store.authenticate(new_plaintext, at_expiry).id == new_record.id


def test_rotated_key_shares_project_env_scopes_with_new_id_and_plaintext():
    store, old_record, old_plaintext = _issue(
        scopes=(Scope.ACCOUNTS_READ, Scope.SYNC_TRIGGER)
    )
    new_record, new_plaintext = store.rotate(
        old_record.id, HOUR_MS, FrozenClock(T0 + 1_000)
    )
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
    _, new_plaintext = store.rotate(old_record.id, 0, FrozenClock(T0))
    with pytest.raises(AuthError):
        store.authenticate(old_plaintext, FrozenClock(T0))
    assert store.authenticate(new_plaintext, FrozenClock(T0)) is not None


def test_rotate_unknown_key_id_raises_not_found():
    store, _, _ = _issue()
    with pytest.raises(NotFoundError):
        store.rotate("key_missing", HOUR_MS, FrozenClock(T0))


def test_rotation_keeps_both_records_listed_for_the_project():
    store, old_record, _ = _issue()
    new_record, _ = store.rotate(old_record.id, HOUR_MS, FrozenClock(T0))
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
