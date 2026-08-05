"""An executable doc artefact must pin values the CURRENT code produces.

MOTIVATING FINDING (0.1.1 wave 2, `docs/books/09_embedding.ipynb:326`, seam,
major): the published embedding book asserts `connection.id ==
"conn_b116094c537a85e6"`, the CHAINLESS 0.1.0 connection id. #26 made the embed
connection id chain-scoped, so the live value is `conn_d0327e21d9b0ea55`; the
book's stored outputs carry the retired id too, and its stored
`ConnectionSyncReport(...)` repr predates the `failed=` field #24 added. The
suite stayed green: `.venv/bin/pytest` never opens a notebook, and
`scripts/run_books.sh` (the only thing that executes the books) is wired into
the GitHub `notebooks` job, not into `commands.style`, `commands.test` or
`scripts/release_check.sh`. The loop's own gates therefore cannot see a false
published artefact.

WHY THE CLASS IS DANGEROUS. When a derived value moves, its consumers in
`src/` and `tests/` move with it — a red test is loud. Documentation is the one
consumer that fails silently, and it is the copy a HOST reads: a book asserting
a retired id teaches an integrator to expect ids our library no longer mints,
and a stored repr missing a field hides the very field a release added to make
partial failure visible. Worse, the retired value survives on purpose in the
tests (`CONN_ADDR_0_1_0`, `CONN_0_1_0`, kept to prove the break was deliberate),
so grepping for it finds "live" hits and the stale book looks corroborated.

THE RULES, mechanically — text-level only, so they stay fast and cannot rot
against a refactor:

1. RETIRED VALUES STAY OUT OF EXECUTABLE DOCS. Any id-shaped string literal
   that `tests/` or `src/` binds to a name marked as superseded (a trailing
   `_0_1_0`-style version, or `OLD`/`PREV`/`LEGACY`/`RETIRED`/`SUPERSEDED`)
   must not appear in `docs/books/*.ipynb` or `examples/*.py`. Those
   artefacts demonstrate live behaviour; history belongs in prose files
   (CHANGELOG, DECISIONS, the release note), which this gate leaves alone.
2. A STORED REPR CARRIES EVERY CURRENT FIELD. A `ClassName(field=...)` repr in
   a book's stored output, where `ClassName` is a `@dataclass` in
   `src/auradefi/`, must mention every one of that class's current repr fields.
   A field added since the book was last executed is the exact shape of #24's
   `failed=`: the output still parses, still reads plausibly, and is wrong.

Both rules are satisfied by re-executing the book (`BOOKS_INPLACE=1 bash
scripts/run_books.sh`) after the code change — which is the fix, not a
workaround.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO / "src" / "auradefi"
TEST_ROOT = REPO / "tests"
BOOKS = REPO / "docs" / "books"
EXAMPLES = REPO / "examples"

#: `conn_b116094c537a85e6`, `usr_1e63721d071ea2d9`, … — the DECISIONS-pinned
#: id shape: a short lowercase prefix and a 16-hex truncated sha256.
_ID_LITERAL = re.compile(r"\b[a-z]{2,12}_[0-9a-f]{16}\b")

#: A constant name that says "this value is history". A trailing dotted
#: version (`CONN_ADDR_0_1_0`) or an explicit word.
_RETIRED_NAME = re.compile(
    r"(?:_\d+_\d+_\d+$)|(?:(?:^|_)(?:OLD|PREV|LEGACY|RETIRED|SUPERSEDED|STALE)(?:_|$))"
)

#: `ConnectionSyncReport(connection_id=…` — a dataclass repr, not a call with
#: positional args (a keyword must follow the paren for us to compare fields).
_REPR_HEAD = re.compile(r"\b([A-Z][A-Za-z0-9]*)\((?=[a-z_]+=)")

#: `field=` at the top level of a repr's argument list.
_REPR_KEY = re.compile(r"(?:^|[\s,(\[])([a-z_][a-z_0-9]*)=")


def _executable_docs() -> list[Path]:
    """The doc artefacts that are RUN, so they must describe live behaviour.

    Both roots must be non-empty. A glob over a directory that has been
    moved or renamed matches nothing and passes vacuously, which is the
    same silence this gate exists to break.
    """
    books = sorted(BOOKS.glob("*.ipynb"))
    examples = sorted(EXAMPLES.glob("*.py"))
    assert books, f"no notebooks under {BOOKS.relative_to(REPO)} — gate is blind"
    assert examples, f"no examples under {EXAMPLES.relative_to(REPO)} — gate is blind"
    return books + examples


def _retired_literals() -> dict[str, str]:
    """``{id literal: "file:line NAME"}`` for every superseded constant."""
    retired: dict[str, str] = {}
    for path in sorted(TEST_ROOT.rglob("*.py")) + sorted(
        SOURCE_ROOT.rglob("*.py")
    ):
        try:
            module = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken file is another gate
            continue
        for node in ast.walk(module):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            value = node.value
            if not isinstance(value, ast.Constant) or not isinstance(
                value.value, str
            ):
                continue
            if not _ID_LITERAL.fullmatch(value.value):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and _RETIRED_NAME.search(
                    target.id
                ):
                    rel = path.relative_to(REPO)
                    retired[value.value] = f"{rel}:{node.lineno} {target.id}"
    return retired


def test_executable_docs_never_pin_a_retired_derived_value() -> None:
    """Rule 1 — see the module docstring's motivating finding."""
    retired = _retired_literals()
    assert retired, (
        "no superseded id constant found in the suite — either the naming "
        "convention changed (update _RETIRED_NAME) or this gate is now blind"
    )

    offences: list[str] = []
    for path in _executable_docs():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for literal in _ID_LITERAL.findall(line):
                if literal in retired:
                    offences.append(
                        f"{path.relative_to(REPO)}:{lineno} pins {literal}, "
                        f"retired at {retired[literal]}"
                    )

    assert not offences, (
        "an executed doc artefact pins a value the code no longer produces; "
        "re-run it (BOOKS_INPLACE=1 bash scripts/run_books.sh) instead of "
        "editing the literal by hand:\n  " + "\n  ".join(offences)
    )


