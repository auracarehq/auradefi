"""Build the published docs site into `dist/site/` — no network, no CDN.

    pip install '.[docs]'
    python scripts/build_site.py            # then open dist/site/index.html
    python scripts/build_site.py --no-run   # skip executing the examples

Everything published here already lives in the repository: the README, the
examples (each one EXECUTED at build time, with its real output captured
onto the page), the twelve PyBooks with their stored outputs, and the
reference documents. A page whose source moves or whose example breaks fails
this build rather than going stale on the web.

Deployed to GitHub Pages by `.github/workflows/pages.yml`. The output is
self-contained static HTML: one stylesheet, no web fonts, no third-party
requests of any kind, and the only script is a dozen inline lines for the
light/dark toggle. It therefore works offline, from a file:// path, and
behind any CSP that allows inline script.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from site_render import (  # noqa: E402  (path set above so this runs from anywhere)
    PAGE_FOR,
    REPO_URL,
    pygments_css,
    render_example,
    render_markdown,
    render_notebook,
)

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "dist" / "site"
STYLE = Path(__file__).resolve().parent / "site_style.css"

TAGLINE = ("Open-source multi-tenant crypto data aggregator — Vezgo's "
           "tenancy, DeBank's DeFi depth, Plaid's wire format.")

#: Examples needing an optional extra, and the import that proves it is there.
EXTRA_FOR = {"04_persist_to_your_database.py": ("sqlmodel", "[sql]"),
             "05_serve_the_http_api.py": ("fastapi", "[api]")}


@dataclass
class Page:
    """One output page: where it goes, what it is called, what is in it."""

    path: str                      # site-relative, e.g. "books/04_ledger.html"
    title: str
    body: str
    outline: list = field(default_factory=list)
    section: str = ""
    summary: str = ""


def _run_example(path: Path) -> tuple[str | None, str | None]:
    """Execute one example; return (captured output, note).

    A failure here fails the BUILD. Publishing an example whose output we
    could not produce is the exact class of defect the examples exist to
    prevent (`tests/style/test_examples_are_published.py`).
    """
    module_extra = EXTRA_FOR.get(path.name)
    if module_extra is not None:
        module, extra = module_extra
        probe = subprocess.run([sys.executable, "-c", f"import {module}"],
                               capture_output=True)
        if probe.returncode != 0:
            return None, (f"Needs the {extra} extra, which was not installed when "
                          "this page was built, so its output is not shown here. "
                          f"Install it with: pip install 'auradefi{extra}'")
    finished = subprocess.run([sys.executable, str(path)], capture_output=True,
                              text=True, cwd=REPO, timeout=300)
    if finished.returncode != 0:
        raise SystemExit(
            f"example failed, refusing to publish it: {path.name}\n"
            f"{finished.stdout[-2000:]}\n{finished.stderr[-2000:]}"
        )
    return finished.stdout, None


def _first_sentence(path: Path) -> str:
    """An example's docstring title line, for the index and the nav."""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip().strip('"').strip()
        if stripped:
            return stripped
    return path.name


def collect(run_examples: bool) -> list[Page]:
    pages: list[Page] = []

    body, outline = render_markdown(REPO / "README.md", "README.md", 0)
    pages.append(Page("index.html", "auradefi", body, outline, "Start here",
                      "What it is, what works today, and what is not there."))

    body, outline = render_markdown(REPO / "examples" / "README.md",
                                    "examples/README.md", 1)
    pages.append(Page("examples/index.html", "Examples", body, outline, "Examples",
                      "Ten task-shaped recipes that run offline."))

    example_files = [REPO / "examples" / "quickstart.py"] + sorted(
        path for path in (REPO / "examples").glob("[0-9][0-9]_*.py")
    )
    for path in example_files:
        output, note = _run_example(path) if run_examples else (None, None)
        body, outline = render_example(path, output, note)
        pages.append(Page(f"examples/{path.stem}.html", path.stem, body, outline,
                          "Examples", _first_sentence(path)))

    books = sorted((REPO / "docs" / "books").glob("*.ipynb"))
    book_rows = "\n".join(
        f'<li><a href="{path.stem}.html">{path.stem.replace("_", " ")}</a></li>'
        for path in books
    )
    pages.append(Page(
        "books/index.html", "PyBooks",
        "<h1>PyBooks</h1><p>Twelve executable notebooks, one per SPEC phase. "
        "Each runs offline against committed fixtures, asserts its own "
        "outputs, and is executed headlessly in CI, so it cannot drift from "
        f"the code.</p><ul class=\"cards\">{book_rows}</ul>",
        section="PyBooks", summary="Twelve executable notebooks, run in CI."))
    for path in books:
        body, outline = render_notebook(path, 1)
        pages.append(Page(f"books/{path.stem}.html", path.stem.replace("_", " "),
                          body, outline, "PyBooks", ""))

    # The output path is stated, never derived from the title: `site_render`'s
    # PAGE_FOR maps links onto these exact names, and a slug that drifts from
    # the map is a 404 nobody notices until a reader hits it.
    for source, title, summary in (
        ("docs/SPEC.md", "SPEC", "The design contract."),
        ("docs/DECISIONS.md", "Decisions", "Every pinned algorithm and id formula."),
        ("STATUS.md", "Status", "Phase gates, live test count, known caveats."),
        ("CHANGELOG.md", "Changelog", "What changed per release."),
        ("docs/RELEASING.md", "Releasing", "The pip + Docker release procedure."),
        ("docs/RELEASE_0.1.1.md", "Release 0.1.1",
         "Every defect found in 0.1.0, and its fix."),
        ("docs/AGENT_PROMPTS.md", "Agent loop",
         "The agent loop that builds this repo."),
    ):
        body, outline = render_markdown(REPO / source, source, 0)
        pages.append(Page(PAGE_FOR[source], title, body, outline, "Reference", summary))

    return pages


