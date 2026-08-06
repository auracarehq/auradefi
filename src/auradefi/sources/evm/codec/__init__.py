"""Pure EVM encoding: keccak256 and the ABI codec. No HTTP, no state.

Split out of ``sources/evm/`` for two reasons. The directory cap is ten
modules (``tests/style/test_structure.py``) and phase 11 adds four
HTTP-facing ones; and nothing here opens a socket, so keeping the
encoding beside the transport would have buried the only part of the EVM
path that is verifiable against published vectors alone.

``hashlib.sha3_256`` is NOT keccak256. The two differ in their padding
byte, so a selector computed with the stdlib SHA-3 addresses a different
function than the one you named. That is why :mod:`keccak` exists at all
under a rule forbidding new dependencies.
"""
