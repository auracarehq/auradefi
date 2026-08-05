"""Chain data sources: raw chain bytes to typed records (SPEC §3.3).

Knows HTTP, RPC, ABIs; knows nothing about positions or fiat. No shared
Source protocol yet. The abstraction arrives with the third caller
(SPEC §5.4). Docstring-only __init__: import concrete modules, e.g.
Auradefi.sources.evm.etherscan.
"""
