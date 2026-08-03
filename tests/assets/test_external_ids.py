"""External-id vocabulary and additive_merge: never deletes, existing
wins, conflicts reported not silently overwritten (SPEC §4.2).
"""

from __future__ import annotations

from auradefi.assets.external_ids import CMC, COINGECKO, additive_merge


def test_provider_constants_are_pinned():
    assert COINGECKO == "coingecko"
    assert CMC == "cmc"


def test_merge_disjoint_keys_adds_everything():
    merged, conflicts = additive_merge({COINGECKO: "usd-coin"}, {CMC: "3408"})
    assert merged == {COINGECKO: "usd-coin", CMC: "3408"}
    assert conflicts == ()


def test_merge_agreeing_values_is_not_a_conflict():
    merged, conflicts = additive_merge(
        {COINGECKO: "usd-coin", CMC: "3408"}, {COINGECKO: "usd-coin"}
    )
    assert merged == {COINGECKO: "usd-coin", CMC: "3408"}
    assert conflicts == ()


def test_existing_value_wins_on_conflict_and_key_is_reported():
    merged, conflicts = additive_merge({COINGECKO: "usd-coin"}, {COINGECKO: "usdc"})
    assert merged == {COINGECKO: "usd-coin"}  # never overwritten
    assert conflicts == (COINGECKO,)


def test_merge_never_removes_a_key():
    existing = {COINGECKO: "usd-coin", CMC: "3408", "defillama": "usdc"}
    merged, _ = additive_merge(existing, {})
    assert merged == existing
    merged, _ = additive_merge(existing, {COINGECKO: "different"})
    assert set(merged) >= set(existing)


def test_merge_into_empty_existing_takes_all_new():
    merged, conflicts = additive_merge({}, {COINGECKO: "usd-coin", CMC: "3408"})
    assert merged == {COINGECKO: "usd-coin", CMC: "3408"}
    assert conflicts == ()


def test_merge_of_two_empties():
    merged, conflicts = additive_merge({}, {})
    assert merged == {}
    assert conflicts == ()


def test_multiple_conflicts_are_reported_sorted():
    merged, conflicts = additive_merge(
        {"b": "1", "a": "2", "c": "3"},
        {"b": "x", "a": "y", "d": "4"},
    )
    assert merged == {"a": "2", "b": "1", "c": "3", "d": "4"}
    assert conflicts == ("a", "b")  # sorted, deterministic


def test_return_types_are_dict_and_tuple():
    merged, conflicts = additive_merge({COINGECKO: "x"}, {COINGECKO: "y"})
    assert isinstance(merged, dict)
    assert isinstance(conflicts, tuple)


def test_inputs_are_never_mutated():
    existing = {COINGECKO: "usd-coin"}
    new = {COINGECKO: "usdc", CMC: "3408"}
    additive_merge(existing, new)
    assert existing == {COINGECKO: "usd-coin"}
    assert new == {COINGECKO: "usdc", CMC: "3408"}


def test_merged_is_a_new_dict_not_an_alias():
    existing = {COINGECKO: "usd-coin"}
    merged, _ = additive_merge(existing, {})
    assert merged is not existing
    merged[CMC] = "3408"
    assert CMC not in existing
