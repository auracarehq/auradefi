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
    "STATUS.md": "status.html",
    "CHANGELOG.md": "changelog.html",
    "docs/SPEC.md": "spec.html",
    "docs/DECISIONS.md": "decisions.html",
    "docs/RELEASING.md": "releasing.html",
    "docs/RELEASE_0.1.1.md": "release-0-1-1.html",
    "docs/AGENT_PROMPTS.md": "agent-prompts.html",
    "examples": "examples/index.html",
    "examples/README.md": "examples/index.html",
    "docs/books": "books/index.html",
}

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
    ``../docs/SPEC.md`` written inside ``examples/README.md`` resolves the
    same way GitHub resolves it. ``depth`` is how many directories deep the
    OUTPUT page sits, so every emitted link stays relative — the site is
    served from a subpath (`/auradefi/`) and root-relative links break.
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
        resolved = posixpath.normpath(posixpath.join(base, target)).lstrip("./")
        page = PAGE_FOR.get(resolved) or PAGE_FOR.get(resolved.rstrip("/"))
        if page is None:
            if resolved.startswith("docs/books/") and resolved.endswith(".ipynb"):
                page = "books/" + Path(resolved).stem + ".html"
            elif resolved.startswith("examples/") and resolved.endswith(".py"):
                page = "examples/" + Path(resolved).stem + ".html"
        if page is None:
            suffix = f"#{fragment}" if fragment else ""
            return f'href="{BLOB}/{resolved}{suffix}"'
        suffix = f"#{fragment}" if fragment else ""
        return f'href="{up}{page}{suffix}"'

    return _HREF.sub(replace, body)


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
    chunks = [f"<h1>{html.escape(title.strip())}</h1>", md.render(rest.strip("\n"))]
    if note:
        chunks.append(f'<p class="note">{html.escape(note)}</p>')
    if output is not None:
        chunks.append("<h2>What it prints</h2>")
        chunks.append(f'<pre class="out">{html.escape(output.rstrip())}</pre>')
    chunks.append("<h2>The whole file</h2>")
    chunks.append(
        f'<p class="meta"><a href="{BLOB}/examples/{path.name}">'
        f"{path.name} on GitHub</a> — self-contained, offline, "
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
