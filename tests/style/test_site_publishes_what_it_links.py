"""A link that should be a page must not silently become an outbound one.

MOTIVATING DEFECT CLASS. `scripts/site_render.rewrite_links` maps a
repo-relative markdown link to its published page, and falls back to a
GitHub blob URL when there is no page. That fallback is correct for source
files and for the design documents in `INTERNAL_DOCS` — and it is the
perfect hiding place for a regression, because the href it emits is
syntactically valid and resolves to a real web address.

So when a document is renamed, moved, or dropped from `build_site.collect()`
while other pages still link to it, three things all pass: the site builds,
`scripts/check_site_links.py` finds no broken internal links (it skips
anything containing `://`), and CI is green. The only symptom is that a
reader clicking "Quickstart" leaves the documentation for a GitHub file
view. Nothing else in this repository can see that.

THE RULE. Every repo-relative link in a PUBLISHED markdown source must
resolve to either a published page or a deliberately-unpublished document
named in `INTERNAL_DOCS`. Anything else fails here, naming the link.

This also pins the relocation of the design documents: SPEC, DECISIONS,
STATUS, RELEASING, RELEASE_0.1.1 and AGENT_PROMPTS live under
`docs/internal/` and are cited, never published. Moving one back onto the
site means adding it to `collect()` AND removing it from `INTERNAL_DOCS`,
in one edit, on purpose.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

from site_render import (  # noqa: E402  (path set above)
    INTERNAL_DOCS,
    markdown,
    page_for,
    unpublished_targets,
)

#: The markdown sources the site publishes, and the repo-relative name each
#: is rendered under (which is what relative links inside it resolve against).
PUBLISHED_SOURCES = (
    "README.md",
    "CHANGELOG.md",
    "examples/README.md",
    "docs/quickstart.md",
    "docs/authentication.md",
    "docs/bring-your-own.md",
    "docs/schema.md",
)


def test_every_published_source_exists() -> None:
    missing = [name for name in PUBLISHED_SOURCES if not (REPO / name).is_file()]
    assert not missing, f"published sources that are gone: {missing}"


def test_published_pages_never_link_out_by_accident() -> None:
    offenders: list[str] = []
    for name in PUBLISHED_SOURCES:
        body = markdown().render((REPO / name).read_text(encoding="utf-8"))
        for target in unpublished_targets(body, name):
            # A source file or a directory is a legitimate outbound link.
            path = REPO / target
            if path.is_file() and path.suffix not in {".md", ".ipynb"}:
                continue
            if path.is_dir():
                continue
            offenders.append(f"{name} -> {target}")
    assert not offenders, (
        "links that silently became GitHub URLs instead of site pages — "
        "either publish the target in build_site.collect() or declare it in "
        "site_render.INTERNAL_DOCS:\n  " + "\n  ".join(offenders)
    )


def test_the_internal_documents_are_where_they_say_they_are() -> None:
    """A stale INTERNAL_DOCS entry would exempt a link that is simply broken."""
    missing = [name for name in sorted(INTERNAL_DOCS) if not (REPO / name).is_file()]
    assert not missing, (
        "INTERNAL_DOCS names files that do not exist, so links to them are "
        f"exempted from every check for nothing: {missing}"
    )


def test_the_internal_documents_are_not_published() -> None:
    published = [name for name in sorted(INTERNAL_DOCS) if page_for(name) is not None]
    assert not published, (
        "a document is both declared internal and published, so the site and "
        f"the exemption disagree: {published}"
    )


def test_no_design_document_sits_in_the_readers_path() -> None:
    """The repo root is a landing page; a spec and a build log are not."""
    strays = sorted(
        path.name
        for path in REPO.glob("*.md")
        if path.name not in {"README.md", "CHANGELOG.md"}
    )
    assert not strays, (
        "markdown at the repo root that is neither the README nor the "
        f"changelog — design and process documents belong in docs/internal/: {strays}"
    )
