"""``UserHandle`` after the split out of ``facade.py``.

The move was mechanical, so this file pins the properties that a mechanical
move could quietly break, not the connect-time error contract, which
``tests/embed/test_facade.py`` and ``tests/contract/test_embedding.py``
already own and exercise through the facade.

What matters here: a handle carries NO state but its derived tenant id, it
borrows every collaborator from the instance that made it, and two handles
for the same host user id are interchangeable. Those are what let
``Auradefi.user()`` stay a pure, cheap call.
"""

from __future__ import annotations

import pytest

from auradefi.embed.facade import Auradefi
from auradefi.embed.handle import UserHandle
from auradefi.embed.models import derive_tenant_id
from auradefi.errors import ConflictError, UnknownChainError
from auradefi.sources import sandbox as recording


@pytest.fixture
def aura() -> Auradefi:
    """A sandbox instance: real ports, recorded transport, no keys."""
    return Auradefi.sandbox(connect=False)


class TestConstruction:
    def test_user_returns_a_handle_from_the_split_module(self, aura):
        handle = aura.user("host-user-1")
        assert isinstance(handle, UserHandle)
        assert type(handle).__module__ == "auradefi.embed.handle"

    def test_the_tenant_id_is_derived_not_supplied(self, aura):
        handle = aura.user("host-user-1")
        assert handle.tenant_id == derive_tenant_id("host-user-1", "embed")
        assert handle.external_user_id == "host-user-1"

    def test_two_handles_for_one_user_agree(self, aura):
        first, second = aura.user("host-user-1"), aura.user("host-user-1")
        assert first is not second
        assert first.tenant_id == second.tenant_id

    def test_creating_a_handle_performs_no_io(self, aura):
        """`user()` is a pure call: nothing stored, nothing requested."""
        aura.user("host-user-1")
        assert aura.user("host-user-1").connections() == ()

    def test_different_users_are_different_tenants(self, aura):
        assert aura.user("a").tenant_id != aura.user("b").tenant_id


class TestBorrowedCollaborators:
    def test_a_handle_holds_no_ports_of_its_own(self, aura):
        handle = aura.user("host-user-1")
        # Only the facade and the two derived strings; every port is the
        # facade's, so a handle cannot drift from the instance that made it.
        assert set(vars(handle)) == {"_facade", "external_user_id", "tenant_id"}
        assert handle._facade is aura

    def test_connections_read_through_the_facades_state_port(self, aura):
        handle = aura.user(recording.SANDBOX_USER)
        handle.connect_address(recording.SANDBOX_CHAIN, recording.SANDBOX_ADDRESS)

        # Same rows, whether read through the handle or the injected store.
        assert handle.connections() == aura._sync_state.connections(handle.tenant_id)

    def test_scoped_views_answer_only_this_users_connections(self, aura):
        mine = aura.user(recording.SANDBOX_USER)
        mine.connect_address(recording.SANDBOX_CHAIN, recording.SANDBOX_ADDRESS)
        theirs = aura.user("someone-else")

        assert len(mine.holdings()) == 1
        assert theirs.holdings() == ()
        assert theirs.sync(budget=2).no_op is True      # vacuously: no work
        assert theirs.scalar_metrics() == ()


class TestValidationSurvivedTheMove:
    def test_a_duplicate_connection_still_conflicts(self, aura):
        handle = aura.user(recording.SANDBOX_USER)
        handle.connect_address(recording.SANDBOX_CHAIN, recording.SANDBOX_ADDRESS)

        # The 40 hex DIGITS are case-insensitive; the `0x` prefix is not,
        # so only the digits are re-cased here.
        same_address = "0x" + recording.SANDBOX_ADDRESS[2:].upper()
        with pytest.raises(ConflictError) as caught:
            handle.connect_address(recording.SANDBOX_CHAIN, same_address)
        assert caught.value.existing_id.startswith("conn_")

    def test_an_unseeded_chain_is_still_refused_at_connect(self, aura):
        with pytest.raises(UnknownChainError):
            aura.user("host-user-1").connect_address(
                "eip155:99999", recording.SANDBOX_ADDRESS
            )
