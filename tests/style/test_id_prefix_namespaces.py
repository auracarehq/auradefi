"""One id prefix, one recipe, or a registered, documented divergence.

MOTIVATING FINDING (0.1.1 wave 2, `src/auradefi/api/wire.py:63`, seam, major).
Two independently-approved fixes composed into an unreviewed defect. #19 made
the library and `GET /crypto/sync` resolve the SAME tenant (that is its whole
acceptance criterion). #26 made the embed connection id CHAIN-SCOPED, so
`embed.models.derive_connection_id` no longer returns the bytes
`tenancy.models.connection_id` returns for the same descriptor
(`conn_d0327e21d9b0ea55` vs `conn_b116094c537a85e6`). Separately both are
correct and both are pinned in DECISIONS. Together they put two DIFFERENT
`conn_`-prefixed namespaces inside ONE tenant's client-visible view: the sync
envelope's `account_id` (wire.py:63, 81, 143) carries the chain-scoped id the
library stamped, while `POST/GET /connections` hands the client the chainless
`tenancy.connection_id` (routes/connections.py:78). A client cannot join a
transaction to the connection that produced it, and nothing failed, because
each half has its own green tests and the ids are lexically indistinguishable:
same prefix, same 16 hex digits.

WHY THE CLASS IS DANGEROUS. Every id in this codebase is an opaque
`<prefix>_<16 hex>` string, so the prefix is the ONLY thing a reader, human or
client, can use to tell one namespace from another. When two mint sites share
a prefix there are exactly two possible intents, and both fail silently when
they rot:

* the recipes are meant to be IDENTICAL (`ledger.models.transaction_id` /
  `decode.models.transaction_id` under DECISIONS' duplication waiver;
  `embed.models.derive_tenant_id` / `tenancy.models.end_user_id`, whose
  byte-equality IS #19). Nothing in the language couples them: the day one
  preimage gains a segment, the two surfaces address different rows and every
  per-module test stays green.
* the recipes are meant to DIVERGE (`conn_` after #26; `grp_` for a
  positions risk-unit vs an assets aggregation bucket). Then the collision is
  the hazard: values from two namespaces flow into one map, one wire field, or
  one client's join key, and look like peers.

THE RULES, mechanically. This gate inventories every id mint site in
`src/auradefi/`, a `return "<prefix>_" + …` or `return f"<prefix>_{…}"`, and
derives each site's RECIPE from the preimage/join literals in its function
(placeholders erased, so `f"{a}|{b}"` and `f"{x}|{y}"` are the same recipe and
`f"{a}|address|{b}"` is not). For every prefix minted at two or more sites:

1. RECIPES THAT MATCH MUST BE CROSS-PINNED. Some ONE file under `tests/` must
   name both minting packages, the mint function, and a golden literal of that
   prefix. A single place that goes red when one copy drifts. (Rule 2 in
   spirit is DECISIONS' duplication waiver made enforceable.)
2. RECIPES THAT DIFFER MUST BE REGISTERED HERE, in `DIVERGENT_PREFIXES`, with
   an anchor phrase that must literally appear in `docs/internal/DECISIONS.md`. An
   unregistered divergence is the wire.py:63 defect; a registered one whose
   DECISIONS anchor has been deleted is the same defect with the reasoning
   thrown away.

Registering a divergence is deliberately cheap and deliberately visible: the
cost is one entry plus one documented sentence, and the reward is that the next
person who adds a third `conn_` recipe cannot do it silently.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO / "src" / "auradefi"
TEST_ROOT = REPO / "tests"
DECISIONS = REPO / "docs" / "internal" / "DECISIONS.md"

#: A mint prefix literal: `conn_`, `txn_`, `usr_`, … lowercase, trailing `_`.
_PREFIX = re.compile(r"^([a-z]{2,8})_$")


@dataclass(frozen=True)
class MintSite:
    """One place a new opaque id string is minted."""

    package: str  # e.g. "auradefi.embed"
    rel_path: str
    line: int
    func: str
    prefix: str
    recipe: tuple[str, ...]  # placeholder-erased preimage/join literals

    def __str__(self) -> str:  # pragma: no cover - failure messages only
        return f"{self.rel_path}:{self.line} {self.func}() -> {self.prefix}_"


def _prefix_of(node: ast.expr) -> str | None:
    """The mint prefix a `return` expression starts with, or None.

    Two shapes, both used in this codebase: `"conn_" + digest[:16]` and
    `f"txn_{digest[:16]}"`.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = node.left
        if isinstance(left, ast.Constant) and isinstance(left.value, str):
            match = _PREFIX.match(left.value)
            return match.group(1) if match else None
        return None
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            match = _PREFIX.match(head.value)
            return match.group(1) if match else None
    return None


