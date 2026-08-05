"""Bitcoin family constants (SPEC §4.2).

CAIP-2 for bip122 chains uses the first 32 lowercase hex chars of the
genesis block hash as the reference: a fact about the chain itself, never
a vendor name. SLIP-44 coin type 0 keys the native-asset CAIP-19
(``bip122:.../slip44:0``).
"""

from __future__ import annotations

MAINNET = "bip122:000000000019d6689c085ae165831e93"
TESTNET = "bip122:000000000933ea01ad0ee984209779ba"
SLIP44 = 0
