"""Machine-facing copies of this site, and a prompt to paste into a model.

Three artefacts, built from the same tree as the HTML:

* ``llms.txt``: the llmstxt.org index. A title, a summary, the five-line
  program, the rules a model cannot infer, then every published page as a
  link with one line of description. Small enough to read in full before
  deciding what to fetch.
* ``llms-full.txt``: the whole corpus as plain text. Every prose page, every
  example's source, the environment file and a compact signature listing of
  the public surface. One fetch, no crawling.
* ``prompt.txt``: what a person pastes into Claude, ChatGPT or an editor
  agent to start a session that writes working auradefi code. The
  ``Build with an LLM`` page shows the same text, because both are rendered
  from :data:`GROUND_RULES` here.

WHY THIS IS GENERATED. A model writing against a library it has never seen
fails in one specific way: it produces plausible code against an API that
does not exist. Every rule below is a place this package's real shape differs
from the obvious guess, so the list is the useful part, and a hand-copied
list on a web page would be the first thing to go stale. The link sections
come from `build_site.collect()`, so a page that is added, renamed or dropped
moves here in the same build.

`tests/style/test_llm_context_is_true.py` checks the claims: every symbol
named must import, every environment variable must be one `Settings` reads,
and every port method list must match the real Protocol.
"""

from __future__ import annotations

import inspect
import textwrap
from html import escape
from pathlib import Path

# The reference page and this file describe one surface, so they share the
# introspection helpers instead of keeping two versions that can disagree.
from site_reference import SECTIONS, _clean, _field_rows, _load, _signature_of

REPO = Path(__file__).resolve().parents[1]

#: The library in one paragraph, for a reader with no context at all.
SUMMARY = (
    "Open-source multi-tenant crypto data aggregator for Python. Reads EVM, "
    "Bitcoin and Solana balances and history, prices them exactly, keeps "
    "tenants isolated, and emits Plaid's wire format. Library first: import "
    "it and pay no network cost. The HTTP API is a thin shell over the same "
    "core. Version 0.1.2, Apache-2.0, alpha."
)

#: The whole program, and the only code most readers need to see first.
FIRST_PROGRAM = """from auradefi import Auradefi

aura = Auradefi.sandbox()            # no key, no network, no configuration
for holding in aura.holdings()[0].holdings:
    print(holding.symbol, holding.quantity, holding.value)"""

#: Where this package's real shape differs from the obvious guess. Each one
#: is a defect a model would otherwise write, and each is checked by the gate.
GROUND_RULES: tuple[str, ...] = (
    "Start in Sandbox. `Auradefi.sandbox()` needs no key, no network and no "
    "configuration: it replays a recording bundled inside the wheel, through "
    "the production source, decoder, ledger and pricing code. Going live is "
    "one line, `Auradefi.from_env()`.",

    "Sandbox answers are constants: one address on `eip155:1`, 2 ETH and 25 "
    "USDC worth 5025 USD, seven transactions. Asking it for a different "
    "address, chain or page raises `CassetteMissError`, which means the "
    "recording does not hold that request. No credential is missing.",

    "Amounts are exact. `Quantity` and `Money` wrap `Decimal` and go on the "
    "wire as tagged strings, never as JSON numbers. A float anywhere in this "
    "arithmetic is a bug.",

    "An unpriced asset is never zero. It comes back in `report.holdings` with "
    "`price=None` and is named in `report.unpriced`, so a total is either "
    "right or visibly incomplete.",

    "There are no Bitcoin or Solana prices. The one shipped oracle is "
    "DefiLlama: current prices, six EVM chains. Anything else needs your own "
    "`prices` port.",

    "Chains are CAIP-2 strings. `user.connect_address(\"eip155:1\", \"0x…\")` "
    "works; `\"ethereum\"` is refused, as is any chain the registry has not "
    "been given.",

    "Configuration is prefixed. `Settings.from_env()` reads "
    "`AURADEFI_ETHERSCAN_API_KEY` and its siblings. A bare "
    "`ETHERSCAN_API_KEY` is ignored deliberately, so an unrelated variable "
    "cannot become this library's credential.",

    "Nothing runs on its own. There is no scheduler, no worker and no "
    "background thread. You call `aura.sync(budget=n)` on your own tick, "
    "where `budget` caps the source pages that one call may spend; cursors "
    "make the next call resume.",

    "The default ledger is memory and loses everything at exit. Production "
    "means `ledger=SqlModelLedger(session_factory=…)` or your own object. It "
    "takes a session factory, not a URL, because your application owns the "
    "engine and the migrations.",

    "A source is one object satisfying two seams, `balances()` and "
    "`fetch_txlist()`. Binding one that has only the first raises at "
    "construction time.",

    "Ports are structural protocols. There is no base class and no "
    "registration: an object with the right methods is the port. Five of "
    "them, all optional keyword arguments to `Auradefi.sandbox()` and "
    "`Auradefi.from_env()`.",

    "Every failure inherits `auradefi.errors.AuradefiError`, so one `except` "
    "clause catches this library and nothing else.",

    "The gaps are real and documented: no multicall, no on-chain reader for "
    "the position adapters, no historical prices, no async surface, no "
    "Solana transaction decode. If the reference does not name a symbol, it "
    "does not exist. Say so instead of inventing one.",
)

