"""External id vocabulary and additive merging (SPEC §4.2).

Vezgo ships no external ids and everyone rebuilds the join to CoinGecko /
CoinMarketCap by hand; we carry them on the Asset. Merging is ADDITIVE,
never destructive (rotki's scar: a transient source failure once wiped
previously-detected data): a merge can only ever add keys, and a
disagreement is reported, not silently overwritten. stdlib only.
"""

from __future__ import annotations

from collections.abc import Mapping

COINGECKO = "coingecko"
CMC = "cmc"


def additive_merge(
    existing: Mapping[str, str], new: Mapping[str, str]
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Merge ``new`` into ``existing`` without ever deleting anything.

    Returns ``(merged, conflicts)`` where ``merged`` is a NEW dict
    holding every key from both inputs, and ``conflicts`` is a sorted
    tuple of the keys present in both with differing values. On a
    conflict the EXISTING value wins: the merge never overwrites and
    never removes. Neither input is mutated. A key present in both with
    the same value is not a conflict.
    """
    merged = dict(existing)
    conflicts = []
    for key, value in new.items():
        if key not in merged:
            merged[key] = value
        elif merged[key] != value:
            conflicts.append(key)
    return merged, tuple(sorted(conflicts))
