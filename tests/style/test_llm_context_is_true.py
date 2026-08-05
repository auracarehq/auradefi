"""What we hand a model must be true, and must stay true.

MOTIVATING CLASS. `scripts/site_llms.py` publishes three files written for
machines: `llms.txt`, `llms-full.txt` and `prompt.txt`. Their link sections
and their reference listing are generated, so those cannot drift. The
valuable part cannot be: thirteen hand-written rules naming environment
variables, port methods, class names and a five-line program, chosen
because each is a place this package differs from the obvious guess.

A wrong rule there is worse than a missing one. A human reading a stale
sentence on a web page shrugs and reads the code. A model reads the sentence
as ground truth, writes it into working-looking code, and the person who
pasted the prompt trusts the result precisely because it came with a
citation. So every mechanically checkable claim in that text is checked
here:

1. THE FIRST PROGRAM RUNS. It is executed, offline, in this suite. The
   `tests/conftest.py` socket guard makes "no network" a real assertion
   rather than a promise in prose.
2. EVERY ENVIRONMENT VARIABLE NAMED IS READ. A rule that names
   `AURADEFI_SOMETHING` which `Settings.from_env` ignores teaches a reader
   to export a variable that does nothing.
3. EVERY SYMBOL NAMED EXISTS. Dotted paths must import; bare capitalised
   names must be in the published reference, in `auradefi.errors`, or in a
   short list of standard-library types the rules legitimately mention.
4. THE PORTS ARE THE PORTS. The five keyword arguments the rules promise
   must really be bindable, and the two source seams must carry the method
   names the rules quote.
5. NOTHING PUBLISHED IS MISSING FROM THE INDEX. `llms.txt` is built from
   `build_site.collect()`, so a new page appears in it automatically; this
   pins that the wiring stays in place instead of quietly emitting a
   shorter file.
"""

from __future__ import annotations

import importlib
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

from site_llms import (  # noqa: E402  (path set above)
    FIRST_PROGRAM,
    GROUND_RULES,
    PROSE_FILES,
    SUMMARY,
    full_txt,
    index_txt,
    prompt_txt,
)
from site_reference import SECTIONS  # noqa: E402

BASE = "https://auradefi.info"

#: Everything a model is told, as one blob. The prompt contains the rules,
#: the summary and the program, so this is the whole surface under test.
LLM_TEXT = "\n".join((SUMMARY, FIRST_PROGRAM, *GROUND_RULES))

#: `` `AURADEFI_ETHERSCAN_API_KEY` ``
_ENV_NAME = re.compile(r"\bAURADEFI_[A-Z0-9_]+\b")

#: A backticked dotted path into the package: `auradefi.errors.AuradefiError`.
_DOTTED = re.compile(r"`(auradefi(?:\.[a-z_]+)+(?:\.[A-Za-z_][A-Za-z0-9_]*)?)`")

#: Anything inside backticks. The rules write code spans, so this is where
#: every name a model might type appears.
_CODE_SPAN = re.compile(r"`([^`]+)`")

#: A capitalised identifier standing alone inside one. The lookarounds keep
#: `AURADEFI_ETHERSCAN_API_KEY` from arriving here as four class names.
_SYMBOL = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Za-z0-9]*)(?![A-Za-z0-9_])")

#: Names the rules may use that are not ours: Python's own vocabulary, the
#: one standard-library type the arithmetic rule has to name, and the ticker
#: symbols the Sandbox rule quotes. Kept short on purpose, since a long
#: allowlist is how a typo gets waved through.
FOREIGN_NAMES = frozenset(
    {"Decimal", "None", "True", "False", "JSON", "USD", "USDC", "ETH", "KB"}
)


def _reference_names() -> set[str]:
    """Every symbol the published reference documents, by bare name."""
    return {target.partition(":")[2] for _, _, group in SECTIONS for target in group}


def _error_names() -> set[str]:
    from auradefi import errors

    return {
        name for name, value in vars(errors).items()
        if inspect.isclass(value) and issubclass(value, errors.AuradefiError)
    }


