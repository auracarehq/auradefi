"""A door that promises ``SourceError`` must catch every root a send raises.

MOTIVATING FINDING (0.2.0 phase 11, `src/auradefi/sources/evm/rpc.py:346`,
adversarial, major): `_post` wrapped `self._client.post(self._url, json=...)`
in `except httpx.HTTPError` alone, under a module docstring stating "Every
failure raises `auradefi.errors.SourceError` and nothing else". Constructing
`EvmRpc(client, "localhost:8545")`, the likeliest local-node configuration
there is, leaked `builtins.ValueError: unknown url type: '/8545'` straight
past the taxonomy: httpx hands a scheme-less URL to urllib's cookie code,
which raises `ValueError`, and `issubclass(ValueError, httpx.HTTPError)` is
False.

WHY THE CLASS IS WIDER THAN ONE MODULE, and why `httpx.HTTPError` reads as
sufficient when it is not. `HTTPError` names only the request/response tree
(`TransportError`, `ConnectError`, `TimeoutException`, `HTTPStatusError`).
Three other things come out of the same one-line send, and none of them is an
`HTTPError` in httpx 0.28:

  * `httpx.InvalidURL`, which descends straight from `Exception`. It fires on
    a control character ANYWHERE in the URL, so it is not only host
    configuration: `Esplora.address_stats("\\x7f")` and
    `DefiLlamaOracle.usd_prices(["eip155:1/erc20:0x\\x7f"])` both put a
    CALLER's string into the path.
  * `ValueError` from urllib, for a URL with no scheme.
  * `TypeError`, when `json=` is handed a payload `json.dumps` cannot encode.

Each is one keystroke away from the configuration a host actually writes, and
each turns a documented `SourceError` contract into a bare builtin the caller
has no reason to have written an `except` for.

THE RULE, mechanically: inside a function that raises `SourceError` (that is
the promise, in the code, not in the prose), a call to `client.get` /
`.post` / `.request` / `.send` / `.stream` must sit in a `try` whose handlers
cover `httpx.HTTPError`, `httpx.InvalidURL` AND `ValueError`. A handler naming
`Exception` covers all three. A handler naming a module-level tuple constant
is resolved to its members.

Deliberately NOT checked. `webhooks/deliver.py:_attempt` posts to a host-
supplied endpoint URL but raises no `SourceError`: it records a failed
attempt, because a dead receiver must not strand the drain, and its
`_CLIENT_ERRORS` already carries all four httpx roots. Its scheme is pinned
upstream by `urls.validate_endpoint_url`. Nothing here can tell whether a
given URL was validated somewhere else, so this gate asks only of the doors
that state a single-exception taxonomy of their own. Also not checked: the
`TypeError` arm. Only `evm/rpc.py` forwards a caller-shaped payload into
`json=`, and it pre-encodes with `json.dumps` to keep a caller's unencodable
argument apart from an internal `TypeError` of its own; demanding a blanket
`except TypeError` at every door would swallow real bugs.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO / "src" / "auradefi"

#: The httpx.Client methods that perform a send. Every one of them runs the
#: URL construction and the cookie extraction the finding came out of.
_SEND_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head",
                           "options", "request", "send", "stream"})

#: What a door must catch, by last dotted component so `httpx.HTTPError` and
#: a `from httpx import HTTPError` both read the same.
_REQUIRED = frozenset({"HTTPError", "InvalidURL", "ValueError"})

#: Handlers that cover everything, so the three above need not be spelled.
_CATCH_ALL = frozenset({"Exception", "BaseException"})


def _leaf(node: ast.AST) -> str:
    """Last dotted component of an exception reference: ``httpx.X`` -> ``X``."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _module_tuples(tree: ast.Module) -> dict[str, set[str]]:
    """``{NAME: {leaf, …}}`` for module-level tuple-of-exceptions constants.

    `deliver._CLIENT_ERRORS` is the shape: a named tuple is the good way to
    write a shared handler, and a gate that could not read one would push
    authors back to repeating the tuple inline.
    """
    tuples: dict[str, set[str]] = {}
    for node in tree.body:
        target, value = None, None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and isinstance(value, ast.Tuple):
            tuples[target.id] = {_leaf(element) for element in value.elts}
    return tuples


