"""Golden vectors and invariants for auradefi.tenancy.models (SPEC §3.1, §7.2).

The id literals below were derived INDEPENDENTLY of the code under test,
via ``python3 -c`` over the algorithms pinned in docs/DECISIONS.md:

    end_user_id   = "usr_"  + sha256(f"{project_id}|{external_user_id}".encode()).hexdigest()[:16]
    connection_id = "conn_" + sha256(f"{project_id}|{end_user_id}|{kind}|{normalized}".encode()).hexdigest()[:16]
    normalized    = descriptor.strip(), lowercased iff kind == "address" and it startswith "0x"

A stability contract is a hardcoded string, not a call to the function
under test.
"""

from __future__ import annotations

import dataclasses
from dataclasses import FrozenInstanceError

import pytest

from auradefi.errors import ValidationError
from auradefi.tenancy.models import (
    ApiKey,
    Connection,
    ConnectionKind,
    EndUser,
    Environment,
    Organisation,
    Project,
    Scope,
    connection_id,
    end_user_id,
    new_key_id,
    new_org_id,
    new_project_id,
    normalize_descriptor,
    validate_external_user_id,
)

# Derived independently (see module docstring); NEVER regenerate from the
# implementation.
USR_PROJ_A = "usr_2b67bf34d2444625"  # proj_a | host-user-1
USR_PROJ_B = "usr_2414ea20b3d09c41"  # proj_b | host-user-1
USR_PROJ_A_USER_2 = "usr_5e61442cb9be6a17"  # proj_a | host-user-2

MIXED_CASE_ADDRESS = "0xAbCd000000000000000000000000000000000001"
LOWERED_ADDRESS = "0xabcd000000000000000000000000000000000001"
XPUB = (
    "xpub6CUGRUonZSQ4TWtTMmzXdrXDtypWKiKrhko4egpiMZbpiaQL2jkwSB1icqYh2cfDf"
    "Vxdx4df189oLKnC5fSwqPfgyP3hooxujYzAu3fDVmz"
)

CONN_ADDR = "conn_eaa947a5814c21e3"  # proj_a | USR_PROJ_A | address | lowered addr
CONN_XPUB = "conn_cf172ab449912898"  # proj_a | USR_PROJ_A | xpub | XPUB verbatim
CONN_EXCHANGE = "conn_cb1ec0d0bd5623ac"  # proj_a | USR_PROJ_A | exchange | Kraken-Main
CONN_ADDR_PROJ_B = "conn_97a57e61d0b18beb"  # proj_b | USR_PROJ_A | address | lowered addr

MS = 1_754_000_000_000  # ms-epoch, matches the repo's frozen clock era

HEX_DIGITS = set("0123456789abcdef")


def make_org(**overrides) -> Organisation:
    fields = {"id": "org_" + "ab" * 8, "name": "Aura Care", "created_at": MS}
    fields.update(overrides)
    return Organisation(**fields)


def make_project(**overrides) -> Project:
    fields = {
        "id": "proj_" + "cd" * 8,
        "org_id": "org_" + "ab" * 8,
        "name": "portfolio-widget",
        "environment": Environment.LIVE,
        "signing_secret": "f0" * 32,
        "created_at": MS,
    }
    fields.update(overrides)
    return Project(**fields)


def make_end_user(**overrides) -> EndUser:
    fields = {
        "id": USR_PROJ_A,
        "project_id": "proj_a",
        "external_user_id": "host-user-1",
        "created_at": MS,
    }
    fields.update(overrides)
    return EndUser(**fields)


def make_connection(**overrides) -> Connection:
    fields = {
        "id": CONN_ADDR,
        "project_id": "proj_a",
        "end_user_id": USR_PROJ_A,
        "kind": ConnectionKind.ADDRESS,
        "descriptor": LOWERED_ADDRESS,
        "created_at": MS,
    }
    fields.update(overrides)
    return Connection(**fields)


def make_api_key(**overrides) -> ApiKey:
    fields = {
        "id": "key_" + "ef" * 8,
        "project_id": "proj_" + "cd" * 8,
        "environment": Environment.TEST,
        "prefix": "adk_test_0123abcd",  # plaintext[:17] per DECISIONS
        "secret_hash": "9a" * 32,  # sha256 hexdigest of the plaintext
        "scopes": frozenset({Scope.ACCOUNTS_READ, Scope.SYNC_TRIGGER}),
        "created_at": MS,
    }
    fields.update(overrides)
    return ApiKey(**fields)


