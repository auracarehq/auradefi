"""Golden vectors and invariants for auradefi.embed.models (SPEC §8, §7.1).

The id literals below were derived INDEPENDENTLY of the code under test,
via ``python3 -c`` over the algorithms pinned in docs/DECISIONS.md, with
project id fixed to ``"embed"``:

    tenant_id     = "usr_"  + sha256(f"embed|{external_user_id}".encode()).hexdigest()[:16]
    connection_id = "conn_" + sha256(f"embed|{tenant_id}|address|{normalized}".encode()).hexdigest()[:16]
    normalized    = address.strip(), lowercased iff it startswith "0x"

A stability contract is a hardcoded string, not a call to the function
under test. These pin the DECISIONS duplication waiver: embed's local
copies must produce the same bytes as the tenancy formulas forever.
"""

from __future__ import annotations

import dataclasses
from dataclasses import FrozenInstanceError

import pytest

from auradefi.embed.models import (
    EMBED_PROJECT_ID,
    ConnectionRecord,
    ConnectionSyncReport,
    SyncReport,
    SyncState,
    derive_connection_id,
    derive_tenant_id,
)
from auradefi.errors import ValidationError

# Derived independently (see module docstring); NEVER regenerate from the
# implementation.
USR_1 = "usr_1e63721d071ea2d9"  # embed | host-user-1
USR_2 = "usr_d6ace495d5f89481"  # embed | host-user-2
USR_MAX = "usr_1b449786b9a4c12c"  # embed | "z" * 128

ADDR = "0x" + "1" * 40
ADDR_2 = "0x" + "2" * 40
MIXED_CASE_ADDRESS = "0xAbCdEf" + "1" * 34
UPPER_HEX_ADDRESS = "0xABCDEF" + "1" * 34
LOWERED_ADDRESS = "0xabcdef" + "1" * 34
SOLANA_ADDRESS = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

CONN_ADDR = "conn_b116094c537a85e6"  # embed | USR_1 | address | ADDR
CONN_MIXED = "conn_b5d62ac34b85acb6"  # embed | USR_1 | address | lowered mixed
CONN_ADDR_2 = "conn_3a8b8993bc6953a9"  # embed | USR_1 | address | ADDR_2
CONN_UNDER_USR_2 = "conn_41ee2cd106c1f426"  # embed | USR_2 | address | ADDR
CONN_SOL = "conn_afea59bc61c58c1f"  # embed | USR_1 | address | SOLANA verbatim
CONN_SOL_LOWERED = "conn_86dedf519e6d918e"  # embed | USR_1 | address | solana lowered

MS = 1_754_000_000_000  # ms-epoch, matches the repo's frozen clock era
HEX_DIGITS = set("0123456789abcdef")


def make_record(**overrides) -> ConnectionRecord:
    fields = {
        "id": CONN_ADDR,
        "chain_id": "eip155:1",
        "address": ADDR,
        "created_at_ms": MS,
    }
    fields.update(overrides)
    return ConnectionRecord(**fields)


def make_conn_report(**overrides) -> ConnectionSyncReport:
    fields = {
        "connection_id": CONN_ADDR,
        "no_op": False,
        "pages_fetched": 3,
        "live_pages": 2,
        "backfill_pages": 1,
        "transactions_ingested": 57,
        "live_cursor": 42,
        "backfill_cursor": 7,
        "backfill_complete": False,
    }
    fields.update(overrides)
    return ConnectionSyncReport(**fields)


def make_report(**overrides) -> SyncReport:
    fields = {
        "no_op": False,
        "pages_fetched": 3,
        "live_pages": 2,
        "backfill_pages": 1,
        "transactions_ingested": 57,
    }
    fields.update(overrides)
    return SyncReport(**fields)


class TestEmbedProjectId:
    def test_is_exactly_the_string_embed(self):
        assert EMBED_PROJECT_ID == "embed"