#: Prose published as-is, in reading order. `.env.example` is here because
#: every configurable name and its default live in it, commented.
PROSE_FILES: tuple[str, ...] = (
    "docs/quickstart.md",
    "README.md",
    "docs/authentication.md",
    "docs/limits.md",
    "docs/bring-your-own.md",
    "docs/schema.md",
    "docs/glossary.md",
    "examples/README.md",
    ".env.example",
)

_RULE = "=" * 72


def _example_files() -> list[Path]:
    return [REPO / "examples" / "quickstart.py"] + sorted(
        (REPO / "examples").glob("[0-9][0-9]_*.py")
    )


def _first_line(text: str) -> str:
    return _clean(text).split("\n")[0]


def _fill(text: str, initial: str = "", indent: str = "") -> str:
    """One paragraph at 76 columns, with URLs left whole."""
    return textwrap.fill(" ".join(text.split()), width=76,
                         initial_indent=initial, subsequent_indent=indent,
                         break_long_words=False, break_on_hyphens=False)


def prompt_txt(base_url: str, full_bytes: int) -> str:
    """The paste-in prompt, as plain text.

    Every paragraph is filled to 76 columns rather than written pre-wrapped,
    because the site name and the corpus size are interpolated: hand-wrapping
    around them leaves a short line wherever a value lands. It has to read
    well in a chat box, a `CLAUDE.md` and a terminal.
    """
    program = "\n".join(
        ("    " + line) if line else "" for line in FIRST_PROGRAM.split("\n")
    )
    paragraphs = [
        _fill("You are writing Python against auradefi 0.1.2, an "
              "open-source, multi-tenant crypto data aggregator. Its "
              f"documentation is at {base_url} and its source is at "
              "https://github.com/auracarehq/auradefi."),
        _fill(f"Before writing code, read {base_url}/llms.txt, which indexes "
              f"every page. If you can fetch and hold about "
              f"{full_bytes // 1024} KB, read {base_url}/llms-full.txt "
              "instead: it is the whole documentation, including every "
              "worked example's source. If you can fetch neither, work from "
              "the rules below and tell me which parts of your answer you "
              "could not check."),
        "    pip install auradefi     "
        "   # extras: [sql] SQLModel ledger, [api] FastAPI",
        "This runs with no credentials, and is where an answer should start:",
        program,
        _fill(f"These {len(GROUND_RULES)} rules are where auradefi differs "
              "from the obvious guess. Most wrong answers about this library "
              "break one of them."),
        *(_fill(_plain(rule), f"{number:2}. ", "    ")
          for number, rule in enumerate(GROUND_RULES, 1)),
        _fill("Write code that runs. Prefer the shipped defaults, and when I "
              "ask for something the package does not do, name the port I "
              "bind to get it. If you are unsure whether a symbol exists, "
              f"fetch its page under {base_url}/reference/ instead of "
              "guessing at a signature."),
        "My task:",
    ]
    return "\n\n".join(paragraphs) + "\n"