def _caught(handler: ast.ExceptHandler, tuples: dict[str, set[str]]) -> set[str]:
    """Every exception leaf name one ``except`` clause catches."""
    kind = handler.type
    if kind is None:
        return set(_CATCH_ALL)
    parts = kind.elts if isinstance(kind, ast.Tuple) else [kind]
    names: set[str] = set()
    for part in parts:
        leaf = _leaf(part)
        names |= tuples.get(leaf, {leaf})
    return names


def _owning_functions(tree: ast.Module) -> list[ast.AST]:
    """Every def in the module; nesting is handled by the body scan below."""
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _raises_source_error(function: ast.AST) -> bool:
    """True iff this function itself raises ``SourceError``.

    A nested def's raise belongs to the nested def, but a nested def is also
    walked in its own right, so counting it twice costs nothing and missing
    it would be a hole.
    """
    for node in ast.walk(function):
        if isinstance(node, ast.Raise) and node.exc is not None:
            call = node.exc
            target = call.func if isinstance(call, ast.Call) else call
            if _leaf(target) == "SourceError":
                return True
    return False


def _is_send(node: ast.AST) -> bool:
    """True for ``<something client-ish>.<send method>(…)``."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in _SEND_METHODS:
        return False
    return "client" in ast.unparse(node.func.value).lower()


def _guarded_sends(body: list[ast.stmt], guards: frozenset[str],
                   tuples: dict[str, set[str]]) -> list[tuple[ast.Call, frozenset[str]]]:
    """Every send in ``body``, paired with the handlers wrapping it.

    Descends statement by statement so a `try` contributes its handlers to
    its own body only: a send in the `except` or `finally` arm is NOT
    guarded by that `try`, which is exactly where a retry loop would put one.
    """
    found: list[tuple[ast.Call, frozenset[str]]] = []
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue  # walked as its own function
        if isinstance(statement, ast.Try):
            inner = guards
            for handler in statement.handlers:
                inner |= _caught(handler, tuples)
            found += _guarded_sends(statement.body, inner, tuples)
            for arm in (statement.handlers, statement.orelse, statement.finalbody):
                for node in arm:
                    found += _guarded_sends(
                        node.body if isinstance(node, ast.ExceptHandler) else [node],
                        guards, tuples,
                    )
            continue
        nested_blocks = [b for name in ("body", "orelse", "finalbody")
                         if isinstance(b := getattr(statement, name, None), list)]
        if nested_blocks:
            for block in nested_blocks:
                found += _guarded_sends(block, guards, tuples)
            # the loop/if header itself can hold a send too
            for node in ast.iter_child_nodes(statement):
                if isinstance(node, ast.expr):
                    found += [(c, guards) for c in ast.walk(node) if _is_send(c)]
            continue
        found += [(c, guards) for c in ast.walk(statement) if _is_send(c)]
    return found


def _offences(tree: ast.Module) -> list[tuple[int, frozenset[str]]]:
    """``(line, missing)`` for every under-guarded door in one module."""
    tuples = _module_tuples(tree)
    offences: list[tuple[int, frozenset[str]]] = []
    for function in _owning_functions(tree):
        if not _raises_source_error(function):
            continue
        for call, guards in _guarded_sends(function.body, frozenset(), tuples):
            if guards & _CATCH_ALL:
                continue
            missing = _REQUIRED - guards
            if missing:
                offences.append((call.lineno, frozenset(missing)))
    return offences


def _tree_offences() -> tuple[list[str], int]:
    """Every offending door under the source root, and how many were seen."""
    reported: list[str] = []
    doors = 0
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in _owning_functions(tree):
            if _raises_source_error(function):
                doors += sum(
                    1 for _ in _guarded_sends(function.body, frozenset(),
                                              _module_tuples(tree))
                )
        for line, missing in _offences(tree):
            reported.append(
                f"{path.relative_to(REPO)}:{line} catches neither "
                + " nor ".join(sorted(missing))
            )
    return reported, doors


def test_every_source_error_door_catches_all_three_roots() -> None:
    """See the module docstring's motivating finding (`EvmRpc._post`)."""
    reported, doors = _tree_offences()
    # Six today: evm/rpc, evm/etherscan, evm/txfetch, bitcoin/esplora,
    # solana/rpc, prices/oracles/defillama. A collapse to one or two means
    # the detector went blind, not that the doors were sealed.
    assert doors >= 5, (
        f"only {doors} transport doors found under {SOURCE_ROOT}: the "
        "detector has gone blind; fix it rather than enjoying a green gate"
    )
    assert not reported, (
        "a function promising SourceError sends through httpx without "
        "catching every root a send raises. httpx.HTTPError covers neither "
        "httpx.InvalidURL (a control character anywhere in the URL) nor "
        "urllib's ValueError (a scheme-less url like 'localhost:8545'):\n  "
        + "\n  ".join(reported)
    )