def test_the_first_program_actually_runs(capsys: pytest.CaptureFixture) -> None:
    """Rule 1. The five lines a model is told to start from, executed.

    Offline, with no configuration, exactly as the text claims. The socket
    guard in `tests/conftest.py` fails this if anything reaches for a
    network, so "no network" is asserted here and not merely written.
    """
    exec(compile(FIRST_PROGRAM, "<first program>", "exec"), {})  # noqa: S102
    printed = capsys.readouterr().out.strip().split("\n")
    assert len(printed) == 2, f"the recording answers two holdings: {printed}"
    assert printed[0].startswith("ETH "), printed
    assert "USD" in printed[0], printed


def test_every_environment_variable_named_is_one_settings_reads() -> None:
    """Rule 2."""
    from auradefi.config import Settings

    named = set(_ENV_NAME.findall(LLM_TEXT + prompt_txt(BASE, 0)))
    assert named, "no AURADEFI_ variable named at all: this check has gone blind"

    real = {
        f"AURADEFI_{field.upper()}"
        for field in Settings.__dataclass_fields__
    }
    strays = sorted(named - real)
    assert not strays, (
        "the LLM context names environment variables Settings.from_env does "
        f"not read, so a reader would export them for nothing: {strays}"
    )


def test_every_dotted_path_named_imports() -> None:
    """Rule 3, first half."""
    paths = set(_DOTTED.findall(LLM_TEXT))
    assert paths, "no dotted auradefi path named: this check has gone blind"

    broken: list[str] = []
    for path in sorted(paths):
        module, _, attribute = path.rpartition(".")
        try:
            importlib.import_module(path)
            continue
        except ImportError:
            pass
        try:
            assert hasattr(importlib.import_module(module), attribute)
        except (ImportError, AssertionError):
            broken.append(path)
    assert not broken, (
        f"the LLM context names paths that do not resolve: {broken}"
    )


def test_every_symbol_named_is_one_we_publish() -> None:
    """Rule 3, second half."""
    named = {
        symbol
        for span in _CODE_SPAN.findall(LLM_TEXT)
        for symbol in _SYMBOL.findall(span)
    }
    assert len(named) >= 8, f"only {named} scanned: this check has gone blind"

    known = _reference_names() | _error_names() | FOREIGN_NAMES
    strays = sorted(named - known)
    assert not strays, (
        "the LLM context names symbols that are neither in the published "
        "reference nor in auradefi.errors. A model will import them and the "
        f"import will fail: {strays}"
    )


def test_the_ports_the_rules_promise_are_bindable() -> None:
    """Rule 4, first half: five ports, all keyword arguments."""
    from auradefi.embed.facade import Auradefi

    parameters = set(inspect.signature(Auradefi.__init__).parameters)
    promised = {"ledger", "source", "prices", "sync_state", "clock"}
    assert promised <= parameters, (
        "the rules promise five ports by name; the facade takes "
        f"{sorted(parameters)}"
    )
    assert "overrides" in inspect.signature(Auradefi.sandbox).parameters
    assert "overrides" in inspect.signature(Auradefi.from_env).parameters


def test_the_two_source_seams_carry_the_names_the_rules_quote() -> None:
    """Rule 4, second half."""
    from auradefi.embed.sync import PageFetcher
    from auradefi.portfolio.holdings import BalanceSource

    for protocol, expected in ((BalanceSource, "balances"),
                               (PageFetcher, "fetch_txlist")):
        methods = [name for name, value in vars(protocol).items()
                   if callable(value) and not name.startswith("_")]
        assert methods == [expected], f"{protocol.__name__} now has {methods}"
        assert f"`{expected}()`" in LLM_TEXT, (
            f"the rules stopped naming the {expected} seam"
        )


