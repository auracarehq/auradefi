"""How do I make each error happen on purpose, so I can test my handler?

    pip install auradefi
    python examples/11_provoke_every_error.py

Reading which errors exist tells you nothing about whether your `except`
clause works. This file causes sixteen of them deliberately, offline, in
three lines each, so you can copy the trigger into your own test suite and
watch your handler run.

Every trigger below is deterministic. None needs a key, a network or a
database, and none depends on an upstream service being in a bad mood.

The errors are grouped by whose problem they are, because that is what
decides what you do about one:

    your call is wrong      fix the code, the input never reaches a service
    your data disagrees     two values that cannot be combined met
    a credential is bad     your customer's token or your quota, not a bug
    upstream said no        the network, or a recording that lacks a request

Over HTTP each of these becomes `{"error": {"type", "message", "status",
"docs_url"}}`, and `docs_url` is the errors page anchored at the row for
that type. This file prints the anchor beside each one, so you can see the
correspondence between the exception you catch in Python and the body your
own API clients will read.
"""

from __future__ import annotations

from decimal import Decimal

from auradefi import Auradefi
from auradefi.assets.registry import AssetRegistry
from auradefi.clock import FrozenClock
from auradefi.config import Settings
from auradefi.errors import (
    AuradefiError,
    AuthError,
    CaipParseError,
    CassetteError,
    CassetteMissError,
    ConfigError,
    ConflictError,
    CurrencyMismatchError,
    CursorError,
    NotFoundError,
    QuotaExceededError,
    SourceError,
    TokenExpiredError,
    TokenRevokedError,
    UnknownAssetError,
    UnknownChainError,
    ValidationError,
)
from auradefi.ledger.backends.memory import MemoryLedger
from auradefi.money.fiat import Money
from auradefi.tenancy.quota import QuotaCounter, QuotaLimits
from auradefi.tenancy.tokens import RevocationSet, mint_token, verify_token
from auradefi.testing.cassettes import load

#: The address the bundled Sandbox recording holds. Anything else misses.
RECORDED = "0x1111111111111111111111111111111111111111"
UNRECORDED = "0x2222222222222222222222222222222222222222"

#: The page every error body links to. `api/errors.py` builds this from the
#: exception's own class, so it is right without a table to maintain.
DOCS = "https://auradefi.info/errors.html"

NOW = 1_754_000_000_000
SECRET = "s" * 64


def provoke(expected: type[AuradefiError], label: str, trigger) -> None:
    """Run `trigger`, require `expected`, and show what a caller would see."""
    try:
        trigger()
    except AuradefiError as raised:
        assert isinstance(raised, expected), (
            f"{label}: expected {expected.__name__}, got {type(raised).__name__}"
        )
        name = type(raised).__name__
        print(f"  {name:<22} {str(raised)[:58]}")
        print(f"  {'':<22} {DOCS}#{name.lower()}")
        return
    raise AssertionError(f"{label}: nothing raised, so nothing was proved")


# ------------------------------------------------------- your call is wrong
# Every one of these is caught before a request leaves the process, so a
# wrong chain id or a zero budget costs you nothing but the exception.

print("your call is wrong: fix the code, no request was made")

aura = Auradefi.sandbox()
user = aura.user("demo-user")

provoke(CaipParseError, "a chain name instead of a CAIP-2 id",
        lambda: user.connect_address("ethereum", RECORDED))

provoke(UnknownChainError, "a CAIP-2 id the registry was never given",
        lambda: user.connect_address("eip155:999999", RECORDED))

provoke(ValidationError, "a budget that cannot fetch a page",
        lambda: aura.sync(budget=0))

# This one succeeds, and the next one needs it to have succeeded.
user.connect_address("eip155:1", RECORDED)

provoke(ConflictError, "the same address on the same chain twice",
        lambda: user.connect_address("eip155:1", RECORDED))

ledger = MemoryLedger()
provoke(NotFoundError, "a transaction id nobody stored",
        lambda: ledger.get("usr_absent", "txn_nope"))

provoke(CursorError, "a cursor the caller made up",
        lambda: ledger.sync("usr_absent", cursor="page-2"))

provoke(ConfigError, "a timeout that is not a number",
        lambda: Settings.from_env({"AURADEFI_HTTP_TIMEOUT_S": "soon"}))

# A 409 is the one refusal that hands back something usable: the id of the
# connection you already have, so a retry can adopt it instead of guessing.
try:
    user.connect_address("eip155:1", RECORDED)
except ConflictError as conflict:
    assert conflict.existing_id is not None
    assert conflict.existing_id.startswith("conn_")
    print(f"\n  the 409 names what you already own: {conflict.existing_id}")


# ------------------------------------------------------- your data disagrees
# Two values met that cannot be combined. Nothing is broken upstream and
# nothing is wrong with your credentials.

print("\nyour data disagrees: two values that cannot be combined met")

provoke(CurrencyMismatchError, "adding two currencies",
        lambda: Money(Decimal("1"), "USD") + Money(Decimal("1"), "GBP"))