def index_txt(pages: list, base_url: str) -> str:
    """`llms.txt`: the llmstxt.org index over every published page."""
    rules = "\n".join(f"- {_plain(rule)}" for rule in GROUND_RULES)
    chunks = [
        "# auradefi\n",
        f"\n> {SUMMARY}\n",
        "\nInstall with `pip install auradefi`. This is the whole first "
        "program, and it needs no credentials:\n",
        f"\n```python\n{FIRST_PROGRAM}\n```\n",
        "\nWhat a reader coming from another library gets wrong:\n\n",
        rules,
        "\n",
    ]

    sections: dict[str, list] = {}
    for page in pages:
        if page.section:
            sections.setdefault(page.section, []).append(page)
    # llmstxt.org gives `## Optional` one meaning: safe to skip when context
    # is short. The notebooks are the long tail, so that is where they go.
    for section, entries in sections.items():
        heading = "Optional" if section == "Notebooks" else section
        chunks.append(f"\n## {heading}\n\n")
        for entry in entries:
            # A guide's summary IS its title, since the question a page
            # answers is the best name for it. Printing both reads as noise.
            note = (f": {entry.summary}"
                    if entry.summary and entry.summary != entry.title else "")
            chunks.append(f"- [{entry.title}]({base_url}/{entry.path}){note}\n")

    chunks.append(
        "\n## Machine-readable\n\n"
        f"- [llms-full.txt]({base_url}/llms-full.txt): every page above as "
        "one plain-text file, with each example's full source.\n"
        f"- [prompt.txt]({base_url}/prompt.txt): a prompt to paste into a "
        "model before asking it for auradefi code.\n"
        f"- [openapi.json]({base_url}/openapi.json): the HTTP surface, "
        "generated from the app.\n"
        "- Every prose page above is also served as markdown at the same "
        "path with a `.md` suffix, so "
        f"[{base_url}/limits.md]({base_url}/limits.md) is that page without "
        "the nav around it.\n"
        f"- [Source](https://github.com/auracarehq/auradefi): Apache-2.0. "
        "Every example on this site is executed at build time.\n"
    )
    return "".join(chunks)


def _plain(rule: str) -> str:
    """A rule with its markdown ticks kept: llms.txt is markdown."""
    return " ".join(rule.split())


def reference_txt() -> str:
    """The public surface as signatures and docstrings, no HTML."""
    lines = [f"{_RULE}\n# API REFERENCE (generated from the code)\n{_RULE}"]
    for title, blurb, targets in SECTIONS:
        lines.append(f"\n\n## {title}\n{blurb}")
        for target in targets:
            obj, qualname = _load(target)
            lines.append(f"\n### {qualname}   ({target.partition(':')[0]})\n")
            lines.append(_clean(getattr(obj, "__doc__", "")))
            if not inspect.isclass(obj):
                lines.append(f"\n    {qualname}{_signature_of(obj)}")
                continue
            rows = _field_rows(obj)
            if rows:
                lines.append("\nFields: " + ", ".join(
                    f"{name}: {kind}" for name, kind, _ in rows))
            for name, member in vars(obj).items():
                if name.startswith("_") and name != "__init__":
                    continue
                unwrapped = member.__func__ if isinstance(member, classmethod) else member
                if isinstance(member, property) or not callable(unwrapped):
                    continue
                summary = _first_line(getattr(unwrapped, "__doc__", "") or "")
                lines.append(f"\n    {name}{_signature_of(unwrapped)}")
                if summary:
                    lines.append(f"        {summary}")
    return "\n".join(lines)


def full_txt(base_url: str) -> str:
    """`llms-full.txt`: the documentation as one file a model can hold."""
    chunks = [
        f"# auradefi 0.1.2: the complete documentation\n\n{SUMMARY}\n\n"
        f"Source: https://github.com/auracarehq/auradefi\n"
        f"Rendered: {base_url}\n"
        "Licence: Apache-2.0\n\n"
        "This file is generated from the repository by scripts/build_site.py "
        "and contains every prose page, every worked example in full, and a "
        "signature listing of the public surface. The twelve notebooks and "
        "the changelog are not here; both are linked from llms.txt.\n\n"
        "Read this first:\n\n" + "\n".join(
            f"  {number}. {_plain(rule)}"
            for number, rule in enumerate(GROUND_RULES, 1))
        + "\n",
    ]
    for name in PROSE_FILES:
        text = (REPO / name).read_text(encoding="utf-8")
        if not name.endswith(".md"):
            text = f"```\n{text.rstrip()}\n```"
        chunks.append(f"\n\n{_RULE}\n# FILE: {name}\n{_RULE}\n\n{text.rstrip()}")
    for path in _example_files():
        source = path.read_text(encoding="utf-8").rstrip()
        chunks.append(f"\n\n{_RULE}\n# FILE: examples/{path.name}\n{_RULE}\n\n"
                      f"```python\n{source}\n```")
    chunks.append("\n\n" + reference_txt() + "\n")
    return "".join(chunks)


