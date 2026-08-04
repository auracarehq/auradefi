"""Solana source adapters (SPEC §3.2, §10 Solana row).

``spl.py`` is the PURE half: it parses ``getTokenAccountsByOwner``
``jsonParsed`` rows into typed records, aggregates them per mint and
assembles the balance set. It knows no HTTP — the RPC transport lives in
its own module and hands decoded JSON here.

Token-2022 ScaledUiAmount lives HERE, not in ``chains/solana.py``
(docs/DECISIONS.md "Solana ScaledUiAmount detection" — placement waiver:
``chains/`` is a committed Phase-0 surface that may import nothing). The
``raw / 10**decimals`` identity is NOT safe on Solana (SPEC §4.1 warning),
so both the exact ``Quantity`` and the RPC's ``uiAmountString`` are
carried, and the divergence is detected by string comparison alone.
"""