def _erase(node: ast.JoinedStr) -> str:
    """`f"{project_id}|{kind}"` -> `"{}|{}"`: structure without names.

    Renaming a parameter must not read as a recipe change; adding or moving
    a `|`-segment must.
    """
    out: list[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            out.append(part.value)
        else:
            out.append("{}")
    return "".join(out)


def _recipe(func: ast.FunctionDef, prefix: str) -> tuple[str, ...]:
    """Every literal that shapes the PREIMAGE, sorted.

    Exactly two contributors, and nothing else: a validation regex or an
    error message must not read as part of the recipe:

    * f-string templates, placeholders erased (`f"{project_id}|{kind}"`);
    * the separator of a `"\\n".join(...)`, so a join-built preimage
      (`assets.groups.group_id`) never collides with an f-string one.

    The docstring is skipped, and so is the prefix literal itself.
    """
    body = func.body[1:] if ast.get_docstring(func) is not None else func.body
    literals: set[str] = set()
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.JoinedStr):
                erased = _erase(node)
                if erased != f"{prefix}_":
                    literals.add(erased)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "join"
                and isinstance(node.func.value, ast.Constant)
                and isinstance(node.func.value.value, str)
            ):
                literals.add(f"join({node.func.value.value!r})")
    return tuple(sorted(literals))


def _mint_sites(root: Path) -> list[MintSite]:
    """Inventory every id mint site under `root`."""
    sites: list[MintSite] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path)
        module_parts = path.relative_to(root).with_suffix("").parts
        package = ".".join((root.name, *module_parts[:-1])) or root.name
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef):
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Return) or node.value is None:
                    continue
                prefix = _prefix_of(node.value)
                if prefix is None:
                    continue
                sites.append(
                    MintSite(
                        package=package,
                        rel_path=rel,
                        line=node.lineno,
                        func=func.name,
                        prefix=prefix,
                        recipe=_recipe(func, prefix),
                    )
                )
    return sites


def _by_prefix(sites: list[MintSite]) -> dict[str, list[MintSite]]:
    grouped: dict[str, list[MintSite]] = {}
    for site in sites:
        grouped.setdefault(site.prefix, []).append(site)
    return {prefix: group for prefix, group in grouped.items() if len(group) > 1}


def divergent_prefixes(sites: list[MintSite]) -> dict[str, list[MintSite]]:
    """Prefixes minted by two or more DIFFERENT recipes."""
    return {
        prefix: group
        for prefix, group in _by_prefix(sites).items()
        if len({site.recipe for site in group}) > 1
    }


def duplicated_prefixes(sites: list[MintSite]) -> dict[str, list[MintSite]]:
    """Prefixes minted twice or more by the SAME recipe."""
    return {
        prefix: group
        for prefix, group in _by_prefix(sites).items()
        if len({site.recipe for site in group}) == 1
    }


#: Divergences reviewed and accepted, each with the phrase docs/internal/DECISIONS.md
#: must carry so the reasoning cannot be deleted without a red test.
#:
#: `conn_`: #26 chain-scoped the embed id; `tenancy.connection_id` stays
#: chainless because rehashing it would orphan every id the HTTP surface has
#: persisted. The composition hazard (both live in one tenant's view; the sync
#: envelope's `account_id` is the chain-scoped one) is what this gate exists
#: to keep visible.
DIVERGENT_PREFIXES: dict[str, tuple[str, ...]] = {
    "conn_": (
        "deliberately no longer byte-equal",
        "chain-scoped",
    ),
    # Pre-existing, found by this gate on the run that introduced it: two
    # unrelated namespaces both mint `grp_`. assets.groups.group_id hashes a
    # sorted SET OF ASSET IDS (a display grouping); positions.models
    # .group_id_for hashes (adapter, chain, risk-unit key). Nothing joins
    # them today: an AssetGroup.id and a PositionGroup.group_id are never
    # compared, so this is registered rather than unified: changing either
    # recipe would rehash live ids to fix a collision nobody has hit. It is
    # registered and not ignored because the shapes are lexically
    # identical, so the day something DOES try to join them, the failure
    # would look like a lookup miss rather than a category error.
    "grp_": (
        "two unrelated `grp_` namespaces",
        "never compared",
    ),
}


