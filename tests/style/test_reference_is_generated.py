"""The generated reference must describe the surface the package HAS.

Signatures, types, defaults and field lists come from `inspect`, so they
cannot drift. Two things in `scripts/site_reference.py` are hand-written and
therefore can:

1. **The symbol list.** A renamed or deleted public symbol would take a
   reference page with it, and because the build imports by string, the
   failure is an ImportError at deploy time rather than something a reader
   ever sees. Better to fail here.
2. **`PARAM_DOCS`.** Prose per parameter cannot be derived from a narrative
   docstring. A documented parameter that no longer exists in the signature
   is a lie published beside a correct type, which is worse than no prose:
   the page looks authoritative and the reader trusts it.

A parameter with NO prose is allowed, it renders as ", ", because the
alternative is a gate that blocks adding a keyword argument. A documented
parameter that is not real is never allowed.

This file also pins that every authored `.html` link in the published
markdown resolves to a page the build actually emits, which nothing else
can check: `rewrite_links` passes those hrefs through untouched, and the
rendered-link checker only runs after a build.

THE THIRD DERIVED-VS-AUTHORED PAIR is `docs_url`. Every HTTP error body
carries `https://<domain>/errors.html#<lowercased type>`, built in
`api/errors.py`, and the anchor it names is emitted by
`scripts/site_errors_http.py` from the same class tree. Two files, one
convention, no import between them: the library cannot import the build
script and the build script does not read the constant. So a renamed error
class, a moved page or a changed domain breaks the link silently, and a
404 in an error body reads to a client as the error type not existing.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

from site_reference import PARAM_DOCS, POSSIBLE_VALUES, SECTIONS, _load  # noqa: E402


def _all_targets() -> list[str]:
    return [target for _, _, group in SECTIONS for target in group]


def test_the_reference_covers_the_surface_a_host_touches() -> None:
    """A sanity floor: the list must not quietly shrink to nothing."""
    targets = _all_targets()
    assert len(targets) >= 25, f"only {len(targets)} symbols documented"
    assert len(set(targets)) == len(targets), "a symbol is listed twice"


@pytest.mark.parametrize("target", _all_targets())
def test_every_documented_symbol_still_exists(target: str) -> None:
    obj, qualname = _load(target)          # raises if it moved or was renamed
    assert obj is not None, target
    assert getattr(obj, "__doc__", None), (
        f"{qualname} has no docstring, so its reference page would be a bare "
        "signature: the page is generated, the prose is not"
    )


def _signature_names(owner: str, member_name: str) -> set[str] | None:
    """Parameter names of `owner.member_name`, or None if there is no such thing."""
    for target in _all_targets():
        obj, qualname = _load(target)
        if qualname != owner:
            continue
        if inspect.isclass(obj):
            member = getattr(obj, member_name, None)
            if member is None:
                return None
            member = member.__func__ if isinstance(member, classmethod) else member
        else:
            member = obj
        try:
            return set(inspect.signature(member).parameters)
        except (TypeError, ValueError):
            return None
    return None


def test_param_docs_name_only_real_parameters() -> None:
    strays: list[str] = []
    for key, documented in PARAM_DOCS.items():
        owner, _, member = key.rpartition(".")
        names = _signature_names(owner, member) if owner else _signature_names(key, key)
        if names is None:
            strays.append(f"{key} (no such callable in the reference)")
            continue
        for name in documented:
            if name not in names:
                strays.append(f"{key}({name}): signature has {sorted(names)}")
    assert not strays, (
        "PARAM_DOCS documents parameters that do not exist; the page would "
        "publish prose for an argument nobody can pass:\n  " + "\n  ".join(strays)
    )


def test_possible_values_name_only_real_parameters() -> None:
    strays = []
    for key in POSSIBLE_VALUES:
        *owner_parts, member, param = key.split(".")
        owner = ".".join(owner_parts) or member
        names = _signature_names(owner, member)
        if names is None or param not in names:
            strays.append(key)
    assert not strays, f"POSSIBLE_VALUES entries with no matching parameter: {strays}"


def test_authored_html_links_resolve_to_real_pages() -> None:
    """`.html` hrefs are passed through unrewritten, so only this can check them."""
    import re

    from build_site import collect

    pages = {page.path for page in collect(run_examples=False)}
    pages.add("openapi.json")               # written beside the HTTP page

    sources = {
        "docs/quickstart.md": "quickstart.html",
        "docs/authentication.md": "authentication.html",
        "docs/limits.md": "limits.html",
        "docs/bring-your-own.md": "bring-your-own.html",
        "docs/glossary.md": "glossary.html",
    }
    broken = []
    for source, rendered_at in sources.items():
        text = (REPO / source).read_text(encoding="utf-8")
        directory = Path(rendered_at).parent
        for href in re.findall(r"\]\(([^)]+\.(?:html|json)[^)]*)\)", text):
            target = href.split("#")[0]
            resolved = (directory / target).as_posix().lstrip("./")
            if resolved not in pages:
                broken.append(f"{source} -> {href}")
    assert not broken, (
        "authored links to pages the build does not emit:\n  " + "\n  ".join(broken)
        + f"\n\npages built: {sorted(pages)[:12]} …"
    )


# --------------------------------------------------------------------------
# docs_url: the link every error body carries


def test_the_docs_url_base_is_the_site_this_repository_publishes() -> None:
    """A stale domain here ships a dead link inside every error response."""
    import build_site

    from auradefi.api.errors import DOCS_URL_BASE

    assert DOCS_URL_BASE == f"https://{build_site.CUSTOM_DOMAIN}/errors.html"


def test_the_docs_url_page_is_one_the_build_emits() -> None:
    from build_site import collect

    from auradefi.api.errors import DOCS_URL_BASE

    page = DOCS_URL_BASE.rsplit("/", 1)[-1]
    assert page in {built.path for built in collect(run_examples=False)}


def test_every_error_type_has_the_anchor_its_docs_url_points_at() -> None:
    """The two halves of the convention, checked against each other.

    `api/errors.py` builds `#<lowercased class name>`; `site_errors_http.py`
    emits `id="<lowercased class name>"` per row. Neither imports the other,
    so only this can see them disagree.
    """
    import re as regex

    from site_errors_http import errors_html

    from auradefi import errors
    from auradefi.api.errors import docs_url_for

    served = set(regex.findall(r'<tr id="([^"]+)"', errors_html()))
    assert served, "the errors page emitted no anchored rows at all"

    missing = []
    for name, value in sorted(vars(errors).items()):
        if not (inspect.isclass(value) and issubclass(value, errors.AuradefiError)):
            continue
        anchor = docs_url_for(value("probe")).rsplit("#", 1)[-1]
        if anchor not in served:
            missing.append(f"{name} -> #{anchor}")
    assert not missing, (
        "error types whose docs_url anchor the errors page does not serve, so "
        "the link in the response body lands on the wrong row:\n  "
        + "\n  ".join(missing)
    )
