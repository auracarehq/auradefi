"""SEAM AUDIT — wave 0.1.1-wave2: one logical id, derived in two places.

The library (``src/auradefi/embed/models.py``, order ``embed-ids-loop``)
and the HTTP surface (``src/auradefi/tenancy/models.py`` reached through
``src/auradefi/api/routes/sync.py``) each derive the ledger tenant key
from the host's opaque user id. The layer gate forbids
``embed`` → ``tenancy`` imports, so the two formulas are LOCAL COPIES and
nothing inside either module can notice them drifting. Every expected
value below is computed independently from the formula written in
``docs/internal/DECISIONS.md``, never regenerated from the code.

Both derivations are RUN, never read. Consequences that cross a module
boundary are pinned too: an embed connection id is the ``account_id``
that ``ledger.models.transaction_id`` hashes, so changing its shape
changes every transaction id in the ledger — including the golden vectors
in ``tests/contract/test_embedding.py``, which no order in this wave owns.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from auradefi.clock import FrozenClock
from auradefi.config import Settings
from auradefi.embed.models import (
    EMBED_PROJECT_ID,
    derive_connection_id,
    derive_tenant_id,
)
from auradefi.ledger.models import transaction_id
from auradefi.tenancy.models import (
    ConnectionKind,
    Environment,
    connection_id,
    end_user_id,
)
from auradefi.tenancy.store import TenancyStore

REPO = Path(__file__).resolve().parents[3]
EXTERNAL_USER_ID = "host-user-1"
ADDRESS = "0x1111111111111111111111111111111111111111"

#: Independently computed from docs/internal/DECISIONS.md:
#: end_user_id = "usr_" + sha256(f"{project_id}|{external_user_id}")[:16]
TENANT_UNDER_EMBED = "usr_1e63721d071ea2d9"

#: Independently computed from docs/internal/DECISIONS.md's 0.1.1 line:
#: "conn_" + sha256(f"embed|{tenant_id}|address|{chain_id}|{descriptor}")[:16]
CONN_MAINNET = "conn_d0327e21d9b0ea55"
CONN_POLYGON = "conn_acb7e927076b309e"

#: The chainless 0.1.0 value, kept to prove the break is deliberate.
CONN_0_1_0 = "conn_b116094c537a85e6"


def _sha16(value: str) -> str:
    """The pinned truncated digest every id formula in DECISIONS uses."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _embedding_gate_constants() -> tuple[str, tuple[str, ...]]:
    """``(CONNECTION_ID, TXN_IDS)`` as the phase-5 gate hardcodes them.

    Read with ``ast`` rather than imported: the gate is another agent's
    file and collecting it twice would be a side effect.
    """
    module = ast.parse(
        (REPO / "tests" / "contract" / "test_embedding.py").read_text(
            encoding="utf-8"
        )
    )
    found: dict[str, object] = {}
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in {
                "CONNECTION_ID",
                "TXN_IDS",
            }:
                found[target.id] = ast.literal_eval(node.value)
    return str(found["CONNECTION_ID"]), tuple(found["TXN_IDS"])


class TestTenantDerivation:
    """#19 — the library and the API must key the same ledger tenant."""

    def test_the_default_still_derives_the_0_1_0_value(self):
        """The default must stay ``"embed"`` or 0.1.0 data is unreachable."""
        assert EMBED_PROJECT_ID == "embed"
        assert derive_tenant_id(EXTERNAL_USER_ID) == TENANT_UNDER_EMBED
        assert Settings().project_id == "embed"

    def test_the_end_user_cross_pin_is_untouched_at_the_default(self):
        """Both local copies still produce the same bytes at ``"embed"``.

        This half of the duplication waiver survives 0.1.1; only the
        connection-id half is retired.
        """
        assert derive_tenant_id(EXTERNAL_USER_ID) == end_user_id(
            "embed", EXTERNAL_USER_ID
        )

    def test_the_library_hashes_the_project_id_the_api_hashes(self):
        """The seam itself: a real project id, both sides, same bytes.

        ``GET /crypto/sync`` keys the ledger by
        ``resolve_end_user(...).id``, which the tenancy store derives as
        ``end_user_id(claims.project_id, external_user_id)``. The library
        must reach the same string for the same project, or the client
        reads an account with zero transactions forever with no error on
        either side.
        """
        project_id = "proj_abcdef0123456789"
        expected = "usr_" + _sha16(f"{project_id}|{EXTERNAL_USER_ID}")
        assert end_user_id(project_id, EXTERNAL_USER_ID) == expected
        assert derive_tenant_id(EXTERNAL_USER_ID, project_id=project_id) == expected, (
            "the library cannot be pointed at the API's project, so the "
            "two surfaces address different ledger tenants"
        )

    def test_the_tenancy_store_agrees_with_the_library(self):
        """Run the REAL consumer, not just the formula beside it.

        The store is what ``resolve_end_user`` calls, and it is the value
        that becomes the ledger tenant key on the HTTP side.
        """
        clock = FrozenClock(1_700_000_000_000)
        tenancy = TenancyStore()
        org = tenancy.create_organisation("host", clock)
        project = tenancy.create_project(
            org.id, "host-project", Environment.TEST, clock
        )
        user = tenancy.get_or_create_user(project.id, EXTERNAL_USER_ID, clock)
        settings = Settings(project_id=project.id)
        assert derive_tenant_id(
            EXTERNAL_USER_ID, project_id=settings.project_id
        ) == user.id, (
            "the library's tenant id and the tenancy store's end-user id "
            f"disagree for project {project.id!r}: the HTTP surface reads "
            f"{user.id!r}"
        )


