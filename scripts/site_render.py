"""Rendering primitives for the docs site: markdown, notebooks, examples.

Kept apart from `build_site.py` so the page/nav assembly stays readable and
each half stays under the house line budget. No network, no template engine:
markdown-it-py for prose, Pygments for code, `json` for notebooks.
"""

from __future__ import annotations

import html
import json
import posixpath
import re
from pathlib import Path

from markdown_it import MarkdownIt
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

REPO_URL = "https://github.com/auracarehq/auradefi"
BLOB = f"{REPO_URL}/blob/main"

#: repo-relative source -> site page. Anything absent falls back to GitHub,
#: so a link can never silently 404 inside the site.
PAGE_FOR = {
    "README.md": "index.html",
    "CHANGELOG.md": "changelog.html",
    "examples": "examples/index.html",
    "examples/README.md": "examples/index.html",
    "docs/books": "books/index.html",
}

#: Design and build documents. They are NOT published: a spec, a build log
#: and a release post-mortem answer "what is this and how was it made",
#: which is not what a developer opening the docs is asking. They stay in
#: the repository and link out to GitHub, so a citation still resolves.
INTERNAL_DOCS = frozenset(
    {
        "docs/internal/SPEC.md",
        "docs/internal/DECISIONS.md",
        "docs/internal/STATUS.md",
        "docs/internal/RELEASING.md",
        "docs/internal/RELEASE_0.1.1.md",
        "docs/internal/AGENT_PROMPTS.md",
    }
)

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_HEADING = re.compile(r"<h([1-6])>(.*?)</h\1>", re.DOTALL)
_HREF = re.compile(r'href="([^"]+)"')


def _pygments(code: str, language: str = "", _attrs: str = "") -> str:
    """Highlight `code`. markdown-it calls this as (code, lang, attrs)."""
    try:
        lexer = get_lexer_by_name(language or "text")
    except ClassNotFound:
        try:
            lexer = guess_lexer(code)
        except ClassNotFound:
            return f"<pre class=\"code\">{html.escape(code)}</pre>"
    formatter = HtmlFormatter(nowrap=True)
    return f'<pre class="code">{highlight(code, lexer, formatter)}</pre>'


def markdown() -> MarkdownIt:
    """CommonMark + tables + strikethrough, with highlighted fences."""
    return (
        MarkdownIt("commonmark", {"html": True, "highlight": _pygments})
        .enable("table")
        .enable("strikethrough")
    )


def slug(text: str) -> str:
    plain = re.sub(r"<[^>]+>", "", text)
    plain = html.unescape(plain).lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-") or "section"


