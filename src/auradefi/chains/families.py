"""Chain families: the only taxonomy above individual chains (SPEC §4.2).

A family groups chains that share an address format, an RPC shape, and a
source-adapter implementation strategy (SPEC §10: chains ship as source
adapters one family at a time). Values are lowercase strings so the enum
serialises directly into wire formats and config files.
"""

from __future__ import annotations

from enum import StrEnum


class ChainFamily(StrEnum):
    """Address/RPC family a chain belongs to.

    Exactly three members in Phase 0. Adding a family is a public-contract
    change, not a local convenience.
    """

    EVM = "evm"
    BITCOIN = "bitcoin"
    SOLANA = "solana"