def test_the_index_links_every_published_page() -> None:
    """Rule 5."""
    from build_site import collect

    pages = collect(run_examples=False)
    index = index_txt(pages, BASE)
    missing = [page.path for page in pages
               if f"({BASE}/{page.path})" not in index]
    assert not missing, (
        f"llms.txt does not link {len(missing)} published page(s): {missing[:5]}"
    )
    for name in ("llms-full.txt", "prompt.txt", "openapi.json"):
        assert f"{BASE}/{name}" in index, f"llms.txt does not offer {name}"


def test_the_corpus_carries_the_prose_and_the_examples() -> None:
    """`llms-full.txt` is one fetch, so a gap in it is silent."""
    corpus = full_txt(BASE)
    for name in PROSE_FILES:
        assert f"# FILE: {name}" in corpus, f"{name} missing from llms-full.txt"
    examples = sorted((REPO / "examples").glob("*.py"))
    assert len(examples) >= 11, "the example glob has gone blind"
    for path in examples:
        assert f"# FILE: examples/{path.name}" in corpus, path.name
    assert "# API REFERENCE" in corpus
    assert len(corpus) > 100_000, f"corpus is only {len(corpus)} chars"


def test_every_file_the_corpus_publishes_is_in_the_repository() -> None:
    """A published file that git ignores works here and nowhere else.

    Found the hard way: `.env.example` is linked from the authentication
    page and quoted in full by `llms-full.txt`, and `.gitignore` swallowed
    it under `.env.*`. The GitHub link 404ed, and a build from a fresh
    clone would have died reading a file that only ever existed on one
    laptop.
    """
    if not (REPO / ".git").exists():        # the Docker image copies no history
        pytest.skip("no git checkout here")
    tracked = set(subprocess.run(
        ["git", "ls-files", "-z", *PROSE_FILES], cwd=REPO,
        capture_output=True, text=True, check=True).stdout.split("\0"))
    missing = [name for name in PROSE_FILES if name not in tracked]
    assert not missing, (
        "llms-full.txt publishes files that are not in the repository, so "
        f"the build only works where they happen to exist: {missing}"
    )


def test_the_prompt_is_the_prompt() -> None:
    """It has to survive being pasted into a chat box, so keep it plain."""
    prompt = prompt_txt(BASE, 170_000)
    assert FIRST_PROGRAM.split("\n")[0] in prompt
    assert prompt.rstrip().endswith("My task:")
    assert f"{BASE}/llms.txt" in prompt and f"{BASE}/llms-full.txt" in prompt
    assert str(len(GROUND_RULES)) in prompt, "the rule count is hard-coded"
    for rule in GROUND_RULES:
        assert rule.split(".")[0] in " ".join(prompt.split()), rule[:40]
    assert "<" not in prompt and "&" not in prompt, (
        "the prompt is plain text; markup here would be pasted verbatim"
    )


# --- the checks, proved against the mistakes they exist to catch -----------
# Scratch strings. No published text is edited to prove a gate.

def test_the_environment_check_fires_on_a_variable_nobody_reads() -> None:
    """`AURADEFI_DATABASE_URL` is the tempting one, and it does not exist.

    The SQL ledger takes a session factory, so there is no URL to set. A
    rule that invented this name would send every reader to a variable the
    package never reads.
    """
    from auradefi.config import Settings

    named = set(_ENV_NAME.findall("Point it at `AURADEFI_DATABASE_URL`."))
    real = {f"AURADEFI_{field.upper()}" for field in Settings.__dataclass_fields__}
    assert named - real == {"AURADEFI_DATABASE_URL"}
    assert "AURADEFI_ETHERSCAN_API_KEY" in real, "the real one must still pass"


def test_the_symbol_check_fires_on_an_invented_class() -> None:
    """The classic hallucination: a `Client` object this package has no such thing as."""
    span = "construct `AuradefiClient(api_key=...)` and call `Auradefi`"
    named = {symbol for text in _CODE_SPAN.findall(span)
             for symbol in _SYMBOL.findall(text)}
    known = _reference_names() | _error_names() | FOREIGN_NAMES
    assert named - known == {"AuradefiClient"}
    assert "Auradefi" in named, "the real class must still be recognised"