class TestDeriveTenantId:
    def test_pinned_golden_vector(self):
        assert derive_tenant_id("host-user-1") == USR_1

    def test_deterministic_across_calls(self):
        assert derive_tenant_id("host-user-1") == derive_tenant_id("host-user-1")

    def test_other_external_id_differs(self):
        got = derive_tenant_id("host-user-2")
        assert got == USR_2
        assert got != USR_1

    def test_shape_is_usr_plus_16_hex_chars(self):
        got = derive_tenant_id("host-user-1")
        assert got.startswith("usr_")
        suffix = got.removeprefix("usr_")
        assert len(suffix) == 16
        assert set(suffix) <= HEX_DIGITS

    def test_boundary_128_passes_129_fails(self):
        assert derive_tenant_id("z" * 128) == USR_MAX
        with pytest.raises(ValidationError):
            derive_tenant_id("z" * 129)

    def test_vezgos_own_openapi_example_is_rejected_by_name(self):
        # SPEC §7.2: their loginName example is literally user@example.dev.
        with pytest.raises(ValidationError):
            derive_tenant_id("user@example.dev")

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "a b",
            "x" * 129,
            "@",
            "a@b",
            " host-user-1",
            "host-user-1 ",
            "host\tuser",
            "host-user-1\n",
            "usér-1",
            "user/1",
            "user#1",
        ],
    )
    def test_rejects_invalid_external_user_id(self, bad):
        with pytest.raises(ValidationError):
            derive_tenant_id(bad)

    @pytest.mark.parametrize(
        "good",
        ["host-user-1", "HOST_user.1:x", "a", "0", "A-Za-z0-9._:-", "x" * 128],
    )
    def test_valid_charset_is_accepted(self, good):
        got = derive_tenant_id(good)
        assert got.startswith("usr_")
        assert len(got) == 20


class TestDeriveConnectionId:
    def test_pinned_golden_vector(self):
        assert derive_connection_id(USR_1, ADDR) == CONN_ADDR

    def test_whitespace_padded_address_yields_the_same_id(self):
        assert derive_connection_id(USR_1, f"  {ADDR}\n") == CONN_ADDR

    def test_mixed_case_uppercase_hex_and_lowercase_all_yield_the_same_id(self):
        assert derive_connection_id(USR_1, MIXED_CASE_ADDRESS) == CONN_MIXED
        assert derive_connection_id(USR_1, UPPER_HEX_ADDRESS) == CONN_MIXED
        assert derive_connection_id(USR_1, LOWERED_ADDRESS) == CONN_MIXED

    def test_other_address_differs(self):
        got = derive_connection_id(USR_1, ADDR_2)
        assert got == CONN_ADDR_2
        assert got != CONN_ADDR

    def test_tenant_id_is_identity_bearing(self):
        got = derive_connection_id(USR_2, ADDR)
        assert got == CONN_UNDER_USR_2
        assert got != CONN_ADDR

    def test_non_0x_descriptor_keeps_case(self):
        # base58 is case-significant; only 0x-prefixed input is folded.
        assert derive_connection_id(USR_1, SOLANA_ADDRESS) == CONN_SOL
        lowered = derive_connection_id(USR_1, SOLANA_ADDRESS.lower())
        assert lowered == CONN_SOL_LOWERED
        assert lowered != CONN_SOL

    def test_all_goldens_distinct(self):
        goldens = {CONN_ADDR, CONN_MIXED, CONN_ADDR_2, CONN_UNDER_USR_2, CONN_SOL}
        assert len(goldens) == 5

    def test_shape_is_conn_plus_16_hex_chars(self):
        got = derive_connection_id(USR_1, ADDR)
        assert got.startswith("conn_")
        suffix = got.removeprefix("conn_")
        assert len(suffix) == 16
        assert set(suffix) <= HEX_DIGITS


class TestConnectionRecord:
    def test_field_values_round_trip(self):
        record = make_record()
        assert record.id == CONN_ADDR
        assert record.chain_id == "eip155:1"
        assert record.address == ADDR
        assert record.created_at_ms == MS

    def test_created_at_ms_is_an_ms_epoch_int(self):
        assert isinstance(make_record().created_at_ms, int)

    def test_value_equality(self):
        assert make_record() == make_record()
        assert make_record() != make_record(id=CONN_ADDR_2)