class TestConnectionDerivation:
    """#26 — the connection id gains a chain segment, deliberately."""

    def test_it_matches_the_formula_documented_in_decisions(self):
        """The DOCUMENTED segment order is the contract, not the code's.

        ``docs/internal/DECISIONS.md`` states
        ``embed|{tenant_id}|address|{chain_id}|{normalized_descriptor}``;
        any other order produces a different id and silently orphans the
        rows written under the documented one.
        """
        assert CONN_MAINNET == "conn_" + _sha16(
            f"embed|{TENANT_UNDER_EMBED}|address|eip155:1|{ADDRESS}"
        )
        assert (
            derive_connection_id(TENANT_UNDER_EMBED, ADDRESS, "eip155:1")
            == CONN_MAINNET
        )

    def test_two_chains_give_two_ids(self):
        """One address, two chains, two cursors — the point of the change."""
        mainnet = derive_connection_id(TENANT_UNDER_EMBED, ADDRESS, "eip155:1")
        polygon = derive_connection_id(TENANT_UNDER_EMBED, ADDRESS, "eip155:137")
        assert (mainnet, polygon) == (CONN_MAINNET, CONN_POLYGON)
        assert mainnet != polygon

    def test_the_break_from_tenancy_is_deliberate_and_one_sided(self):
        """embed's id diverges; ``tenancy.connection_id`` must NOT move.

        Rehashing the tenancy formula would orphan every connection id
        the HTTP surface has already persisted, so the waiver is retired
        on ONE side only.
        """
        assert connection_id(
            "embed", TENANT_UNDER_EMBED, ConnectionKind.ADDRESS, ADDRESS
        ) == CONN_0_1_0
        assert (
            derive_connection_id(TENANT_UNDER_EMBED, ADDRESS, "eip155:1")
            != CONN_0_1_0
        )


class TestConnectionIdReachesTheLedger:
    """The connection id is an ``account_id``, so it rehashes downstream."""

    def test_the_phase_5_gate_vectors_match_the_derived_connection_id(self):
        """``transaction_id`` hashes ``chain|hash|account_id``.

        ``tests/contract/test_embedding.py`` hardcodes the connection id
        and all seven transaction ids, and is owned by NO order in this
        wave. Changing the connection-id formula rehashes every one of
        them, so that file has to change in the same commit as #26.

        The expected connection id here is the one ``docs/internal/DECISIONS.md``
        specifies, not the one the code returns, so the pin holds whatever
        state ``derive_connection_id``'s signature is in.
        """
        pinned_connection, pinned_txns = _embedding_gate_constants()
        derived_connection = CONN_MAINNET
        hashes = tuple(f"0x{index:064x}" for index in range(1, 8))
        derived_txns = tuple(
            transaction_id("eip155:1", tx_hash, derived_connection)
            for tx_hash in hashes
        )
        assert (pinned_connection, pinned_txns) == (
            derived_connection,
            derived_txns,
        ), (
            "the phase-5 gate's golden vectors no longer match the ids the "
            "library derives. The connection id is the ledger account_id, "
            "so every transaction id moved too:\n"
            f"  connection: {pinned_connection} -> {derived_connection}\n"
            + "\n".join(
                f"  txn: {old} -> {new}"
                for old, new in zip(pinned_txns, derived_txns, strict=False)
                if old != new
            )
        )
