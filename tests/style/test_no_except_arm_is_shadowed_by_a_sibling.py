"""No ``except`` arm may be unreachable behind another arm of the same ``try``.

MOTIVATING FINDING (0.2.0 phase 11, `tests/sources/evm/test_rpc.py:714`,
major, test-quality). `EvmRpc._post` had been widened from
`except httpx.HTTPError` to `except (httpx.HTTPError, httpx.InvalidURL,
ValueError)` to stop a scheme-less url leaking a bare `ValueError` past a
documented `SourceError` contract. Reverting the whole widening left all 90
tests in the mirror file green: the fix lived only in the source, so any
later edit could narrow the tuple back and the suite would say nothing.

The class the sweep of that finding drew out is "an ``except`` arm that no
test pins". Most of it needs judgement, and a regex that guessed at it would
fire on correct code. One corner of it does not, and it is the corner where
the arm is not merely unpinned but UNPINNABLE: an arm that is a subclass of
another arm in the same handler, or of an earlier handler on the same `try`,
can never be entered. `except (ValueError, UnicodeDecodeError, ...)` catches
a `UnicodeDecodeError` on the `ValueError`, because `UnicodeDecodeError` IS a
`ValueError`; the second name is decoration. No test can distinguish the
tuple with it from the tuple without it, so nothing can hold it in place, and
nothing can tell a reader whether it was ever load-bearing.

WHY THAT IS WORTH A GATE, and not just tidiness. A dead arm is a false
statement about the taxonomy in the one place a reader looks for the truth.
Three instances stood in this tree when the gate was written
(`tenancy/tokens.py` twice, `api/deps.py` once), and each sat under a
docstring reasoning carefully about which roots the handler had to cover:
`tokens.py` argued that "the obvious ``(ValueError, UnicodeDecodeError)``
pair" misses `RecursionError`, which is true and important, while implying
the pair is two doors when it is one. The next author who narrows such a
tuple, or who copies it to a new door, is reasoning from a list that has
already lied to them once. The live arms of those same tuples
(`RecursionError`, which is a `RuntimeError` and genuinely uncaught by
`ValueError`) are exactly what must survive; this gate is what keeps the
padding from hiding them.

DELIBERATELY NOT CHECKED. Whether a live arm has a test. That is the wider
class and it needs a human: `except (KeyError, TypeError, ValidationError)`
around a dict access is three real doors, and no static reading can say
which of them a cassette row happens to open. Report those; do not gate them.
An arm whose name this file cannot resolve to a class (a conditional import,
a name built at runtime) is skipped rather than guessed at.
"""

from __future__ import annotations

import ast
import builtins
import importlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Every tree whose handlers are ours to keep honest. `tests/` is included:
#: a dead arm in a test's own `except` misleads exactly as much, and the
#: style directory's handler strings are string literals, invisible to `ast`.
_ROOTS = ("src/auradefi", "scripts", "tests")