class TestSyncState:
    def test_defaults_equal_the_explicit_fresh_state(self):
        assert SyncState() == SyncState(0, None, False, 0)

    def test_default_field_values(self):
        state = SyncState()
        assert state.live_cursor == 0
        assert state.backfill_cursor is None
        assert state.backfill_complete is False
        assert state.last_sync_at_ms == 0

    def test_field_values_round_trip(self):
        state = SyncState(
            live_cursor=42,
            backfill_cursor=7,
            backfill_complete=True,
            last_sync_at_ms=MS,
        )
        assert state.live_cursor == 42
        assert state.backfill_cursor == 7
        assert state.backfill_complete is True
        assert state.last_sync_at_ms == MS

    def test_huge_cursor_round_trips_exactly(self):
        # Cursors are ints; 10^77-scale values must survive untouched.
        big = 10**77 + 3
        state = SyncState(live_cursor=big, backfill_cursor=big - 1)
        assert state.live_cursor == big
        assert state.backfill_cursor == big - 1


class TestConnectionSyncReport:
    def test_happy_path_field_values_round_trip(self):
        report = make_conn_report()
        assert report.connection_id == CONN_ADDR
        assert report.no_op is False
        assert report.pages_fetched == 3
        assert report.live_pages == 2
        assert report.backfill_pages == 1
        assert report.transactions_ingested == 57
        assert report.live_cursor == 42
        assert report.backfill_cursor == 7
        assert report.backfill_complete is False

    def test_no_op_with_zero_counts_constructs(self):
        report = make_conn_report(
            no_op=True,
            pages_fetched=0,
            live_pages=0,
            backfill_pages=0,
            transactions_ingested=0,
        )
        assert report.no_op is True
        assert report.pages_fetched == 0

    def test_no_op_true_with_nonzero_pages_fetched_raises(self):
        with pytest.raises(ValidationError):
            make_conn_report(
                no_op=True,
                pages_fetched=1,
                live_pages=0,
                backfill_pages=0,
                transactions_ingested=0,
            )

    @pytest.mark.parametrize(
        "field",
        ["pages_fetched", "live_pages", "backfill_pages", "transactions_ingested"],
    )
    def test_negative_count_raises(self, field):
        with pytest.raises(ValidationError):
            make_conn_report(**{field: -1})

    def test_backfill_cursor_none_is_allowed(self):
        assert make_conn_report(backfill_cursor=None).backfill_cursor is None

    def test_huge_counts_construct(self):
        # The partition holds at any scale: 10^18 = 10^18 + 0.
        report = make_conn_report(
            pages_fetched=10**18,
            live_pages=10**18,
            backfill_pages=0,
            transactions_ingested=10**18,
        )
        assert report.transactions_ingested == 10**18
        assert report.pages_fetched == report.live_pages + report.backfill_pages

    def test_pages_fetched_is_exactly_the_two_phases_summed(self):
        # SPEC §8: ONE shared budget, spent on the live window then the
        # backfill. There is no third bucket, so the halves are the whole.
        report = make_conn_report()
        assert report.pages_fetched == 3
        assert report.pages_fetched == report.live_pages + report.backfill_pages

    @pytest.mark.parametrize(
        ("pages_fetched", "live_pages", "backfill_pages"),
        [
            (2, 2, 0),  # budget drained entirely by the live window
            (2, 0, 2),  # live window empty, all budget to the backfill
            (0, 0, 0),  # budget spent nothing
            (10**77 + 5, 10**77, 5),  # huge-scale partition
        ],
    )
    def test_every_exact_partition_constructs(
        self, pages_fetched, live_pages, backfill_pages
    ):
        report = make_conn_report(
            pages_fetched=pages_fetched,
            live_pages=live_pages,
            backfill_pages=backfill_pages,
        )
        assert report.pages_fetched == live_pages + backfill_pages

    @pytest.mark.parametrize(
        ("pages_fetched", "live_pages", "backfill_pages"),
        [
            (1, 10**6, 10**6),  # the exact report the reviewer constructed
            (3, 2, 2),  # halves overshoot the whole by one
            (4, 2, 1),  # a page fetched that belongs to neither phase
            (3, 3, 1),  # default backfill left dangling
            (0, 1, 0),  # nothing fetched yet a live page appeared
            (10**18, 10**18, 1),
        ],
    )
    def test_pages_fetched_not_equal_to_the_sum_raises(
        self, pages_fetched, live_pages, backfill_pages
    ):
        with pytest.raises(ValidationError):
            make_conn_report(
                pages_fetched=pages_fetched,
                live_pages=live_pages,
                backfill_pages=backfill_pages,
            )

    def test_no_op_with_zero_pages_but_phase_pages_raises(self):
        # no_op pins pages_fetched to 0; the partition then pins both halves.
        with pytest.raises(ValidationError):
            make_conn_report(
                no_op=True,
                pages_fetched=0,
                live_pages=1,
                backfill_pages=0,
                transactions_ingested=0,
            )