def anchored(body: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Give every heading an id; return the body and its heading outline."""
    outline: list[tuple[int, str, str]] = []
    seen: dict[str, int] = {}

    def replace(match: re.Match[str]) -> str:
        level, inner = int(match.group(1)), match.group(2)
        base = slug(inner)
        seen[base] = seen.get(base, 0) + 1
        anchor = base if seen[base] == 1 else f"{base}-{seen[base]}"
        outline.append((level, anchor, re.sub(r"<[^>]+>", "", inner).strip()))
        return f'<h{level} id="{anchor}">{inner}</h{level}>'

    return _HEADING.sub(replace, body), outline


def rewrite_links(body: str, source: str, depth: int) -> str:
    """Point repo-relative links at their published page, or at GitHub.

    ``source`` is the repo-relative file the markdown came from, so a
    ``../docs/internal/SPEC.md`` written inside ``examples/README.md``
    resolves the same way GitHub resolves it. ``depth`` is how many
    directories deep the OUTPUT page sits, so every emitted link stays
    relative. The site is served from a subpath (`/auradefi/`) and
    root-relative links break.

    A link with no published page becomes a GitHub blob URL. That is
    correct for source files and for the design documents in
    :data:`INTERNAL_DOCS`, and it is a SILENT failure for anything else.
    A page that should be on the site quietly becomes an outbound link.
    :func:`unpublished_targets` exists so a gate can catch that; nothing
    here can tell the two cases apart on its own.
    """
    up = "../" * depth
    base = posixpath.dirname(source)

    def replace(match: re.Match[str]) -> str:
        href = match.group(1)
        if "://" in href or href.startswith(("#", "mailto:")):
            return match.group(0)
        target, _, fragment = href.partition("#")
        if not target:
            return match.group(0)
        # A `.html`/`.json` href names a BUILT artefact, not a repo file, and
        # is already written relative to the page it appears on. Rewriting it
        # as a repo path would turn a valid site link into a GitHub 404.
        # `tests/style/test_site_publishes_what_it_links.py` resolves these
        # against the real page list, which is the only thing that can.
        if target.endswith((".html", ".json")):
            return match.group(0)
        resolved = posixpath.normpath(posixpath.join(base, target)).lstrip("./")
        page = page_for(resolved)
        suffix = f"#{fragment}" if fragment else ""
        if page is None:
            return f'href="{BLOB}/{resolved}{suffix}"'
        return f'href="{up}{page}{suffix}"'

    return _HREF.sub(replace, body)


def page_for(resolved: str) -> str | None:
    """The published page for a repo-relative path, or ``None``.

    ``None`` means "not published here" and the caller falls back to
    GitHub. Books and examples are derived rather than listed, because
    both directories grow.
    """
    page = PAGE_FOR.get(resolved) or PAGE_FOR.get(resolved.rstrip("/"))
    if page is not None:
        return page
    if resolved.startswith("docs/books/") and resolved.endswith(".ipynb"):
        return "books/" + Path(resolved).stem + ".html"
    if resolved.startswith("examples/") and resolved.endswith(".py"):
        return "examples/" + Path(resolved).stem + ".html"
    return None


def unpublished_targets(body: str, source: str) -> list[str]:
    """Repo-relative markdown links that resolve to no published page.

    Every entry became an outbound GitHub link. A design document
    (:data:`INTERNAL_DOCS`) or a source file is meant to; a page that was
    supposed to be published is a regression no rendered-link check can
    see, because the href it produces is perfectly valid.
    """
    base = posixpath.dirname(source)
    missing: list[str] = []
    for href in _HREF.findall(body):
        if "://" in href or href.startswith(("#", "mailto:")):
            continue
        target = href.partition("#")[0]
        if not target or target.endswith((".html", ".json")):
            continue
        resolved = posixpath.normpath(posixpath.join(base, target)).lstrip("./")
        if page_for(resolved) is None and resolved not in INTERNAL_DOCS:
            missing.append(resolved)
    return missing


def render_markdown(path: Path, source: str, depth: int) -> tuple[str, list]:
    """One markdown file -> (body html with anchors and fixed links, outline)."""
    body, outline = anchored(markdown().render(path.read_text(encoding="utf-8")))
    return rewrite_links(body, source, depth), outline


def _output_text(output: dict) -> tuple[str, bool]:
    """One notebook output -> (text or html, is_html)."""
    if output.get("output_type") == "stream":
        return "".join(output.get("text", [])), False
    if output.get("output_type") == "error":
        traceback = _ANSI.sub("", "\n".join(output.get("traceback", [])))
        return traceback, False
    data = output.get("data", {})
    if "text/html" in data:
        return "".join(data["text/html"]), True
    return "".join(data.get("text/plain", [])), False


def render_notebook(path: Path, depth: int) -> tuple[str, list]:
    """A .ipynb -> HTML: markdown prose, highlighted code, stored outputs."""
    notebook = json.loads(path.read_text(encoding="utf-8"))
    md = markdown()
    chunks: list[str] = []
    for cell in notebook.get("cells", []):
        source = "".join(cell.get("source", []))
        if cell.get("cell_type") == "markdown":
            chunks.append(md.render(source))
            continue
        if not source.strip():
            continue
        chunks.append('<div class="cell">')
        chunks.append(_pygments(source, "python"))
        rendered = [_output_text(output) for output in cell.get("outputs", [])]
        text = "".join(value for value, is_html in rendered if not is_html)
        if text.strip():
            chunks.append(f'<pre class="out">{html.escape(text.rstrip())}</pre>')
        for value, is_html in rendered:
            if is_html:
                chunks.append(f'<div class="out">{value}</div>')
        chunks.append("</div>")
    body, outline = anchored("\n".join(chunks))
    return rewrite_links(body, str(path), depth), outline


def _split_run_command(rest: str) -> tuple[str, str]:
    """The leading indented command block, and the prose after it.

    Returns ``("", rest)`` when a docstring does not open with one, so a guide
    written without a command still renders.
    """
    lines = rest.split("\n")
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    command: list[str] = []
    while index < len(lines) and lines[index].startswith("    "):
        command.append(lines[index].strip())
        index += 1
    if not command:
        return "", rest
    return "\n".join(command), "\n".join(lines[index:]).strip("\n")


def render_example(path: Path, output: str | None, note: str | None) -> tuple[str, list]:
    """An example .py -> its docstring as prose, its source, and its output."""
    text = path.read_text(encoding="utf-8")
    match = re.match(r'\s*"""(.*?)"""', text, re.DOTALL)
    docstring = match.group(1).strip() if match else ""
    md = markdown()
    title, _, rest = docstring.partition("\n")
    # Strip NEWLINES only. These docstrings are written flush-left, so an
    # indented line is a deliberate code block and `.strip()` would flatten
    # the first one into a paragraph.
    rest = rest.strip("\n")
    # Each guide's docstring opens with its own install-and-run command as an
    # indented block. Lift it out under a heading, so the page answers "how do
    # I run this?" above the fold instead of leaving the command to be noticed.
    run_block, rest = _split_run_command(rest)
    chunks = [f"<h1>{html.escape(title.strip())}</h1>"]
    if run_block:
        chunks.append("<h2>Run it</h2>")
        chunks.append(f'<pre class="run">{html.escape(run_block)}</pre>')
    chunks.append(md.render(rest))
    if note:
        chunks.append(f'<p class="note">{html.escape(note)}</p>')
    if output is not None:
        chunks.append("<h2>What it prints</h2>")
        chunks.append(f'<pre class="out">{html.escape(output.rstrip())}</pre>')
    chunks.append("<h2>The whole file</h2>")
    chunks.append(
        f'<p class="meta"><a href="{BLOB}/examples/{path.name}">'
        f"{path.name} on GitHub</a>: self-contained, offline, "
        "asserts its own output.</p>"
    )
    chunks.append(_pygments(text, "python"))
    body, outline = anchored("\n".join(chunks))
    return rewrite_links(body, f"examples/{path.name}", 1), outline


def pygments_css() -> str:
    """Light and dark token colours, scoped so both themes can ship."""
    light = HtmlFormatter(style="default").get_style_defs(".code")
    dark = HtmlFormatter(style="monokai").get_style_defs(".code")
    dark_scoped = "\n".join(
        line.replace(".code", ':root[data-theme="dark"] .code', 1) if line.strip().startswith(".code") else line
        for line in dark.splitlines()
    )
    media = "\n".join(
        line.replace(".code", ":root:not([data-theme='light']) .code", 1)
        if line.strip().startswith(".code")
        else line
        for line in dark.splitlines()
    )
    return f"{light}\n@media (prefers-color-scheme: dark) {{\n{media}\n}}\n{dark_scoped}\n"