def _dotted(node: ast.AST) -> str | None:
    """``httpx.InvalidURL`` from the AST of that expression; None if it is
    not a plain (possibly dotted) name."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _namespace(tree: ast.Module) -> dict[str, object]:
    """What each importable name in this module resolves to.

    Built from the module's own import statements rather than by importing
    the module, so a source file needing an optional dependency (fastapi,
    sqlmodel) is still readable here.
    """
    space: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                try:
                    module = importlib.import_module(alias.name)
                except Exception:  # noqa: BLE001 - unimportable is "unknown"
                    continue
                space[alias.asname or alias.name.split(".")[0]] = (
                    module if alias.asname is None or "." not in alias.name
                    else importlib.import_module(alias.name)
                )
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            try:
                module = importlib.import_module(node.module)
            except Exception:  # noqa: BLE001
                continue
            for alias in node.names:
                if hasattr(module, alias.name):
                    space[alias.asname or alias.name] = getattr(module, alias.name)
    return space


def _resolve(name: str, space: dict[str, object]) -> type | None:
    """The exception class ``name`` denotes here, or None if unknowable."""
    head, *rest = name.split(".")
    current: object | None = space.get(head, getattr(builtins, head, None))
    for part in rest:
        current = getattr(current, part, None)
        if current is None:
            return None
    return current if isinstance(current, type) and issubclass(
        current, BaseException) else None


def _arms(handler: ast.ExceptHandler) -> list[tuple[str, int]]:
    """``(dotted name, line)`` for each type this one handler names."""
    kind = handler.type
    if kind is None:
        return []
    parts = kind.elts if isinstance(kind, ast.Tuple) else [kind]
    found = []
    for part in parts:
        name = _dotted(part)
        if name:
            found.append((name, part.lineno))
    return found


def _shadowed(tree: ast.Module, space: dict[str, object]) -> list[str]:
    """``"line: X is already caught by Y"`` for every unreachable arm."""
    dead: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        seen: list[tuple[str, type]] = []  # arms of THIS try, in order
        for handler in node.handlers:
            for name, line in _arms(handler):
                caught = _resolve(name, space)
                if caught is None:
                    continue  # unresolvable: say nothing rather than guess
                for earlier_name, earlier in seen:
                    if issubclass(caught, earlier):
                        dead.append(
                            f"{line}: {name} is already caught by "
                            f"{earlier_name}"
                        )
                        break
                seen.append((name, caught))
    return dead


def _tree_offences() -> tuple[list[str], int]:
    """Every dead arm under the checked roots, and how many arms were read."""
    reported: list[str] = []
    arms = 0
    for root in _ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            space = _namespace(tree)
            arms += sum(len(_arms(h)) for n in ast.walk(tree)
                        if isinstance(n, ast.Try) for h in n.handlers)
            reported += [f"{path.relative_to(REPO)}:{line}"
                         for line in _shadowed(tree, space)]
    return reported, arms


def test_no_handler_arm_is_shadowed_by_an_earlier_one() -> None:
    """See the module docstring: a dead arm is an unpinnable arm."""
    reported, arms = _tree_offences()
    # 95 arms stand in the tree today. A collapse to a handful means name
    # resolution broke, not that the handlers got tidier.
    assert arms >= 60, (
        f"only {arms} except arms read across {_ROOTS}: the detector has "
        "gone blind; fix it rather than enjoying a green gate"
    )
    assert not reported, (
        "an except arm is a subclass of another arm on the same try, so it "
        "can never be entered and no test can hold it in place. Drop it, or "
        "reorder so the specific arm comes first if it needs its own "
        "handler:\n  " + "\n  ".join(reported)
    )


def test_the_gate_fires_on_the_motivating_tuples() -> None:
    """`tenancy/tokens.py` and `api/deps.py`, as strings, no source read."""
    module = ast.parse(
        "import json\n"
        "def decode(segment):\n"
        "    try:\n"
        "        return json.loads(segment)\n"
        "    except (ValueError, UnicodeDecodeError, RecursionError) as exc:\n"
        "        raise AuthError('rejected') from exc\n"
    )
    assert _shadowed(module, _namespace(module)) == [
        "5: UnicodeDecodeError is already caught by ValueError"
    ]

    widened = ast.parse(
        "def decode(segment):\n"
        "    try:\n"
        "        return b64(segment)\n"
        "    except (ValueError, UnicodeEncodeError) as exc:\n"
        "        raise AuthError('rejected') from exc\n"
    )
    assert _shadowed(widened, {}) == [
        "4: UnicodeEncodeError is already caught by ValueError"
    ]


def test_the_live_arms_of_those_same_tuples_are_left_alone() -> None:
    """`RecursionError` is a `RuntimeError`: the arm the gate must protect."""
    module = ast.parse(
        "def decode(segment):\n"
        "    try:\n"
        "        return loads(segment)\n"
        "    except (ValueError, RecursionError):\n"
        "        return None\n"
    )
    assert _shadowed(module, {}) == []


def test_a_later_handler_shadowed_by_an_earlier_one_is_dead_too() -> None:
    """Same defect, spelled across two handlers instead of one tuple."""
    module = ast.parse(
        "def read(path):\n"
        "    try:\n"
        "        return open(path)\n"
        "    except OSError:\n"
        "        return None\n"
        "    except FileNotFoundError:\n"
        "        return None\n"
    )
    assert _shadowed(module, {}) == [
        "6: FileNotFoundError is already caught by OSError"
    ]
    reordered = ast.parse(
        "def read(path):\n"
        "    try:\n"
        "        return open(path)\n"
        "    except FileNotFoundError:\n"
        "        return None\n"
        "    except OSError:\n"
        "        return None\n"
    )
    assert _shadowed(reordered, {}) == []


def test_an_unresolvable_name_is_skipped_rather_than_guessed() -> None:
    """A name this file cannot turn into a class produces no verdict."""
    module = ast.parse(
        "def call():\n"
        "    try:\n"
        "        return go()\n"
        "    except (vendor.Timeout, vendor.ReadTimeout):\n"
        "        return None\n"
    )
    assert _shadowed(module, {}) == []


def test_dotted_and_imported_spellings_resolve_to_the_same_class() -> None:
    """`decimal.InvalidOperation` and a bare import must read alike."""
    dotted = ast.parse(
        "import decimal\n"
        "def total():\n"
        "    try:\n"
        "        return sum(())\n"
        "    except (decimal.InvalidOperation, decimal.ConversionSyntax):\n"
        "        return None\n"
    )
    assert _shadowed(dotted, _namespace(dotted)) == [
        "5: decimal.ConversionSyntax is already caught by decimal.InvalidOperation"
    ]
    imported = ast.parse(
        "from decimal import InvalidOperation, Overflow\n"
        "def total():\n"
        "    try:\n"
        "        return sum(())\n"
        "    except (InvalidOperation, Overflow):\n"
        "        return None\n"
    )
    # `decimal.Overflow` is NOT an `InvalidOperation`: sources/solana/spl.py
    # names both for a reason, and this gate must not touch it.
    assert _shadowed(imported, _namespace(imported)) == []