def llms_html(base_url: str, full_bytes: int) -> str:
    """The `Build with an LLM` page: the prompt, and the files behind it."""
    prompt = escape(prompt_txt(base_url, full_bytes))
    return f"""<h1>Build with an LLM</h1>
<p>A model that has never seen this package writes plausible code against an
API it invented. The prompt below is the shortest thing that stops that: the
first program, and the {len(GROUND_RULES)} places auradefi differs from the
obvious guess. Paste it in, add your task, and go.</p>

<h2>The prompt</h2>
<p class="meta">The same text is at
<a href="prompt.txt">prompt.txt</a>, so an agent can fetch it.</p>
<pre class="prompt" id="prompt">{prompt}</pre>
<button class="copy" type="button" data-for="prompt">Copy the prompt</button>
<script>
  document.querySelector(".copy[data-for]").addEventListener("click", function (event) {{
    var button = event.currentTarget;
    var text = document.getElementById(button.dataset.for).textContent;
    navigator.clipboard.writeText(text).then(function () {{
      button.textContent = "Copied";
    }}, function () {{
      button.textContent = "Select the block and copy it";
    }});
  }});
</script>

<h2>Files for machines</h2>
<p>Every one is generated from this repository in the same build as the pages
you are reading, so none of them can describe a surface the package no longer
has.</p>
<table><thead><tr><th>File</th><th>What it is</th><th>When to use it</th></tr>
</thead><tbody>
<tr><td><a href="llms.txt"><code>llms.txt</code></a></td>
<td>The <a href="https://llmstxt.org/">llmstxt.org</a> index: summary, first
program, the rules, then every page as a link.</td>
<td>Give an agent the map and let it fetch what it needs.</td></tr>
<tr><td><a href="llms-full.txt"><code>llms-full.txt</code></a></td>
<td>Roughly {full_bytes // 1024} KB: every prose page, every example's full
source, and the signature listing.</td>
<td>One fetch, no crawling, when the context window can hold it.</td></tr>
<tr><td><a href="prompt.txt"><code>prompt.txt</code></a></td>
<td>The block above, as plain text.</td>
<td>A system prompt, a <code>CLAUDE.md</code>, an editor rules file.</td></tr>
<tr><td><a href="openapi.json"><code>openapi.json</code></a></td>
<td>The HTTP surface, generated from the running app.</td>
<td>Client generation, or a tool definition.</td></tr>
<tr><td><code>&lt;page&gt;.md</code></td>
<td>Every prose page also exists as its own markdown, at the same path with
a <code>.md</code> suffix: <a href="limits.md"><code>limits.md</code></a>,
<a href="glossary.md"><code>glossary.md</code></a>,
<a href="quickstart.md"><code>quickstart.md</code></a>.</td>
<td>One page, without the nav and the stylesheet around it.</td></tr>
</tbody></table>

<h2>In a coding agent</h2>
<p>Drop the corpus into the repository you are working in, and the agent
reads it like any other file:</p>
<pre class="code">curl -o docs/auradefi.txt {base_url}/llms-full.txt</pre>
<p>For Claude Code, adding the prompt to <code>CLAUDE.md</code> applies it to
every session in that project. For an editor with a rules file, the same text
goes there.</p>

<h2>What the model still cannot know</h2>
<p>Sandbox answers are a recording, so a model can assert them and be right,
and a live address will not match them. The gaps in
<a href="index.html">the README</a> are current as of 0.1.2: no
multicall, one price oracle over six EVM chains, no on-chain reader for the
position adapters, and no scheduler. A model asked to work around one of
those will happily write the missing component and present it as ours, so
check any answer that solves a gap on that list.</p>
"""
