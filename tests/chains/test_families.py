"""ChainFamily: exactly three members, string-valued, wire-stable (SPEC §4.2)."""

from __future__ import annotations

import pytest

from auradefi.chains.families import ChainFamily


def test_member_values_are_pinned_lowercase_strings():
    assert ChainFamily.EVM == "evm"
    assert ChainFamily.BITCOIN == "bitcoin"
    assert ChainFamily.SOLANA == "solana"


def test_exactly_three_families_in_phase_0():
    assert {member.value for member in ChainFamily} == {"evm", "bitcoin", "solana"}
    assert len(ChainFamily) == 3


def test_members_are_str_instances_for_direct_serialisation():
    for member in ChainFamily:
        assert isinstance(member, str)


def test_str_form_is_the_bare_value_not_the_repr():
    # StrEnum contract: str() gives the value, so f-strings and json.dumps
    # emit "evm", never "ChainFamily.EVM".
    assert str(ChainFamily.EVM) == "evm"
    assert f"{ChainFamily.SOLANA}" == "solana"


def test_lookup_by_value_round_trips():
    assert ChainFamily("evm") is ChainFamily.EVM
    assert ChainFamily("bitcoin") is ChainFamily.BITCOIN
    assert ChainFamily("solana") is ChainFamily.SOLANA


def test_vendor_names_are_not_family_values():
    for zoo_name in ("ethereum", "eth-mainnet", "btc", "sol", "EVM"):
        with pytest.raises(ValueError):
            ChainFamily(zoo_name)
