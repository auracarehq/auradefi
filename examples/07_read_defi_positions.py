"""How do I get DeFi positions — an LP, a loan — and not lie about them?

    pip install auradefi && python 07_read_defi_positions.py

A wallet balance is one number. A DeFi position is a claim on other assets,
sometimes negative, and the way most tools get it wrong is to flatten it too
early. Here the shape is:

    adapter.discover() -> which contracts matter on this chain
    adapter.resolve()  -> RAW positions: quantities, no prices at all
    drill(positions, prices) -> value them, group them, net them
    project_to_synthetic_holdings(drilled) -> the flat Plaid-shaped view

Two properties this file proves rather than asserts in prose:

* **the projection invariant.** A client that knows nothing about DeFi, sums
  `institution_value` over the flat holdings and trusts the number, gets
  EXACTLY the net worth — because debt projects as a negative *quantity* at
  a positive price, never a negative price;
* **re-pricing costs zero chain reads.** Positions carry raw quantities, so
  a new price is a pure recomputation. Nothing to invalidate, nothing stale.

Chain reads go through ONE seam — `call(address, fn, args)`. This file binds
a dict of recorded answers, which is also how the shipped adapters are
tested. **No concrete on-chain reader ships in the package**: there is no
`eth_call` transport and no multicall batcher, so running these adapters
against mainnet means writing that `call` yourself (README, *What is not
there*).
"""

from __future__ import annotations

from decimal import Decimal

from auradefi.money.fiat import Money
from auradefi.positions.adapters.lending.aave import AaveV3Adapter, Market
from auradefi.positions.drill import drill, project_to_synthetic_holdings
from auradefi.positions.models import MetaType, PositionKind, PositionType
from auradefi.positions.protocol import (
    ContractSet,
    DiscoveryContext,
    PositionAdapter,
    ResolveContext,
)
from auradefi.positions.registry import AdapterRegistry
from auradefi.positions.resolve import resolve_all

CHAIN = "eip155:1"
BLOCK = 20_450_000
WALLET = "0x00000000000000000000000000000000000a11ce"

POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
AWETH, DEBT_WETH = "0x4d5f47fa6a74757f35c14fd3a6ef8e3c9bc514e8", "0xea51d7853eefb32b6ee06b1c12e6dcca88be0ffe"
AUSDC, DEBT_USDC = "0x98c23e9d8f34fefb1b7bd6a91b7ff122f4e16f5c", "0x72e95b8931767c79ba4eee721354d6e99a61d004"
ETH = "eip155:1/slip44:60"
USDC = "eip155:1/erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


class RecordedReader:
    """The entire chain-read seam: `call(address, fn, args) -> value`.

    Put your `eth_call` + multicall behind this one method and every shipped
    adapter works. It is the only thing standing between these adapters and
    a live chain.
    """

    def __init__(self, answers: dict) -> None:
        self._answers = dict(answers)
        self.calls: list[tuple] = []

    def call(self, address: str, fn: str, args: tuple = ()) -> object:
        self.calls.append((address.lower(), fn, args))
        return self._answers[(address.lower(), fn, args)]


class MainnetAaveV3(AaveV3Adapter):
    """The shipped adapter, told which markets to look at."""

    markets = (Market(AWETH, DEBT_WETH, ETH, 18),
               Market(AUSDC, DEBT_USDC, USDC, 6))


# SPEC §6.3's worked example: supply 10 ETH, borrow 5,000 USDC. ONE risk unit.
reader = RecordedReader({
    (AWETH, "balanceOf", (WALLET,)): 10 * 10**18,       # 10 ETH supplied
    (DEBT_WETH, "balanceOf", (WALLET,)): 0,
    (AUSDC, "balanceOf", (WALLET,)): 0,
    (DEBT_USDC, "balanceOf", (WALLET,)): 5_000 * 10**6,  # 5,000 USDC borrowed
    (POOL, "getUserAccountData", (WALLET,)): (
        3_584_250_000_000, 500_000_000_000, 2_367_400_000_000,
        8250, 8000, 5_812_500_000_000_000_000,          # …ltv, health factor
    ),
})

adapter = MainnetAaveV3()
assert isinstance(adapter, PositionAdapter)   # a Protocol: no base class needed

# ------------------------------------------------------- 1. discover + resolve
contracts = adapter.discover(DiscoveryContext(chain_id=CHAIN, reader=reader))
supply, borrow = adapter.resolve(
    ResolveContext(chain_id=CHAIN, address=WALLET, reader=reader, block_number=BLOCK),
    contracts,
)

