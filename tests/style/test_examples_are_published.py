"""An example nobody is told about, or told about and absent, is a defect.

`examples/` is the surface a reader meets first: before the SPEC, before
the books, usually before the README's capability table. Three ways it rots
silently, all of which this gate makes loud:

1. AN EXAMPLE IS ADDED AND NOT INDEXED. `examples/README.md` is the index
   the site and GitHub both render; a file missing from it is invisible.
2. AN EXAMPLE IS RENAMED OR DELETED AND STILL INDEXED. A dead link in the
   index is worse than no index: the reader assumes the capability is
   there and cannot find how to use it.
3. AN EXAMPLE STOPS BEING RUNNABLE DOCUMENTATION. Every example must open
   with a docstring (the question it answers, which the site renders as its
   summary) and must be executed by `scripts/run_examples.sh`, which CI
   runs, so a broken example fails the build instead of misleading someone.

This is the same rule the notebooks already live under: an unexecuted
document is undelivered work. Rule #10 applies to the docs, not only to the
capability table.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"
INDEX = EXAMPLES / "README.md"
RUNNER = REPO / "scripts" / "run_examples.sh"


def _example_files() -> list[Path]:
    return sorted(path for path in EXAMPLES.glob("*.py") if path.name != "__init__.py")


def test_there_are_examples_at_all() -> None:
    files = _example_files()
    assert len(files) >= 5, f"only {len(files)} example(s): the folder is the front door"
    assert INDEX.exists(), f"{INDEX.relative_to(REPO)} is the index and must exist"


def test_every_example_is_listed_in_the_index() -> None:
    index = INDEX.read_text(encoding="utf-8")
    missing = [path.name for path in _example_files() if path.name not in index]
    assert not missing, (
        "examples absent from examples/README.md (a reader never finds them):\n  "
        + "\n  ".join(missing)
    )


def test_the_index_links_nothing_that_is_gone() -> None:
    index = INDEX.read_text(encoding="utf-8")
    present = {path.name for path in _example_files()}
    # `[`04_x.py`](04_x.py)` and `python examples/04_x.py`: every .py file
    # the index mentions, by basename, however it spells the path.
    mentioned = {
        word.strip("`()[]<>,.").rsplit("/", 1)[-1]
        for word in index.replace("(", " ").replace(")", " ").split()
        if word.strip("`()[]<>,.").endswith(".py")
    }
    dangling = sorted(name for name in mentioned if name not in present)
    assert not dangling, (
        "examples/README.md points at files that do not exist:\n  " + "\n  ".join(dangling)
    )


def test_every_example_opens_with_a_docstring() -> None:
    """The docstring IS the example's published summary. See build_site.py."""
    undocumented = []
    for path in _example_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not ast.get_docstring(tree):
            undocumented.append(path.name)
    assert not undocumented, (
        "examples without a module docstring (nothing to publish as a summary):\n  "
        + "\n  ".join(undocumented)
    )


def test_the_runner_would_execute_every_example() -> None:
    """`run_examples.sh` globs, so its glob must actually cover the tree."""
    runner = RUNNER.read_text(encoding="utf-8")
    assert "examples/quickstart.py" in runner
    assert "examples/[0-9][0-9]_*.py" in runner
    uncovered = [
        path.name
        for path in _example_files()
        if path.name != "quickstart.py" and not path.name[:2].isdigit()
    ]
    assert not uncovered, (
        "examples the runner's glob cannot see: name them NN_something.py:\n  "
        + "\n  ".join(uncovered)
    )