class TestValidateExternalUserId:
    def test_vezgos_own_openapi_example_is_rejected_by_name(self):
        # SPEC §7.2: their loginName example is literally user@example.dev.
        with pytest.raises(ValidationError):
            validate_external_user_id("user@example.dev")

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "a b",
            "x" * 129,
            "@",
            "a@b",
            "first.last@example.org",
            " host-user-1",
            "host-user-1 ",
            "host\tuser",
            "host-user-1\n",
            "usér-1",
            "user/1",
            "user#1",
        ],
    )
    def test_rejects_invalid_input(self, bad):
        with pytest.raises(ValidationError):
            validate_external_user_id(bad)

    @pytest.mark.parametrize(
        "good",
        [
            "host-user-1",
            "HOST_user.1:x",
            "a",
            "0",
            "A-Za-z0-9._:-",
            "x" * 128,
        ],
    )
    def test_valid_input_passes_unchanged(self, good):
        assert validate_external_user_id(good) == good

    def test_boundary_128_passes_129_fails(self):
        assert validate_external_user_id("y" * 128) == "y" * 128
        with pytest.raises(ValidationError):
            validate_external_user_id("y" * 129)


class TestEndUserId:
    def test_pinned_golden_vector(self):
        assert end_user_id("proj_a", "host-user-1") == USR_PROJ_A

    def test_deterministic_across_calls(self):
        first = end_user_id("proj_a", "host-user-1")
        second = end_user_id("proj_a", "host-user-1")
        assert first == second == USR_PROJ_A

    def test_same_external_id_under_other_project_differs(self):
        other = end_user_id("proj_b", "host-user-1")
        assert other == USR_PROJ_B
        assert other != USR_PROJ_A

    def test_other_external_id_differs(self):
        assert end_user_id("proj_a", "host-user-2") == USR_PROJ_A_USER_2

    def test_shape_is_usr_plus_16_hex_chars(self):
        uid = end_user_id("proj_a", "host-user-1")
        assert uid.startswith("usr_")
        suffix = uid.removeprefix("usr_")
        assert len(suffix) == 16
        assert set(suffix) <= HEX_DIGITS


class TestConnectionId:
    def test_pinned_golden_vector_mixed_case_address(self):
        got = connection_id(
            "proj_a", USR_PROJ_A, ConnectionKind.ADDRESS, MIXED_CASE_ADDRESS
        )
        assert got == CONN_ADDR

    def test_mixed_case_and_lowercase_address_yield_the_same_id(self):
        assert (
            connection_id("proj_a", USR_PROJ_A, ConnectionKind.ADDRESS, LOWERED_ADDRESS)
            == CONN_ADDR
        )

    def test_surrounding_whitespace_is_stripped(self):
        padded = f"  {MIXED_CASE_ADDRESS}\n"
        got = connection_id("proj_a", USR_PROJ_A, ConnectionKind.ADDRESS, padded)
        assert got == CONN_ADDR

    def test_pinned_golden_vector_xpub_keeps_case(self):
        got = connection_id("proj_a", USR_PROJ_A, ConnectionKind.XPUB, XPUB)
        assert got == CONN_XPUB

    def test_lowercasing_an_xpub_changes_the_id(self):
        # base58 is case-significant; xpubs must never be folded.
        lowered = connection_id(
            "proj_a", USR_PROJ_A, ConnectionKind.XPUB, XPUB.lower()
        )
        assert lowered != CONN_XPUB

    def test_pinned_golden_vector_exchange_keeps_case(self):
        got = connection_id(
            "proj_a", USR_PROJ_A, ConnectionKind.EXCHANGE, "Kraken-Main"
        )
        assert got == CONN_EXCHANGE
        lowered = connection_id(
            "proj_a", USR_PROJ_A, ConnectionKind.EXCHANGE, "kraken-main"
        )
        assert lowered != CONN_EXCHANGE

    def test_project_id_is_identity_bearing(self):
        got = connection_id(
            "proj_b", USR_PROJ_A, ConnectionKind.ADDRESS, MIXED_CASE_ADDRESS
        )
        assert got == CONN_ADDR_PROJ_B
        assert got != CONN_ADDR

    def test_all_goldens_distinct(self):
        assert len({CONN_ADDR, CONN_XPUB, CONN_EXCHANGE, CONN_ADDR_PROJ_B}) == 4

    def test_shape_is_conn_plus_16_hex_chars(self):
        got = connection_id(
            "proj_a", USR_PROJ_A, ConnectionKind.ADDRESS, MIXED_CASE_ADDRESS
        )
        assert got.startswith("conn_")
        suffix = got.removeprefix("conn_")
        assert len(suffix) == 16
        assert set(suffix) <= HEX_DIGITS


class TestNormalizeDescriptor:
    def test_address_starting_0x_is_stripped_and_lowercased(self):
        got = normalize_descriptor(ConnectionKind.ADDRESS, f" {MIXED_CASE_ADDRESS} ")
        assert got == LOWERED_ADDRESS

    def test_address_without_0x_prefix_keeps_case(self):
        # e.g. a Solana address under kind=address: base58, never folded.
        got = normalize_descriptor(
            ConnectionKind.ADDRESS, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        )
        assert got == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def test_xpub_is_never_lowercased(self):
        assert normalize_descriptor(ConnectionKind.XPUB, f"  {XPUB}") == XPUB

    def test_exchange_is_never_lowercased(self):
        got = normalize_descriptor(ConnectionKind.EXCHANGE, "Kraken-Main ")
        assert got == "Kraken-Main"


