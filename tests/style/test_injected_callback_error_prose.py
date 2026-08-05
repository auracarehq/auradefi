"""An INJECTED callback's docstring may not decide who sees its exception.

MOTIVATING FINDING (0.1.1 wave 2, `src/auradefi/embed/facade.py:287`
`_decode_page`, style, major): the default decoder's docstring ends "a
malformed row's ``auradefi.errors.SourceError`` propagates." It was true in
0.1.0. RELEASE_0.1.1 §5 #24 then made `Auradefi._run_sync` catch every
`AuradefiError` from one connection and file it as that connection's
`ConnectionSyncReport.failure` row, so the SourceError a malformed row raises
no longer reaches the host: `sync()` returns a report with
`failed_connections == (conn,)`. The code changed, the pinning test changed
(`test_a_malformed_row_surfaces_as_a_source_error` became
`test_a_malformed_row_is_reported_against_its_connection_not_raised`), and the
docstring did not: nothing in the acceptance list required it to.

WHY THIS CLASS IS DANGEROUS, and why it is CALLBACKS specifically. A function
that raises to its own caller documents something it controls. A function
handed to somebody else as a callable does not: `self._decode_page` is passed
into `SyncEngine.__init__` and invoked from inside `_run_sync`'s `try`, so
whether its exception is observable is decided entirely by the injector, in
another function, often in another module. That is exactly the distance over
which prose rots silently, no import breaks, no test turns red, and this repo
treats the docstring AS the error contract of a public seam (see
`Auradefi.__init__`, `_probe`, `UserHandle.connect_address`, all of which
promise callers what escapes). A host reading a stale promise writes
`except SourceError:` around a call that will never raise, and its real failure
path, the `failed` row, goes unhandled.

THE RULE, mechanically: if a function defined in a module is referenced BARE
(passed as a value, not called) as an argument in that same module, its
docstring may not assert that an exception ESCAPES ("propagates", "escapes",
"surfaces", "bubbles" near an `…Error` name) unless the same docstring also
says who CONTAINS it: "caught", "contained", "filed", "failed/failure",
"isolated", "reported", "does not escape". Naming the container is what keeps
the sentence honest when the container changes: it puts the two facts in one
place, so the next reader of `_run_sync` sees the prose that depends on it.

Deliberately NOT checked: propagation claims on ordinary functions
(`_probe`'s SourceError really does reach `connect_address`'s caller, that
claim is true and must stay), and cross-module reachability in general, which
needs a call graph no regex can stand in for.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO / "src" / "auradefi"

#: "propagates", "escapes", "surfaces", "bubbles": the verbs this repo uses
#: for "the caller sees this exception".
_ESCAPE_VERB = re.compile(r"\b(propagat\w*|escapes?|surfaces?|bubbles?)\b", re.I)

#: An exception class name, so a docstring about a *value* surfacing is left
#: alone.
_ERROR_NAME = re.compile(r"\b[A-Z][A-Za-z0-9]*Error\b")

#: The docstring said who contains the exception, so the claim is scoped and
#: the container is named where the next reader will see it.
_CONTAINMENT = re.compile(
    r"\b(caught|catches|contain(?:ed|s|ment)|filed|fail(?:ed|ure|s)"
    r"|isolat\w+|report(?:ed|s)|swallow\w*|never escapes?|does not escape"
    r"|not raised)\b",
    re.I,
)

#: A property is not a callback: `underlying.value` inside a call argument is
#: an attribute read, not an injection.
_NOT_A_CALLBACK = frozenset({"property", "cached_property"})


def _defined_functions(tree: ast.Module) -> dict[str, ast.AST]:
    """``{name: node}`` for every def in the module, properties excluded."""
    found: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = {
            getattr(d, "attr", getattr(d, "id", "")) for d in node.decorator_list
        }
        if decorators & _NOT_A_CALLBACK:
            continue
        found.setdefault(node.name, node)
    return found


def _bare_references(node: ast.AST, defined: dict[str, ast.AST]) -> set[str]:
    """Names of ``defined`` functions referenced as VALUES inside ``node``.

    A nested call's own callee is skipped: ``f(g(x))`` injects nothing, it
    calls ``g``. Only ``self.<name>`` and a bare ``<name>`` count: an
    attribute on anything else is somebody else's method.
    """
    names: set[str] = set()
    if isinstance(node, ast.Call):
        for child in list(node.args) + [k.value for k in node.keywords]:
            names |= _bare_references(child, defined)
        return names
    if isinstance(node, ast.Attribute):
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in defined
        ):
            names.add(node.attr)
        return names
    if isinstance(node, ast.Name):
        if node.id in defined:
            names.add(node.id)
        return names
    for child in ast.iter_child_nodes(node):
        names |= _bare_references(child, defined)
    return names


def _injected_callbacks(tree: ast.Module) -> dict[str, ast.AST]:
    """Functions this module hands to somebody else as a callable."""
    defined = _defined_functions(tree)
    injected: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for argument in list(node.args) + [k.value for k in node.keywords]:
            for name in _bare_references(argument, defined):
                injected[name] = defined[name]
    return injected


def _unscoped_escape_claim(docstring: str) -> str | None:
    """The offending sentence, or None when the prose is honest."""
    flat = " ".join(docstring.split())
    for sentence in re.split(r"(?<=[.;])\s+", flat):
        if not (_ESCAPE_VERB.search(sentence) and _ERROR_NAME.search(sentence)):
            continue
        # The whole docstring may name the container, not just this sentence.
        if _CONTAINMENT.search(flat):
            return None
        return sentence
    return None


def _offences() -> list[str]:
    """Every injected callback whose docstring promises an escape."""
    offences: list[str] = []
    callbacks = 0
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name, node in sorted(_injected_callbacks(tree).items()):
            callbacks += 1
            docstring = ast.get_docstring(node)  # type: ignore[arg-type]
            if not docstring:
                continue
            sentence = _unscoped_escape_claim(docstring)
            if sentence is not None:
                offences.append(
                    f"{path.relative_to(REPO)}:{node.lineno} {name}(): "  # type: ignore[attr-defined]
                    f"{sentence}"
                )
    # Eight today (`_decode_page`, `Clock.now_ms`, `_rank`, `holdings`,
    # `total_sats`, `handle`, `endpoint_id`, `event_id`). A collapse to a
    # handful means the AST shapes moved, not that injection stopped.
    assert callbacks >= 5, (
        f"only {callbacks} injected callbacks found across the source: the "
        "detector has gone blind; fix it rather than enjoying a green gate"
    )
    return offences


def test_an_injected_callback_never_promises_that_an_error_escapes() -> None:
    """See the module docstring's motivating finding (#24, `_decode_page`)."""
    assert not _offences(), (
        "an injected callback's docstring states who sees its exception, but "
        "the INJECTOR decides that: say which caller contains it (\"filed as "
        "ConnectionSyncReport.failure\", \"caught by …\") or drop the claim:\n"
        "  " + "\n  ".join(_offences())
    )


