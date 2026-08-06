"""The shipped tree vs docs/internal/SPEC.md §3.2, diffed both ways (§5 #17).

README's *What is not there* understated the gap: it omitted the whole
``jobs/`` package, five ``api/routes/`` modules, ``project/plaid.py`` and
``native.py``, and ``prices/historian.py`` and ``store.py``. A README that
understates its own gaps is a correctness problem, not a documentation
nicety: rule #10 cuts both ways, and a reader budgeting work off that
section was being told the package was closer to the spec than it is.

Prose cannot be asserted on, so this gate asserts the DIFF instead, in
both directions, against two committed inventories:

* ``DECLARED_BUT_ABSENT``: in the spec's layout, not in the tree. Every
  entry is a documented limitation. Shipping one of these makes this test
  fail, which is the point: the module arriving is exactly when the README
  needs editing.
* ``SHIPPED_BUT_UNDECLARED``: in the tree, not in the spec's layout.
  These are modules the build added after §3.2 was written. Two of them
  (``api/sinks.py``, ``embed/dispatch.py``) were added by 0.1.1 itself
  when stating a seam honestly and containing a failure outgrew their
  parents' line budget.

Either list going stale fails here, so nobody can add or remove a module
without looking at both this inventory and the README section it feeds.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "auradefi"
SPEC = REPO / "docs" / "internal" / "SPEC.md"
README = REPO / "README.md"

#: Declared in §3.2, absent from the tree. Each is a documented limitation.
DECLARED_BUT_ABSENT = frozenset(
    {
        # No background worker at all. The host owns the tick.
        "jobs/scheduler.py",
        "jobs/discover.py",
        "jobs/refresh.py",
        "jobs/reprocess.py",
        "jobs/backfill.py",
        # Five of the nine declared route modules. auth/connections/sync/admin ship.
        "api/routes/accounts.py",
        "api/routes/holdings.py",
        "api/routes/positions.py",
        "api/routes/transactions.py",
        "api/routes/webhooks.py",
        # project/ ships only scalar.py; the Plaid envelope lives in api/wire.py.
        "project/plaid.py",
        "project/native.py",
        # No historical price service and no price store: marks are the caller's.
        "prices/historian.py",
        "prices/store.py",
        "prices/oracles/coingecko.py",
        "prices/oracles/manual.py",
        "prices/oracles/onchain_amm.py",
        # decode/ is pipeline-only: no rule engine, no per-protocol decoders,
        # so acts[] is always one act and protocol is always None.
        "decode/rules.py",
        "decode/acts.py",
        "decode/enrich.py",
        "decode/action_items.py",
        "decode/protocols/registry.py",
        "decode/protocols/transfer/erc20.py",
        "decode/protocols/transfer/native.py",
        "decode/protocols/transfer/nft.py",
        "decode/protocols/amm/uniswap_v2.py",
        "decode/protocols/amm/uniswap_v3.py",
        "decode/protocols/amm/curve.py",
        "decode/protocols/lending/aave.py",
        "decode/protocols/lending/compound.py",
        "decode/protocols/lending/morpho.py",
        "decode/protocols/staking/lido.py",
        "decode/protocols/staking/rocketpool.py",
        "decode/protocols/staking/solana_stake.py",
        # No enhanced-transaction Solana source, so no Solana tx decode.
        "sources/solana/helius.py",
        # Position adapters the spec names that no golden vector covers.
        "positions/adapters/erc4626.py",
        "positions/adapters/amm/curve.py",
        "positions/adapters/lending/compound.py",
        "positions/adapters/staking/native.py",
    }
)

#: In the tree, absent from §3.2. Added after the layout was written.
SHIPPED_BUT_UNDECLARED = frozenset(
    {
        "accounting/report.py",
        "api/app.py",
        "api/sinks.py",  # 0.1.1 §5 Wave C: the webhook seam, split out of deps.py
        "api/wire.py",
        "decode/models.py",
        # The developer-experience change: nothing shipped a DEFAULT for any
        # port, so the shortest working program began with forty lines of
        # adapter. These four give `Auradefi.sandbox()` / `.from_env()` a
        # wiring to hand back, and split what no longer fits the line budget.
        "embed/bootstrap.py",  # default port sets for sandbox + live
        "embed/dispatch.py",  # 0.1.1 §5 #24: budget dispatch + containment
        "embed/facade.py",
        "embed/handle.py",  # UserHandle, split off at facade.py's 400-line cap
        "embed/models.py",
        "embed/state.py",
        "embed/sync.py",
        "ledger/backends/models.py",
        "ledger/bridge.py",
        "portfolio/holdings.py",
        "portfolio/models.py",
        "positions/models.py",
        "sources/bitcoin/encoding.py",
        # 0.2.0 phase 11 (#1, #2, #3). §3.2's `sources/evm/` names the four
        # HTTP modules and nothing else, because it did not anticipate that
        # "no new third-party dependencies" makes the codec ours to write:
        # `hashlib.sha3_256` is not keccak256, so a function selector cannot
        # come from the stdlib. `reader.py` is undeclared for a different
        # reason: it binds `positions.protocol.ContractReader` structurally,
        # and §3.2 put the protocol in `positions/` without naming who
        # implements it.
        "sources/evm/codec/abi.py",
        "sources/evm/codec/keccak.py",
        "sources/evm/reader.py",
        "sources/evm/source.py",  # both seams over one client, so a host writes none
        "sources/evm/txfetch.py",
        "sources/evm/txlist.py",
        "sources/sandbox.py",  # the Sandbox environment's replay transport
        "tenancy/store.py",
        "testing/cassettes.py",
        "webhooks/urls.py",
    }
)

#: Each absent GROUP and a phrase README's gap section must contain for it.
#: This is the #17 complaint made mechanical: the four things it named were
#: missing from the prose entirely.
README_MUST_MENTION = {
    "jobs/": "jobs/",
    "api/routes/": "api/routes/",
    "project/": "project/",
    "prices/ history": "historian",
}


def _declared_modules() -> set[str]:
    """Every ``*.py`` the §3.2 tree block names, as a package-relative path."""
    lines = SPEC.read_text(encoding="utf-8").split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("### 3.2"))
    block: list[str] = []
    inside = False
    for line in lines[start:]:
        if line.strip() == "```":
            if inside:
                break
            inside = True
            continue
        if inside:
            block.append(line)

    declared: set[str] = set()
    package = ""
    for line in block:
        if line.startswith("tests/"):
            break
        match = re.match(r"^(\s*)([a-z_0-9]+)/\s*(.*)$", line)
        if match:
            indent, name, rest = match.groups()
            if name == "src":
                continue
            parts = package.split("/") if package else []
            package = "/".join(parts[: max(0, (len(indent) - 2) // 2)] + [name])
            tail = rest
        else:
            tail = line
        for module in re.findall(r"([a-z_0-9]+)\.py", tail):
            if module == "__init__":
                continue
            declared.add(f"{package}/{module}.py" if package else f"{module}.py")
    return declared


def _shipped_modules() -> set[str]:
    return {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path.name != "__init__.py"
    }


def _gap_section() -> str:
    """README's *What is not there* section, verbatim.

    The heading is located by its words with emphasis markers stripped, so
    that restyling it (``What is **not** there`` to ``What is not there``)
    moves no test. Anchoring on decorative markup made this gate fail for a
    reason that had nothing to do with the gap prose it exists to guard.
    """
    text = README.read_text(encoding="utf-8")
    heading = next(
        line
        for line in text.split("\n")
        if line.replace("*", "").strip() == "### What is not there"
    )
    start = text.index(heading)
    end = text.index("\n## ", start)
    return text[start:end]


def test_the_spec_declares_a_layout_this_test_can_read():
    # A parse that silently found nothing would make every assertion below
    # vacuously true: the failure mode this whole gate exists to prevent.
    declared = _declared_modules()
    assert len(declared) > 90, f"parsed only {len(declared)} declared modules"
    assert "jobs/scheduler.py" in declared
    assert "api/routes/auth.py" in declared


def test_every_declared_absence_is_inventoried():
    missing = _declared_modules() - _shipped_modules()
    assert missing == DECLARED_BUT_ABSENT, (
        "the spec-vs-tree gap moved and the inventory did not.\n"
        f"newly absent: {sorted(missing - DECLARED_BUT_ABSENT)}\n"
        f"now shipped:  {sorted(DECLARED_BUT_ABSENT - missing)}\n"
        "Update DECLARED_BUT_ABSENT *and* README's 'What is not there': a "
        "module arriving or leaving is exactly when that prose goes stale."
    )


def test_every_undeclared_module_is_inventoried():
    extra = _shipped_modules() - _declared_modules()
    assert extra == SHIPPED_BUT_UNDECLARED, (
        "a module exists that docs/internal/SPEC.md §3.2 does not declare and this "
        "inventory does not list.\n"
        f"new: {sorted(extra - SHIPPED_BUT_UNDECLARED)}\n"
        f"gone: {sorted(SHIPPED_BUT_UNDECLARED - extra)}\n"
        "Either add it to §3.2 or record it here with the reason."
    )


def test_the_readme_names_every_absent_group():
    # The #17 complaint, mechanically: these four were absent from the tree
    # AND absent from the prose, so a reader could not learn they were gaps.
    section = _gap_section()
    unmentioned = [
        group for group, phrase in README_MUST_MENTION.items()
        if phrase not in section
    ]
    assert not unmentioned, (
        "README's 'What is not there' does not mention these absent parts of "
        f"the declared layout: {unmentioned}"
    )


def test_the_gap_section_is_not_silently_emptied():
    section = _gap_section()
    assert len(section.split("\n- ")) >= 8, (
        "the gap section lost entries; it must state every limitation, and "
        "shrinking it is how a README starts overstating what ships"
    )
