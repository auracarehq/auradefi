"""DECISIONS.md duplication-waiver cross-pin: embed vs tenancy id formulas.

``embed.models.derive_tenant_id`` is a value-identical local copy of
``tenancy.models.end_user_id``: the layer contract forbids embed→tenancy
imports (``tests/style/test_layering.py``: ``tenancy`` is absent from
``embed``'s allowed set). Byte equality between the two is therefore only
checkable from a test. This is that test, in the established shape of
``tests/ledger/test_bridge.py``.

**The connection-id half of that waiver was RETIRED in 0.1.1**
(RELEASE_0.1.1 §5 #26). ``embed.models.derive_connection_id`` now hashes
``chain_id`` as well, because without it one address could be connected
on exactly one chain and two chains would share one sync cursor. That is
a deliberate divergence, so this file pins the opposite of byte equality:
the two formulas must now DIFFER, and ``tenancy.connection_id`` must NOT
grow a chain segment. Rehashing it would orphan every connection id
already persisted by the HTTP surface. The tenant half is untouched and
still cross-pinned below.

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
# a cross-pin anyway. These literals must equal the ones over there.
USR_1 = "usr_1e63721d071ea2d9"  # embed | host-user-1
USR_2 = "usr_d6ace495d5f89481"  # embed | host-user-2
USR_MAX = "usr_1b449786b9a4c12c"  # embed | "z" * 128

PROJECT_X = "proj_9f8e7d6c5b4a3928"  # a host's REAL project id
USR_1_UNDER_X = "usr_2d8ea8d7f9c2c31e"  # PROJECT_X | host-user-1
USR_2_UNDER_X = "usr_0833f2095815e9b5"  # PROJECT_X | host-user-2

ADDR = "0x" + "1" * 40
MIXED_CASE_ADDRESS = "0xAbCdEf" + "1" * 34
UPPER_HEX_ADDRESS = "0xABCDEF" + "1" * 34
SOLANA_ADDRESS = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

CHAIN = "eip155:1"
CHAIN_POLYGON = "eip155:137"
CHAIN_SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

# embed | USR_1 | address | chain | normalized-descriptor
CONN_ADDR = "conn_d0327e21d9b0ea55"  # eip155:1  | ADDR
CONN_ADDR_POLYGON = "conn_acb7e927076b309e"  # eip155:137 | ADDR
CONN_MIXED = "conn_6b627bb29f855dd2"  # eip155:1  | lowered mixed
CONN_SOL = "conn_a683a123e9b8a8dd"  # solana:…  | SOLANA verbatim
CONN_SOL_LOWERED = "conn_a656fc7b897f7b8d"  # solana:…  | lowered

# tenancy's own chainless id for the same inputs: the 0.1.0 embed value,
# and still exactly what the HTTP surface persists today.
TENANCY_CONN_ADDR = "conn_b116094c537a85e6"

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


# pins: the two end_user_id formulas agree for ANY project id, not just
#       "embed". The whole point of #19 is that a host can configure one
#       project and have both surfaces land on the same tenant.
@pytest.mark.parametrize(
    ("external_user_id", "expected"),
    [("host-user-1", USR_1_UNDER_X), ("host-user-2", USR_2_UNDER_X)],
)
def test_tenant_id_duplicate_agrees_under_a_real_project_id(
    external_user_id, expected
):
    assert derive_tenant_id(external_user_id, project_id=PROJECT_X) == expected
    assert tenancy.end_user_id(PROJECT_X, external_user_id) == expected


# pins: embed's default project id is still "embed", so a 0.1.0 tenant id
#       resolves unchanged in 0.1.1 and its ledger rows stay readable.
def test_the_default_project_id_is_still_the_0_1_0_value():
    assert derive_tenant_id("host-user-1") == USR_1
    assert derive_tenant_id("host-user-1", project_id="embed") == USR_1


# pins: the RETIRED half of the waiver. Embed's connection id carries the
#       chain, so it is deliberately NOT tenancy's id for the same inputs.
@pytest.mark.parametrize(
    ("chain_id", "descriptor", "expected"),
    [
        (CHAIN, ADDR, CONN_ADDR),
        (CHAIN, f"  {ADDR}\n", CONN_ADDR),
        (CHAIN_POLYGON, ADDR, CONN_ADDR_POLYGON),
        (CHAIN, MIXED_CASE_ADDRESS, CONN_MIXED),
        (CHAIN, UPPER_HEX_ADDRESS, CONN_MIXED),
        (CHAIN_SOLANA, SOLANA_ADDRESS, CONN_SOL),
        (CHAIN_SOLANA, SOLANA_ADDRESS.lower(), CONN_SOL_LOWERED),
    ],
)
def test_connection_id_is_chain_scoped_and_no_longer_byte_identical(
    chain_id, descriptor, expected
):
    assert derive_connection_id(USR_1, descriptor, chain_id) == expected
    assert (
        tenancy.connection_id(EMBED_PROJECT_ID, USR_1, ADDRESS, descriptor)
        != expected
    )


# pins: tenancy's connection id did NOT grow a chain segment: rehashing
#       it would orphan every id the HTTP surface has already persisted.
def test_tenancy_connection_id_is_unchanged_and_chainless():
    assert (
        tenancy.connection_id(EMBED_PROJECT_ID, USR_1, ADDRESS, ADDR)
        == TENANCY_CONN_ADDR
    )
    assert CONN_ADDR != TENANCY_CONN_ADDR


def test_descriptor_normalization_duplicate_agrees():
    # embed inlines "strip, lowercase iff startswith 0x"; tenancy owns it
    # as normalize_descriptor. The 0x fold and the base58 non-fold must
    # stay the same rule on both sides, only the chain segment diverged.
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