class TestSyncReport:
    def test_happy_path_field_values_round_trip(self):
        report = make_report()
        assert report.no_op is False
        assert report.pages_fetched == 3
        assert report.live_pages == 2
        assert report.backfill_pages == 1
        assert report.transactions_ingested == 57

    def test_connections_defaults_to_empty_tuple(self):
        assert make_report().connections == ()

    def test_no_op_all_zero_constructs_positionally(self):
        report = SyncReport(True, 0, 0, 0, 0)
        assert report.no_op is True
        assert report.pages_fetched == 0
        assert report.connections == ()

    def test_no_op_true_with_nonzero_pages_fetched_raises(self):
        with pytest.raises(ValidationError):
            SyncReport(True, 1, 0, 0, 0)

    @pytest.mark.parametrize(
        "field",
        ["pages_fetched", "live_pages", "backfill_pages", "transactions_ingested"],
    )
    def test_negative_count_raises(self, field):
        with pytest.raises(ValidationError):
            make_report(**{field: -1})

    def test_carries_per_connection_breakdown(self):
        child = make_conn_report()
        report = make_report(connections=(child,))
        assert report.connections == (child,)
        assert isinstance(report.connections, tuple)

    def test_pages_fetched_is_exactly_the_two_phases_summed(self):
        report = make_report()
        assert report.pages_fetched == 3
        assert report.pages_fetched == report.live_pages + report.backfill_pages

    @pytest.mark.parametrize(
        ("pages_fetched", "live_pages", "backfill_pages"),
        [(2, 2, 0), (2, 0, 2), (0, 0, 0), (10**77 + 5, 10**77, 5)],
    )
    def test_every_exact_partition_constructs(
        self, pages_fetched, live_pages, backfill_pages
    ):
        report = make_report(
            pages_fetched=pages_fetched,
            live_pages=live_pages,
            backfill_pages=backfill_pages,
        )
        assert report.pages_fetched == live_pages + backfill_pages

    @pytest.mark.parametrize(
        ("pages_fetched", "live_pages", "backfill_pages"),
        [(1, 10**6, 10**6), (3, 2, 2), (4, 2, 1), (0, 1, 0)],
    )
    def test_pages_fetched_not_equal_to_the_sum_raises(
        self, pages_fetched, live_pages, backfill_pages
    ):
        with pytest.raises(ValidationError):
            make_report(
                pages_fetched=pages_fetched,
                live_pages=live_pages,
                backfill_pages=backfill_pages,
            )

    def test_no_op_with_zero_pages_but_phase_pages_raises(self):
        with pytest.raises(ValidationError):
            SyncReport(True, 0, 1, 0, 0)


class TestImmutability:
    def test_connection_record_is_frozen(self):
        record = make_record()
        with pytest.raises(FrozenInstanceError):
            record.address = ADDR_2

    def test_sync_state_is_frozen(self):
        state = SyncState()
        with pytest.raises(FrozenInstanceError):
            state.live_cursor = 1

    def test_connection_sync_report_is_frozen(self):
        report = make_conn_report()
        with pytest.raises(FrozenInstanceError):
            report.pages_fetched = 0

    def test_sync_report_is_frozen(self):
        report = make_report()
        with pytest.raises(FrozenInstanceError):
            report.no_op = True

    def test_all_four_models_are_slotted_dataclasses(self):
        instances = (make_record(), SyncState(), make_conn_report(), make_report())
        for instance in instances:
            assert dataclasses.is_dataclass(instance)
            assert not hasattr(instance, "__dict__")  # slots=True
