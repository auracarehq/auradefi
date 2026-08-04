"""DECISIONS.md duplication-waiver cross-pin: embed vs tenancy id formulas.

``embed.models.derive_tenant_id`` / ``derive_connection_id`` are
value-identical local copies of ``tenancy.models.end_user_id`` /
``connection_id`` — the layer contract forbids embed→tenancy imports
(``tests/style/test_layering.py``: ``tenancy`` is absent from ``embed``'s
allowed set). Byte equality between the two is therefore only checkable
from a test. This is that test, in the established shape of
``tests/ledger/test_bridge.py``.

Both sides are ALSO asserted against the literals pinned in
tests/embed/test_models.py (derived independently via ``python3 -c`` from
the DECISIONS formulas), so a *simultaneous* drift in both copies still
goes red rather than agreeing with itself.

The load-bearing fragility this guards: embed hardcodes the kind segment
as the string ``"address"``, while tenancy interpolates
``f"{ConnectionKind.ADDRESS}"``. Those agree only because
``ConnectionKind`` is a ``StrEnum``. Demote it to ``Enum`` and every
``conn_`` id in the product silently changes.
"""

from __future__ import annotations

import pytest

from auradefi.embed.models import (
    EMBED_PROJECT_ID,
    derive_connection_id,
    derive_tenant_id,
)
from auradefi.errors import ValidationError
from auradefi.tenancy import models as tenancy

# Restated here rather than imported from tests/embed/test_models.py: the
# suite runs under --import-mode=importlib with no tests/__init__.py
# (DECISIONS #4), and an independent restatement is the stronger form for
# a cross-pin anyway — these literals must equal the ones over there.
USR_1 = "usr_1e63721d071ea2d9"  # embed | host-user-1
USR_2 = "usr_d6ace495d5f89481"  # embed | host-user-2
USR_MAX = "usr_1b449786b9a4c12c"  # embed | "z" * 128

ADDR = "0x" + "1" * 40
MIXED_CASE_ADDRESS = "0xAbCdEf" + "1" * 34
UPPER_HEX_ADDRESS = "0xABCDEF" + "1" * 34
SOLANA_ADDRESS = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

CONN_ADDR = "conn_b116094c537a85e6"  # embed | USR_1 | address | ADDR
CONN_MIXED = "conn_b5d62ac34b85acb6"  # embed | USR_1 | address | lowered mixed
CONN_SOL = "conn_afea59bc61c58c1f"  # embed | USR_1 | address | SOLANA verbatim
CONN_SOL_LOWERED = "conn_86dedf519e6d918e"  # embed | USR_1 | address | lowered

ADDRESS = tenancy.ConnectionKind.ADDRESS


def test_embed_project_id_is_the_segment_both_sides_agree_on():
    assert EMBED_PROJECT_ID == "embed"


def test_connection_kind_address_interpolates_as_the_bare_string():
    # embed hardcodes "address"; tenancy interpolates the enum member.
    # StrEnum is the ONLY reason those produce the same bytes.
    assert f"{ADDRESS}" == "address"
    assert str(ADDRESS) == "address"


@pytest.mark.parametrize(
    ("external_user_id", "expected"),
    [
        ("host-user-1", USR_1),
        ("host-user-2", USR_2),
        ("z" * 128, USR_MAX),
    ],
)
def test_tenant_id_duplicate_is_byte_identical(external_user_id, expected):
    assert derive_tenant_id(external_user_id) == expected
    assert tenancy.end_user_id(EMBED_PROJECT_ID, external_user_id) == expected


@pytest.mark.parametrize(
    ("descriptor", "expected"),
    [
        (ADDR, CONN_ADDR),
        (f"  {ADDR}\n", CONN_ADDR),
        (MIXED_CASE_ADDRESS, CONN_MIXED),
        (UPPER_HEX_ADDRESS, CONN_MIXED),
        (SOLANA_ADDRESS, CONN_SOL),
        (SOLANA_ADDRESS.lower(), CONN_SOL_LOWERED),
    ],
)
def test_connection_id_duplicate_is_byte_identical(descriptor, expected):
    assert derive_connection_id(USR_1, descriptor) == expected
    assert (
        tenancy.connection_id(EMBED_PROJECT_ID, USR_1, ADDRESS, descriptor) == expected
    )


def test_descriptor_normalization_duplicate_agrees():
    # embed inlines "strip, lowercase iff startswith 0x"; tenancy owns it
    # as normalize_descriptor. The 0x fold and the base58 non-fold must
    # stay the same rule on both sides.
    assert tenancy.normalize_descriptor(ADDRESS, f"  {MIXED_CASE_ADDRESS} ") == (
        MIXED_CASE_ADDRESS.lower()
    )
    assert tenancy.normalize_descriptor(ADDRESS, f" {SOLANA_ADDRESS} ") == (
        SOLANA_ADDRESS
    )


@pytest.mark.parametrize(
    "bad",
    ["", "a b", "z" * 129, "user@example.dev", " host-user-1", "user/1"],
)
def test_rejection_set_agrees_with_tenancy(bad):
    with pytest.raises(ValidationError):
        derive_tenant_id(bad)
    with pytest.raises(ValidationError):
        tenancy.validate_external_user_id(bad)


@pytest.mark.parametrize("good", ["host-user-1", "HOST_user.1:x", "a", "0"])
def test_acceptance_set_agrees_with_tenancy(good):
    assert tenancy.validate_external_user_id(good) == good
    assert derive_tenant_id(good) == tenancy.end_user_id(EMBED_PROJECT_ID, good)