# Raw quantities only. No price reached this layer, so nothing here can go
# stale, and the sign of a debt lives in `meta_type`, not in the number.
assert supply.position_type is PositionType.DEPOSIT
assert borrow.position_type is PositionType.LOAN
assert borrow.kind is PositionKind.CONTRACT_POSITION      # no token to hold
assert supply.underlyings[0].meta_type is MetaType.SUPPLIED
assert borrow.underlyings[0].meta_type is MetaType.BORROWED
assert borrow.underlyings[0].quantity.raw > 0
assert all(under.price is None for under in supply.underlyings + borrow.underlyings)

# The two are ONE risk unit and say so with a shared group id — you cannot
# show the collateral without the debt by accident.
assert supply.group_id == borrow.group_id
print(f"resolved 2 positions in group {supply.group_id}:")
print(f"  {supply.position_type.value:<8} {supply.underlyings[0].quantity} ETH "
      f"({supply.underlyings[0].meta_type.value})")
print(f"  {borrow.position_type.value:<8} {borrow.underlyings[0].quantity} USDC "
      f"({borrow.underlyings[0].meta_type.value})")
print(f"  health factor {supply.group_info.health_factor}, "
      f"ltv {supply.group_info.ltv}")

# Four markets were probed; the two zero balances produced no position at all.
probes = [call for call in reader.calls if call[1] == "balanceOf"]
assert len(probes) == 4
print(f"  {len(probes)} balance probes -> 2 positions (zero balances dropped)")

# ------------------------------------------------------------ 2. value it
prices = {ETH: Money(Decimal("3584.17"), "USD"),
          USDC: Money(Decimal("0.999839"), "USD")}
drilled = drill([supply, borrow], prices)

assert drilled.gross_assets.amount == Decimal("35841.70")
assert drilled.total_debt.amount == Decimal("4999.195")
assert drilled.net_worth.amount == Decimal("30842.505")
print(f"\ngross {drilled.gross_assets}")
print(f"debt  {drilled.total_debt}")
print(f"net   {drilled.net_worth}")

# --------------------------------------------------- 3. the flat projection
holdings = project_to_synthetic_holdings(drilled)
by_asset = {holding.asset_id: holding for holding in holdings}

assert by_asset[ETH].quantity == Decimal("10")
assert by_asset[USDC].quantity == Decimal("-5000")        # the QUANTITY is negative
assert by_asset[USDC].institution_price.amount > 0        # the PRICE never is
naive_total = sum((holding.institution_value.amount for holding in holdings), Decimal("0"))
assert naive_total == drilled.net_worth.amount

print("\nflat holdings a Plaid-only client sees:")
for holding in holdings:
    print(f"  {str(holding.quantity):>7} @ {str(holding.institution_price):>13}"
          f" = {holding.institution_value}")
print(f"  {'sum':>7}   {'':>13}   {naive_total} == net worth "
      "— the invariant, by exact Decimal equality")

# ------------------------------------------------- 4. re-price, zero reads
reads_before = len(reader.calls)
repriced = drill([supply, borrow], {**prices, ETH: Money(Decimal("3600"), "USD")})
rebuilt = sum((holding.institution_value.amount
               for holding in project_to_synthetic_holdings(repriced)), Decimal("0"))
assert repriced.net_worth.amount == Decimal("31000.805") == rebuilt
assert len(reader.calls) == reads_before
print(f"\nETH re-priced at 3600 -> net {repriced.net_worth}, "
      f"{len(reader.calls) - reads_before} extra chain reads")

# --------------------------------------------- 5. one broken adapter, contained
# Register many adapters and resolve them together. A protocol whose ABI
# changed under you fails on its own row: you get every other position plus
# a named failure, never a half-built portfolio presented as whole.
class BrokenAdapter:
    id = "some-protocol"
    chains = frozenset({CHAIN})

    def discover(self, ctx):
        return ContractSet.empty()

    def resolve(self, ctx, contracts):
        raise RuntimeError("upstream ABI changed")


registry = AdapterRegistry()
registry.register(adapter)
registry.register(BrokenAdapter())
outcome = resolve_all(
    registry.adapters(),
    ResolveContext(chain_id=CHAIN, address=WALLET, reader=reader, block_number=BLOCK),
    {"aave-v3": contracts, "some-protocol": ContractSet.empty()},
)
assert [position.id for position in outcome.positions] == [supply.id, borrow.id]
assert [failure.adapter_id for failure in outcome.failures] == ["some-protocol"]
print(f"\nresolve_all over {len(registry.adapters())} adapters: "
      f"{len(outcome.positions)} positions, failures="
      f"{[(f.adapter_id, f.error) for f in outcome.failures]}")

print("\nOK — raw positions, one grouping, and a flat view that still adds up.")