def nav_html(pages: list[Page], current: str) -> str:
    """The sidebar. Sections are open when the current page is inside one."""
    sections: dict[str, list[Page]] = {}
    for page in pages:
        if page.section:
            sections.setdefault(page.section, []).append(page)
    depth = current.count("/")
    up = "../" * depth
    chunks = ['<nav class="nav">']
    for section, entries in sections.items():
        inside = any(entry.path == current for entry in entries)
        chunks.append(f'<details {"open" if inside else ""}>'
                      f"<summary>{section}</summary><ul>")
        for entry in entries:
            active = ' class="active"' if entry.path == current else ""
            chunks.append(f'<li><a href="{up}{entry.path}"{active}>{entry.title}</a></li>')
        chunks.append("</ul></details>")
    chunks.append("</nav>")
    return "".join(chunks)


def toc_html(page: Page) -> str:
    entries = [(level, anchor, text) for level, anchor, text in page.outline
               if level == 2]
    if len(entries) < 3:
        return ""
    items = "".join(f'<li><a href="#{anchor}">{text}</a></li>'
                    for _, anchor, text in entries)
    return f'<aside class="toc"><p>On this page</p><ul>{items}</ul></aside>'


def write(page: Page, pages: list[Page]) -> None:
    depth = page.path.count("/")
    up = "../" * depth
    destination = OUT / page.path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page.title} · auradefi</title>
<meta name="description" content="{page.summary or TAGLINE}">
<link rel="stylesheet" href="{up}style.css">
<script>
  // Apply the reader's saved choice BEFORE first paint; absent one, the CSS
  // follows prefers-color-scheme on its own.
  try {{
    var saved = localStorage.getItem("auradefi-theme");
    if (saved) document.documentElement.dataset.theme = saved;
  }} catch (error) {{ /* private mode: fall through to the OS preference */ }}
</script>
</head>
<body>
<a class="skip" href="#content">Skip to content</a>
<header class="top">
  <a class="brand" href="{up}index.html">auradefi</a>
  <span class="version">0.1.1</span>
  <div class="spacer"></div>
  <a href="https://pypi.org/project/auradefi/">PyPI</a>
  <a href="{REPO_URL}">GitHub</a>
  <button class="theme" type="button" aria-label="Switch between light and dark">
    <span aria-hidden="true">◐</span>
  </button>
</header>
<div class="shell">
{nav_html(pages, page.path)}
<main id="content">
{page.body}
<footer>
<p>Apache-2.0 · <a href="{REPO_URL}">auracarehq/auradefi</a> ·
built from the repository, examples executed at build time.</p>
</footer>
</main>
{toc_html(page)}
</div>
<script>
  document.querySelector(".theme").addEventListener("click", function () {{
    var root = document.documentElement;
    var dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try {{ localStorage.setItem("auradefi-theme", root.dataset.theme); }} catch (error) {{}}
  }});
</script>
</body>
</html>
""", encoding="utf-8")


def main() -> int:
    run_examples = "--no-run" not in sys.argv
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    pages = collect(run_examples)
    for page in pages:
        write(page, pages)
    (OUT / "style.css").write_text(
        STYLE.read_text(encoding="utf-8") + "\n" + pygments_css(), encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    total = sum(path.stat().st_size for path in OUT.rglob("*") if path.is_file())
    print(f"built {len(pages)} pages into {OUT.relative_to(REPO)} "
          f"({total / 1024:.0f} KiB, no external requests)")
    if not run_examples:
        print("  NOTE: --no-run, so no example output is published on this build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