def test_divergent_id_prefixes_are_registered_and_documented() -> None:
    """Rule 2: two recipes under one prefix need a reviewed reason."""
    sites = _mint_sites(SOURCE_ROOT)
    divergent = divergent_prefixes(sites)
    unregistered = {
        prefix: group
        for prefix, group in divergent.items()
        if f"{prefix}_" not in DIVERGENT_PREFIXES
    }
    assert not unregistered, (
        "id prefix minted by MORE THAN ONE recipe and not registered in "
        "DIVERGENT_PREFIXES. Two namespaces now share one lexically "
        "indistinguishable id shape (the wire.py:63 defect: the sync "
        "envelope's account_id and /connections' connection_id are both "
        "`conn_<16 hex>` and never equal). Either make the recipes agree, or "
        "register the prefix here and document the divergence in "
        "docs/internal/DECISIONS.md:\n"
        + "\n".join(
            f"  {prefix}_:\n"
            + "\n".join(f"    {site}  recipe={site.recipe}" for site in group)
            for prefix, group in sorted(unregistered.items())
        )
    )
    decisions = DECISIONS.read_text(encoding="utf-8")
    missing = {
        prefix: [anchor for anchor in anchors if anchor not in decisions]
        for prefix, anchors in DIVERGENT_PREFIXES.items()
    }
    missing = {prefix: gone for prefix, gone in missing.items() if gone}
    assert not missing, (
        "a registered id-prefix divergence lost its DECISIONS anchor: the "
        "collision survives, the reason does not. Restore the sentence or "
        "update the anchor here: " + repr(missing)
    )


def test_duplicated_id_recipes_are_cross_pinned() -> None:
    """Rule 1. A copied recipe needs one file that goes red on drift."""
    sites = _mint_sites(SOURCE_ROOT)
    test_texts = {
        path: path.read_text(encoding="utf-8")
        for path in TEST_ROOT.rglob("test_*.py")
        if path.resolve() != Path(__file__).resolve()
    }
    unpinned: dict[str, list[MintSite]] = {}
    for prefix, group in duplicated_prefixes(sites).items():
        packages = {site.package for site in group}
        names = {site.func for site in group}
        golden = re.compile(rf"\b{prefix}_[0-9a-f]{{16}}\b")
        pinned = any(
            all(package in text for package in packages)
            and any(name in text for name in names)
            and golden.search(text)
            for text in test_texts.values()
        )
        if not pinned:
            unpinned[prefix] = group
    assert not unpinned, (
        "an id recipe is duplicated across packages with NO single test that "
        "would notice them drifting apart. A cross-pin file must name both "
        "packages, the mint function, and a golden `<prefix>_<16 hex>` value "
        "(see tests/ledger/test_bridge.py for txn_):\n"
        + "\n".join(
            f"  {prefix}_:\n" + "\n".join(f"    {site}" for site in group)
            for prefix, group in sorted(unpinned.items())
        )
    )


def test_gate_detects_the_motivating_defect(tmp_path: Path) -> None:
    """Proof, on a RECONSTRUCTION, never by editing real source.

    Two modules mint `conn_` from different preimages, exactly as
    `embed.models.derive_connection_id` and `tenancy.models.connection_id` do
    after #26. The inventory must see one prefix with two recipes.
    """
    root = tmp_path / "auradefi"
    (root / "embed").mkdir(parents=True)
    (root / "tenancy").mkdir(parents=True)
    (root / "embed" / "models.py").write_text(
        'import hashlib\n'
        'def derive_connection_id(tenant_id, address, chain_id):\n'
        '    d = hashlib.sha256(\n'
        '        f"embed|{tenant_id}|address|{chain_id}|{address}".encode()\n'
        '    ).hexdigest()\n'
        '    return "conn_" + d[:16]\n',
        encoding="utf-8",
    )
    (root / "tenancy" / "models.py").write_text(
        'import hashlib\n'
        'def connection_id(project_id, end_user_id, kind, descriptor):\n'
        '    d = hashlib.sha256(\n'
        '        f"{project_id}|{end_user_id}|{kind}|{descriptor}".encode()\n'
        '    ).hexdigest()\n'
        '    return "conn_" + d[:16]\n',
        encoding="utf-8",
    )
    sites = _mint_sites(root)
    assert len(sites) == 2, sites
    assert set(divergent_prefixes(sites)) == {"conn"}
    assert not duplicated_prefixes(sites)

    # And the identical-recipe half of the class, which rule 1 covers: same
    # preimage shape in two packages is NOT flagged as divergent, only as a
    # duplicate needing a cross-pin.
    (root / "tenancy" / "models.py").write_text(
        'import hashlib\n'
        'def connection_id(project_id, tenant_id, kind, descriptor):\n'
        '    d = hashlib.sha256(\n'
        '        f"embed|{tenant_id}|address|{kind}|{descriptor}".encode()\n'
        '    ).hexdigest()\n'
        '    return "conn_" + d[:16]\n',
        encoding="utf-8",
    )
    same = _mint_sites(root)
    assert not divergent_prefixes(same)
    assert set(duplicated_prefixes(same)) == {"conn"}
