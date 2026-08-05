"""Liquid staking adapters: Lido and Rocket Pool (SPEC §5.4, §4.3).

Two subclasses of ``ReceiptTokenAdapter`` proving the fork economics:
each body is class attributes ONLY: zero methods, ≤15 source lines
(Zapper's entire production Uniswap V2 integration was 15 lines and
zero methods; SPEC §5.4). The redemption arithmetic, discovery and
resolution all live in the fork helper.

StETH rebases 1:1 with ETH, so its rate is the identity (``rate_fn
None``); rETH appreciates against ETH via ``getExchangeRate`` (an
18-decimal fixed point). Golden fixtures pinned to Ethereum block
20_450_000 live in ``tests/golden/test_positions_liquid_staking.py``
(SPEC rule #5).
"""

from __future__ import annotations

from auradefi.positions.adapters.tokens import ReceiptToken, ReceiptTokenAdapter


class LidoAdapter(ReceiptTokenAdapter):
    """Lido stETH: rebasing 1:1 receipt; identity rate (rate_fn None)."""

    id = "lido"
    chains = frozenset({"eip155:1"})
    receipts = {
        "eip155:1": (
            ReceiptToken(
                "0xae7ab96520de3a18e5e111b5eaab095312d7fe84",
                "eip155:1/slip44:60", 18, None,
            ),
        ),
    }


class RocketPoolAdapter(ReceiptTokenAdapter):
    """Rocket Pool rETH: redemption via getExchangeRate (18-dec fixed point)."""

    id = "rocket-pool"
    chains = frozenset({"eip155:1"})
    receipts = {
        "eip155:1": (
            ReceiptToken(
                "0xae78736cd615f374d3085123a210448e74fc6393",
                "eip155:1/slip44:60", 18, "getExchangeRate",
            ),
        ),
    }
