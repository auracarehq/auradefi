"""House style for prose, mechanically. The repo read as machine-written.

MOTIVATING FINDING. Measured against Wikipedia's "Signs of AI writing", this
repository hit almost every structural marker on that list at once:

* 2,356 em dashes across tracked files, one every five lines in the README,
  used as the default connective in place of a comma, colon or full stop.
* The capability table bolded the single word "works" in sixteen consecutive
  rows, which is emphasis applied by machine rather than for a reader.
* Most paragraphs in the guide pages opened with a bold run and a full stop
  ("**Every numeric column is BIGINT.** Python int maps to ...").
* Negative parallelism as a closing flourish, over and over: "not a bug",
  "not an omission", "rather than promising in prose".

None of that is a bug, which is exactly why nothing caught it. It is worse
than a bug for a library nobody has heard of: a reader who decides the prose
was generated stops trusting the technical claims inside it, and this project
asks for trust on unusual claims (exact decimal arithmetic, a green suite that
hid a Postgres defect). The writing is part of the product surface.

THE RULES, and why each is mechanical rather than a matter of taste:

1. NO EM DASH, anywhere in tracked text. This is the one marker with no
   legitimate competing use here: every occurrence was standing in for
   punctuation that says more. En dashes are untouched and expected, because
   "#18-#36" and "Phases 0-4" are numeric ranges a careful writer does reach
   for. The rule is zero rather than a budget, because a budget is how 2,356
   accumulated.
2. NO BOLD LEAD-IN PARAGRAPHS on a published page. A bold run that opens a
   line and closes on a full stop or colon is a heading wearing a paragraph's
   clothes, and the article names it directly ("inline-header vertical
   lists"). Bolded links are exempt: `**[Docs](url)**` is a link, not a
   lead-in.
3. NO BOLDED TOKEN REPEATED more than three times in one published page. This
   is the "works" x16 shape: once emphasis lands on every instance of a term
   it has stopped being emphasis.
4. NEGATIVE PARALLELISM IS CAPPED at three markers per hundred lines. Any one
   "rather than" is ordinary English, so this one is deliberately a density
   budget and not a ban. The published set currently runs at 0 to 1.3 per
   hundred lines, so the cap has real headroom and still refuses a page that
   returns to closing every paragraph on "X, not Y".

DELIBERATELY NOT CHECKED: vocabulary. Word lists ("delve", "tapestry",
"testament", "vibrant", "robust", "seamless") date fast, and this repo never
used them. Sentence rhythm and the rule of three are left to review too, since
three items is very often just three items.

This module writes the em dash as ``\\u2014`` so that the gate does not report
itself, and so a reader cannot accidentally reintroduce one by copying from it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: Written as an escape on purpose: a literal here would make this gate report
#: its own source file, and would give the next reader one to copy.
EM_DASH = "\u2014"

#: Directories whose text is part of the project. `.venv`, `dist` and caches
#: hold other people's writing and are not ours to restyle.
SCANNED_DIRECTORIES = (
    "src",
    "tests",
    "scripts",
    "examples",
    "docs",
    ".claude",
    ".github",
)

SCANNED_ROOT_FILES = (
    "README.md",
    "CHANGELOG.md",
    "Dockerfile",
    "docker-compose.yml",
    # Published verbatim inside llms-full.txt, so its prose is read as often
    # as any page's.
    ".env.example",
)

#: Text formats. Binary and lock files are skipped.
TEXT_SUFFIXES = frozenset(
    {".md", ".py", ".ipynb", ".sql", ".css", ".sh", ".yml", ".yaml", ".js", ""}
)

#: The pages a reader of the project actually meets, in reading order.
PUBLISHED_PAGES = (
    "README.md",
    "docs/quickstart.md",
    "docs/authentication.md",
    "docs/limits.md",
    "docs/bring-your-own.md",
    "docs/schema.md",
    "docs/glossary.md",
    "examples/README.md",
    "CHANGELOG.md",
)

#: A bold run opening a line and closing on a full stop or colon.
_BOLD_LEAD_IN = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+)?\*\*([^*]{2,80}[.:])\*\*")

#: `**[text](url)**` is a bolded link, which is not a lead-in.
_BOLDED_LINK = re.compile(r"^\s*\*\*\[")

_BOLD_RUN = re.compile(r"\*\*([^*\n]{2,40})\*\*")

_NEGATIVE_PARALLELISM = re.compile(
    r"\brather than\b|\bnot just\b|\bnot only\b|\bnot merely\b"
    r"|\bis not (?:a|an|the) [a-z]+, (?:but|it)\b",
    re.IGNORECASE,
)

MAX_BOLD_REPEATS = 3
MAX_NEGATIVE_PARALLELISM_PER_100_LINES = 3.0


def _scanned_files() -> list[Path]:
    """Every tracked text file whose prose this project owns."""
    found: list[Path] = []
    for name in SCANNED_ROOT_FILES:
        path = REPO / name
        if path.is_file():
            found.append(path)
    for directory in SCANNED_DIRECTORIES:
        root = REPO / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix in TEXT_SUFFIXES:
                found.append(path)
    return sorted(found)


def bold_lead_ins(text: str) -> list[str]:
    """Lines opening with a bold run that closes on a full stop or colon."""
    offenders = []
    for line in text.split("\n"):
        if _BOLDED_LINK.match(line):
            continue
        match = _BOLD_LEAD_IN.match(line)
        if match:
            offenders.append(match.group(1))
    return offenders


def overused_bold(text: str) -> dict[str, int]:
    """Bolded tokens emphasised more than :data:`MAX_BOLD_REPEATS` times."""
    counts: dict[str, int] = {}
    for run in _BOLD_RUN.findall(text):
        key = run.strip().lower()
        counts[key] = counts.get(key, 0) + 1
    return {run: n for run, n in counts.items() if n > MAX_BOLD_REPEATS}


def negative_parallelism_rate(text: str) -> float:
    """Markers per hundred lines."""
    lines = max(1, len(text.split("\n")))
    return len(_NEGATIVE_PARALLELISM.findall(text)) * 100.0 / lines


#: The least each directory may contribute before the scan is presumed blind.
#: Stated per directory rather than as one repo-wide total, because the Docker
#: test image copies a SUBSET of the tree (src, tests, docs, examples, scripts,
#: the two root markdown files) and carries neither `.claude` nor `.github`. A
#: single total made this guard fail in the container at 294 files while
#: passing on the host, which is a gate reporting its own environment rather
#: than the repository's prose.
#: Floors sit near 80% of today's counts (src 118, tests 126, docs 25,
#: examples 12, scripts 11), so ordinary deletion does not trip them but a
#: glob that stops descending does.
MINIMUM_FILES_PER_DIRECTORY = {
    "src": 94,
    "tests": 100,
    "docs": 20,
    "examples": 9,
    "scripts": 8,
}


def test_the_scan_actually_reaches_the_repository() -> None:
    """A glob that found nothing would make every rule below vacuous."""
    files = _scanned_files()
    for directory, minimum in MINIMUM_FILES_PER_DIRECTORY.items():
        root = REPO / directory
        found = [path for path in files if path.is_relative_to(root)]
        assert len(found) >= minimum, (
            f"only {len(found)} text files scanned under {directory}/ "
            f"(expected at least {minimum}): the glob has gone blind"
        )
    names = {path.name for path in files}
    assert {"README.md", "facade.py", "quickstart.py"} <= names


def test_no_em_dash_anywhere() -> None:
    offenders = []
    for path in _scanned_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        count = text.count(EM_DASH)
        if count:
            offenders.append(f"{path.relative_to(REPO)}: {count}")
    assert not offenders, (
        "em dashes found. Reach for the punctuation that says more: a colon "
        "when the second half explains the first, a full stop when it is its "
        "own sentence, a comma when it is an aside. An en dash in a numeric "
        "range is fine and is not what this checks:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("page", PUBLISHED_PAGES)
def test_a_published_page_has_no_bold_lead_in_paragraphs(page: str) -> None:
    text = (REPO / page).read_text(encoding="utf-8")
    offenders = bold_lead_ins(text)
    assert not offenders, (
        f"{page} opens paragraphs with a bold run, which reads as a heading "
        "pretending to be prose. Either promote it to a real heading or write "
        f"the sentence: {offenders}"
    )


@pytest.mark.parametrize("page", PUBLISHED_PAGES)
def test_a_published_page_does_not_bold_the_same_token_repeatedly(
    page: str,
) -> None:
    text = (REPO / page).read_text(encoding="utf-8")
    offenders = overused_bold(text)
    assert not offenders, (
        f"{page} bolds the same token more than {MAX_BOLD_REPEATS} times, so "
        "the emphasis has stopped meaning anything. The capability table "
        f"bolded 'works' in sixteen rows before this gate existed: {offenders}"
    )


@pytest.mark.parametrize("page", PUBLISHED_PAGES)
def test_a_published_page_does_not_lean_on_negative_parallelism(
    page: str,
) -> None:
    text = (REPO / page).read_text(encoding="utf-8")
    rate = negative_parallelism_rate(text)
    assert rate <= MAX_NEGATIVE_PARALLELISM_PER_100_LINES, (
        f"{page} runs {rate:.1f} 'X rather than Y' / 'not just X' markers per "
        f"100 lines, over the {MAX_NEGATIVE_PARALLELISM_PER_100_LINES} cap. "
        "One is ordinary English; a page of them is a tic. State the positive "
        "fact and stop."
    )


# --- the gate, proved against the prose it was written for -----------------
# The repository's own text before the rewrite, as scratch strings. No file is
# edited to test a gate.

_OLD_README_TABLE = (
    "| Cassette replay harness | 0 | **works** |\n"
    "| Style gates | 0 | **works** |\n"
    "| EVM balances | 1 | **works** |\n"
    "| Tenancy | 2 | **works** |\n"
)

_OLD_SCHEMA_PARAGRAPH = (
    "**Every numeric column is `BIGINT`, and it has to be.** Python `int` "
    "maps to SQLAlchemy `Integer`.\n"
    "**`metadata` is the global `SQLModel.metadata`.** If your application "
    "also uses SQLModel, its tables are in the same registry.\n"
)

_OLD_DENSE_PARALLELISM = (
    "a numeric raw is rejected on read rather than coerced\n"
    "That is a real limitation, not an omission from this page.\n"
    "stored as hashes rather than plaintext\n"
    "asserted rather than promised in prose\n"
)

_BOLDED_LINK_LINE = (
    "**[Documentation site](https://example.invalid/)** carries the examples.\n"
)


def test_the_gate_fires_on_the_capability_table_that_bolded_works() -> None:
    assert overused_bold(_OLD_README_TABLE) == {"works": 4}
    assert overused_bold(_OLD_README_TABLE.replace("**works**", "works")) == {}


def test_the_gate_fires_on_bold_lead_in_paragraphs() -> None:
    offenders = bold_lead_ins(_OLD_SCHEMA_PARAGRAPH)
    assert len(offenders) == 2
    assert offenders[0].startswith("Every numeric column")


def test_a_bolded_link_is_not_a_lead_in() -> None:
    """The exemption is real, so the rule does not punish a linked heading."""
    assert bold_lead_ins(_BOLDED_LINK_LINE) == []


def test_the_gate_fires_on_negative_parallelism_density() -> None:
    assert negative_parallelism_rate(_OLD_DENSE_PARALLELISM) > (
        MAX_NEGATIVE_PARALLELISM_PER_100_LINES
    )
    # One marker in a page-length file is ordinary English, not a tic.
    ordinary = "One clause rather than another.\n" + "filler line\n" * 99
    assert negative_parallelism_rate(ordinary) <= (
        MAX_NEGATIVE_PARALLELISM_PER_100_LINES
    )


def test_the_em_dash_rule_is_looking_for_the_right_character() -> None:
    """A regression here would silently pass a repository full of them."""
    assert EM_DASH == "\N{EM DASH}"
    assert "–" != EM_DASH, "an en dash must not trip the em dash rule"
    assert f"text with {EM_DASH} a dash".count(EM_DASH) == 1