def test_the_gate_fires_on_the_motivating_docstring_and_clears_when_fixed() -> None:
    """The 0.1.1 #24 defect, and its fix, as strings, no source edited."""
    stale = (
        "The default decoder, bound lazily (imports live INSIDE).\n\n"
        "Phase 5 ingests the NATIVE stream only; a malformed row's "
        "``auradefi.errors.SourceError`` propagates."
    )
    fixed = (
        "The default decoder, bound lazily (imports live INSIDE).\n\n"
        "Phase 5 ingests the NATIVE stream only; a malformed row's "
        "``auradefi.errors.SourceError`` is filed by ``_run_sync`` as that "
        "connection's ``ConnectionSyncReport.failure`` (§5 #24), so it never "
        "reaches the host."
    )
    honest_neighbour = (
        "The connect-time liveness probe: EXACTLY one cheap request.\n\n"
        "An empty result is a VALID fresh address; "
        "``auradefi.errors.SourceError`` propagates untouched."
    )

    assert _unscoped_escape_claim(stale) is not None
    assert _unscoped_escape_claim(fixed) is None
    # _probe is NOT injected as a callback, so its true claim is never read by
    # this gate; the sentence alone must still be recognised as a claim.
    assert _unscoped_escape_claim(honest_neighbour) is not None


def test_the_detector_sees_a_callback_injected_through_self() -> None:
    """``SyncEngine(..., self._decode_page, ...)`` is the shape that matters."""
    module = ast.parse(
        "class C:\n"
        "    def __init__(self):\n"
        "        self._engine = E(self._decode_page, helper, other(1))\n"
        "    def _decode_page(self):\n"
        "        'doc'\n"
        "    def other(self, n):\n"
        "        'doc'\n"
        "def helper():\n"
        "    'doc'\n"
    )
    assert set(_injected_callbacks(module)) == {"_decode_page", "helper"}