provoke(UnknownAssetError, "an asset id that was never registered",
        lambda: AssetRegistry().get_by_id("ast_deadbeefdeadbeef"))


# --------------------------------------------------------- a credential is bad
# These are your customer's problem to re-authenticate, or your quota to
# raise. None of them is a defect, and all four are 4xx over HTTP.

print("\na credential is bad: 4xx over HTTP, and not a bug")

clock = FrozenClock(NOW)
token = mint_token(
    project_id="proj_demo",
    external_user_id="end-user-1",
    scopes=("accounts:read",),
    signing_secret=SECRET,
    clock=clock,
    ttl_ms=60_000,
)

provoke(AuthError, "a token verified against the wrong project's secret",
        lambda: verify_token(token, signing_secret="w" * 64, clock=clock))

provoke(TokenExpiredError, "a token read after its expiry",
        lambda: verify_token(token, signing_secret=SECRET,
                             clock=FrozenClock(NOW + 3_600_000)))

revoked = RevocationSet()
revoked.revoke(verify_token(token, signing_secret=SECRET, clock=clock).jti)
provoke(TokenRevokedError, "a token whose jti was revoked",
        lambda: verify_token(token, signing_secret=SECRET, clock=clock,
                             revoked=revoked))

quota = QuotaCounter(QuotaLimits(per_second=1, per_day=10, per_month=100), clock)
quota.hit("proj_demo")
provoke(QuotaExceededError, "the second request in a one-per-second window",
        lambda: quota.hit("proj_demo"))


# ------------------------------------------------------------ upstream said no
# The only group that is about the world outside your process.

print("\nupstream said no: the network, or a recording without the request")


class RefusingSource:
    """A source whose upstream can be taken down mid-run.

    Both seams raise `SourceError`, which is the contract: raise that (or
    any `AuradefiError`) for an upstream problem and `sync()` files it
    against the one connection it belongs to.
    """

    def __init__(self, up: bool = True) -> None:
        self.up = up

    def balances(self, chain_id: str, address: str) -> list:
        if self.up:
            return []
        raise SourceError("etherscan balance error: message='NOTOK'")

    def fetch_txlist(self, chain_id, address, **window) -> list[dict]:
        if self.up:
            return []
        raise SourceError("etherscan txlist error: message='NOTOK'")


class EmptyPrices:
    """One method, and returning nothing is allowed rather than an error."""

    def usd_prices(self, caip19s) -> dict:
        return {}


provoke(SourceError, "an upstream that refuses",
        lambda: RefusingSource(up=False).balances("eip155:1", RECORDED))

provoke(CassetteMissError, "asking Sandbox for an address it never recorded",
        lambda: aura.user("other").connect_address("eip155:1", UNRECORDED))

provoke(CassetteError, "a cassette file that is not there",
        lambda: load("no/such/recording.json"))


# ----------------------------------------- one except clause catches them all
# Every type above inherits AuradefiError, so a host that wants to catch this
# library and nothing else needs exactly one clause. Catching narrower is how
# you tell a caller mistake from an upstream failure.

caught = []
for trigger in (
    lambda: user.connect_address("ethereum", RECORDED),
    lambda: Money(Decimal("1"), "USD") + Money(Decimal("1"), "GBP"),
    lambda: RefusingSource(up=False).balances("eip155:1", RECORDED),
    lambda: load("no/such/recording.json"),
):
    try:
        trigger()
    except AuradefiError as raised:
        caught.append(type(raised).__name__)

assert len(caught) == 4, caught
print(f"\none `except AuradefiError` caught all four: {', '.join(caught)}")


# ------------------------------------------------ where the error reaches you
# The same SourceError arrives in two different places depending on when the
# upstream broke, and the difference is deliberate.

print("\nthe same error, in the two places it can reach you")

# 1. At connect time. `connect_address` spends one single-row request as a
#    liveness probe, so a dead endpoint or a bad key surfaces while your user
#    is still on the screen instead of on a tick tomorrow.
dead = RefusingSource(up=False)
early = Auradefi(MemoryLedger(), dead, EmptyPrices())
provoke(SourceError, "a dead upstream at connect time",
        lambda: early.user("tenant-a").connect_address("eip155:1", RECORDED))

# 2. On a later tick. Connect while it is healthy, lose it afterwards, and
#    sync() files the failure against that one connection: its siblings keep
#    their share of the budget and the tick still reports.
source = RefusingSource(up=True)
later = Auradefi(MemoryLedger(), source, EmptyPrices())
later.user("tenant-a").connect_address("eip155:1", RECORDED)
source.up = False
report = later.sync(budget=5)

assert report.failed_connections, "a refusing source must be reported failed"
assert not report.no_op, "a failure is never a success-shaped no-op"
print(f"  contained on the tick: {len(report.failed_connections)} connection "
      f"failed, no_op={report.no_op}, and sync() returned a report")

print("\nOK: sixteen errors, all on purpose, none of them a surprise.")