def _dataclass_repr_fields() -> dict[str, list[str]]:
    """``{class name: repr field names}`` for every dataclass in the source."""
    def _is_dataclass(node: ast.ClassDef) -> bool:
        for decorator in node.decorator_list:
            call = decorator.func if isinstance(decorator, ast.Call) else decorator
            name = (
                call.attr
                if isinstance(call, ast.Attribute)
                else getattr(call, "id", "")
            )
            if name == "dataclass":
                return True
        return False

    def _repr_fields(node: ast.ClassDef) -> list[str]:
        names: list[str] = []
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign):
                continue
            if not isinstance(statement.target, ast.Name):
                continue
            annotation = ast.unparse(statement.annotation)
            if annotation.startswith(("ClassVar", "InitVar")):
                continue
            # `field(repr=False)` is deliberately absent from the repr.
            if statement.value is not None and "repr=False" in ast.unparse(
                statement.value
            ):
                continue
            names.append(statement.target.id)
        return names

    fields: dict[str, list[str]] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(module):
            if isinstance(node, ast.ClassDef) and _is_dataclass(node):
                fields.setdefault(node.name, _repr_fields(node))
    return fields


def _stored_output_lines(notebook: Path) -> list[str]:
    """Every line of every stored output of every code cell."""
    document = json.loads(notebook.read_text(encoding="utf-8"))
    lines: list[str] = []
    for cell in document.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        for output in cell.get("outputs", []):
            if output.get("output_type") == "stream":
                payload = "".join(output.get("text", []))
            elif output.get("output_type") == "execute_result":
                payload = "".join(output.get("data", {}).get("text/plain", []))
            else:
                continue
            lines.extend(payload.splitlines())
    return lines


def _balanced(line: str, open_paren: int) -> str | None:
    """The argument text of the repr opening at ``open_paren``, or None.

    None when the parenthesis does not close on this line — a truncated or
    wrapped print is not evidence of a missing field.
    """
    depth = 0
    for index in range(open_paren, len(line)):
        if line[index] == "(":
            depth += 1
        elif line[index] == ")":
            depth -= 1
            if depth == 0:
                return line[open_paren + 1 : index]
    return None


def test_stored_book_outputs_show_every_current_dataclass_field() -> None:
    """Rule 2 — see the module docstring's motivating finding."""
    fields = _dataclass_repr_fields()
    offences: list[str] = []
    for notebook in sorted(BOOKS.glob("*.ipynb")):
        for line in _stored_output_lines(notebook):
            for match in _REPR_HEAD.finditer(line):
                name = match.group(1)
                if name not in fields or not fields[name]:
                    continue
                inner = _balanced(line, match.end() - 1)
                if inner is None or "…" in inner or "..." in inner:
                    continue
                shown = set(_REPR_KEY.findall(inner))
                # Only ABSENT fields are evidence: a nested repr's keys leak
                # into the outer scan, so unexpected keys prove nothing.
                absent = [key for key in fields[name] if key not in shown]
                if absent:
                    offences.append(
                        f"{notebook.relative_to(REPO)}: stored {name}(...) "
                        f"output omits {absent} — re-execute the book"
                    )

    assert not offences, (
        "a book's stored output predates a dataclass field, so the published "
        "example hides it:\n  " + "\n  ".join(sorted(set(offences)))
    )