class TestEnums:
    def test_scope_round_trips_from_wire_string(self):
        assert Scope("accounts:read") is Scope.ACCOUNTS_READ

    def test_scope_wire_strings_are_exactly_the_spec_set(self):
        assert {scope.value for scope in Scope} == {
            "accounts:read",
            "accounts:write",
            "sync:trigger",
            "users:admin",
        }
        assert len(Scope) == 4

    def test_scope_is_str(self):
        assert Scope.USERS_ADMIN == "users:admin"
        assert isinstance(Scope.ACCOUNTS_WRITE, str)

    def test_environment_members_are_both_exactly_4_chars(self):
        assert Environment.LIVE.value == "live"
        assert Environment.TEST.value == "test"
        assert len(Environment) == 2
        assert all(len(env.value) == 4 for env in Environment)

    def test_connection_kind_members_and_wire_values(self):
        assert ConnectionKind.ADDRESS.value == "address"
        assert ConnectionKind.XPUB.value == "xpub"
        assert ConnectionKind.EXCHANGE.value == "exchange"
        assert len(ConnectionKind) == 3

    def test_connection_kind_interpolates_as_its_wire_value(self):
        # The connection_id preimage embeds {kind}; StrEnum makes that
        # the wire string, not "ConnectionKind.ADDRESS".
        assert f"{ConnectionKind.ADDRESS}" == "address"


class TestImmutability:
    def test_organisation_is_frozen(self):
        org = make_org()
        with pytest.raises(FrozenInstanceError):
            org.name = "Other"

    def test_project_is_frozen(self):
        project = make_project()
        with pytest.raises(FrozenInstanceError):
            project.signing_secret = "00" * 32

    def test_end_user_is_frozen(self):
        user = make_end_user()
        with pytest.raises(FrozenInstanceError):
            user.external_user_id = "someone-else"

    def test_connection_is_frozen(self):
        conn = make_connection()
        with pytest.raises(FrozenInstanceError):
            conn.descriptor = "0x" + "00" * 20

    def test_api_key_is_frozen(self):
        key = make_api_key()
        with pytest.raises(FrozenInstanceError):
            key.revoked_at = MS

    def test_all_models_are_slotted_dataclasses(self):
        instances = (
            make_org(),
            make_project(),
            make_end_user(),
            make_connection(),
            make_api_key(),
        )
        for instance in instances:
            assert dataclasses.is_dataclass(instance)
            assert not hasattr(instance, "__dict__")  # slots=True


class TestShapeAndDefaults:
    def test_api_key_scopes_is_a_frozenset_of_scope(self):
        key = make_api_key()
        assert isinstance(key.scopes, frozenset)
        assert all(isinstance(scope, Scope) for scope in key.scopes)

    def test_api_key_expiry_and_revocation_default_to_none(self):
        key = make_api_key()
        assert key.expires_at is None
        assert key.revoked_at is None

    def test_api_key_lifecycle_timestamps_are_ms_epoch_ints(self):
        key = make_api_key(expires_at=MS + 3_600_000, revoked_at=MS + 60_000)
        assert key.expires_at == MS + 3_600_000
        assert key.revoked_at == MS + 60_000

    def test_created_at_is_ms_epoch_int_everywhere(self):
        for instance in (
            make_org(),
            make_project(),
            make_end_user(),
            make_connection(),
            make_api_key(),
        ):
            assert isinstance(instance.created_at, int)
            assert instance.created_at == MS

    def test_project_environment_is_the_enum(self):
        assert make_project().environment is Environment.LIVE

    def test_connection_kind_field_is_the_enum(self):
        assert make_connection().kind is ConnectionKind.ADDRESS


class TestRandomIds:
    def test_new_project_id_pinned_entropy_vector(self):
        assert new_project_id(lambda n: "cd" * n) == "proj_cdcdcdcdcdcdcdcd"

    def test_new_org_id_pinned_entropy_vector(self):
        assert new_org_id(lambda n: "ab" * n) == "org_abababababababab"

    def test_new_key_id_pinned_entropy_vector(self):
        assert new_key_id(lambda n: "ef" * n) == "key_efefefefefefefef"

    def test_entropy_is_asked_for_exactly_8_bytes(self):
        calls: list[int] = []

        def spy(n: int) -> str:
            calls.append(n)
            return "00" * n

        new_org_id(spy)
        assert calls == [8]

    def test_default_entropy_yields_16_lowercase_hex_chars(self):
        for factory, prefix in (
            (new_org_id, "org_"),
            (new_project_id, "proj_"),
            (new_key_id, "key_"),
        ):
            fresh = factory()
            assert fresh.startswith(prefix)
            suffix = fresh.removeprefix(prefix)
            assert len(suffix) == 16
            assert set(suffix) <= HEX_DIGITS

    def test_two_default_draws_differ(self):
        assert new_key_id() != new_key_id()
