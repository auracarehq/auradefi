"""The Sandbox environment: the real library, over a recording, no keys.

Plaid's Sandbox is the reason a developer can finish their quickstart
before anyone has approved their account, and the same problem applies
here with one extra turn of the screw: this package has no hosted service
to point a test key at. Every published example therefore built its own
fixture inline — tens of lines of cassette JSON before the first real call
— which taught readers the fixture format instead of the library.

So the fixture ships. ``client()`` returns an ``httpx.Client`` whose
transport replays ``fixtures/sandbox.json``: a committed recording of one
address' Etherscan V2 and DefiLlama traffic, bundled in the wheel. Every
code path above the transport is the production one — the same source, the
same decoder, the same ledger, the same pricing — so what Sandbox exercises
is the library, not a mock of it.

What the recording contains, and therefore what Sandbox can answer:

* ``SANDBOX_ADDRESS`` on ``SANDBOX_CHAIN``, holding 2 ETH and 25 USDC,
  priced at 2500 and 1 USD -> **5025 USD** exactly;
* seven transactions across the block window 100-107, pageable two at a
  time (``SANDBOX_PAGE_SIZE``), which is enough to show the anchor page,
  the backfill and a resumed tick;
* one price request covering both assets.

Anything else raises ``CassetteMissError`` — a different address, a second
chain, a wider page. That is the offline guarantee doing its job, not a
bug, and the error names every request the recording holds.

The replay matcher itself lives in ``auradefi.testing.cassettes`` and is
reused rather than reimplemented: one matcher, one set of semantics, and
the module a host already uses to test its own integration.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from auradefi.testing.cassettes import load

#: The recording, inside the installed package.
FIXTURE = Path(__file__).parent / "fixtures" / "sandbox.json"

#: The one address the recording covers, and the chain it is on.
SANDBOX_ADDRESS = "0x1111111111111111111111111111111111111111"
SANDBOX_CHAIN = "eip155:1"

#: The opaque host user id Sandbox connects that address under. Any string
#: works — this one is fixed so the derived ``usr_``/``conn_`` ids in the
#: documentation stay stable between runs.
SANDBOX_USER = "sandbox-user"

#: The instant the traffic was recorded at. Sandbox freezes here so every
#: answer — ``as_of_ms``, quota windows, sync throttling — is reproducible.
SANDBOX_NOW_MS = 1_754_000_000_000

#: The page size the history was recorded with. A different value asks for
#: a window the recording does not hold.
SANDBOX_PAGE_SIZE = 2

#: What the recording prices the two assets at, for docs that quote it.
SANDBOX_TOTAL_USD = "5025"


def client(**kwargs: object) -> httpx.Client:
    """An ``httpx.Client`` that replays the bundled recording.

    Opens no socket: the transport is a ``MockTransport``, so this works
    with no network, no keys and no configuration. ``kwargs`` reach
    ``httpx.Client`` untouched (``timeout=`` and friends); passing
    ``transport=`` collides and is the caller's mistake.

    Raises ``CassetteError`` if the fixture is missing from the install,
    which would mean a broken wheel rather than a usage error.
    """
    return load(FIXTURE).client(**kwargs)