def test_the_gate_fires_on_the_motivating_door_and_clears_when_widened() -> None:
    """`EvmRpc._post` before and after the fix, as strings, no source read."""
    before = (
        "import httpx\n"
        "class R:\n"
        "    def _post(self, payload):\n"
        "        try:\n"
        "            response = self._client.post(self._url, json=payload)\n"
        "        except httpx.HTTPError as exc:\n"
        "            raise SourceError('failed') from exc\n"
        "        return response.json()\n"
    )
    after = before.replace(
        "except httpx.HTTPError as exc:",
        "except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:",
    )
    naked = before.replace("        try:\n", "").replace(
        "            response = self._client.post(self._url, json=payload)\n",
        "        response = self._client.post(self._url, json=payload)\n",
    ).replace("        except httpx.HTTPError as exc:\n", "        if False:\n")

    assert _offences(ast.parse(before)) == [(5, frozenset({"InvalidURL", "ValueError"}))]
    assert _offences(ast.parse(after)) == []
    # DefiLlama's shape: a door with no `try` around the send at all.
    assert [missing for _, missing in _offences(ast.parse(naked))] == [
        frozenset(_REQUIRED)
    ]


def test_a_named_tuple_of_exceptions_is_resolved() -> None:
    """`deliver._CLIENT_ERRORS` is the good shape; it must not read as bare."""
    module = ast.parse(
        "import httpx\n"
        "ERRORS = (httpx.HTTPError, httpx.InvalidURL, ValueError)\n"
        "def fetch(client):\n"
        "    try:\n"
        "        return client.get('u')\n"
        "    except ERRORS as exc:\n"
        "        raise SourceError('x') from exc\n"
    )
    assert _offences(module) == []


def test_a_send_in_the_except_arm_is_not_guarded_by_its_own_try() -> None:
    """A retry inside the handler is a second, unguarded door."""
    module = ast.parse(
        "import httpx\n"
        "def fetch(client):\n"
        "    try:\n"
        "        return client.get('u')\n"
        "    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:\n"
        "        if not client.get('retry'):\n"
        "            raise SourceError('x') from exc\n"
    )
    assert [line for line, _ in _offences(module)] == [6]


def test_a_door_that_promises_nothing_is_left_alone() -> None:
    """`Deliverer._attempt` records an attempt; it is not in this taxonomy."""
    module = ast.parse(
        "import httpx\n"
        "def attempt(client, url):\n"
        "    try:\n"
        "        return client.post(url).status_code\n"
        "    except httpx.HTTPError as exc:\n"
        "        return str(exc)\n"
    )
    assert _offences(module) == []
