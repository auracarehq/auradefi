#!/usr/bin/env bash
# Release readiness gate. Exits non-zero unless the package is genuinely
# pip-installable from its own artifacts. Run locally or in CI; needs a
# python with pip OR uv on PATH.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-python3}"
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; fi

run_pip() { "$1" -m pip "${@:2}"; }

echo "==> version agreement (pyproject vs __init__)"
VERSION="$("$PY" - <<'EOF'
import pathlib, re, tomllib
declared = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"]
source = pathlib.Path("src/auradefi/__init__.py").read_text()
match = re.search(r'__version__\s*=\s*"([^"]+)"', source)
assert match, "__version__ not found in src/auradefi/__init__.py"
assert match.group(1) == declared, (
    f"half-bumped release: pyproject says {declared!r}, "
    f"__init__ says {match.group(1)!r}"
)
print(declared)
EOF
)"
echo "    $VERSION (pyproject == __init__)"

echo "==> building sdist + wheel"
rm -rf dist
"$PY" -m build --outdir dist

echo "==> twine check"
"$PY" -m twine check dist/*

echo "==> wheel contents sanity"
"$PY" - <<'EOF'
import glob, zipfile
wheel = glob.glob("dist/*.whl")[0]
names = zipfile.ZipFile(wheel).namelist()
assert any(n.endswith("auradefi/py.typed") for n in names), "py.typed missing from wheel"
assert not any(n.startswith("tests/") for n in names), "tests leaked into wheel"
# The Sandbox recording is a PRODUCT file, not a test fixture: without it
# `pip install auradefi` plus five lines fails, for the one audience with
# no checkout to debug against. Nothing else in the build would notice.
fixtures = [n for n in names if n.startswith("auradefi/sources/fixtures/") and n.endswith(".json")]
assert fixtures, "the Sandbox recording is missing from the wheel"
print(f"    {wheel}: {len(names)} files ok, {len(fixtures)} sandbox fixture(s)")
EOF

echo "==> fresh-venv wheel install smoke test"
SMOKE="$(mktemp -d)/smoke"
if command -v uv >/dev/null 2>&1 || [ -x "$HOME/.local/bin/uv" ]; then
  export PATH="$HOME/.local/bin:$PATH"
  uv venv "$SMOKE" --seed >/dev/null
  uv pip install --python "$SMOKE/bin/python" dist/*.whl >/dev/null
else
  "$PY" -m venv "$SMOKE"
  run_pip "$SMOKE/bin/python" install --quiet dist/*.whl
fi
AURADEFI_EXPECTED_VERSION="$VERSION" "$SMOKE/bin/python" - <<'EOF'
import os
import auradefi
expected = os.environ["AURADEFI_EXPECTED_VERSION"]
assert auradefi.__version__ == expected, (auradefi.__version__, expected)
print(f"    import auradefi {auradefi.__version__} ok (wheel, fresh venv)")
EOF
if [ -f examples/quickstart.py ]; then
  # The fresh venv has CORE dependencies only, so this also proves the
  # quickstart degrades gracefully without the [sql] and [api] extras.
  "$SMOKE/bin/python" examples/quickstart.py >/dev/null
  echo "    quickstart.py ran green against the installed wheel (core deps only)"
fi
# Every OTHER example, also against the installed wheel: each one is
# self-contained and reads nothing from this checkout, so a reader who copies
# one file out and pip-installs the package gets exactly this behaviour. The
# two needing an extra skip themselves, loudly.
for example in examples/[0-9][0-9]_*.py; do
  [ -f "$example" ] || continue
  case "$(basename "$example")" in
    04_persist_to_your_database.py) needs="sqlmodel" ;;
    05_serve_the_http_api.py) needs="fastapi" ;;
    *) needs="" ;;
  esac
  # Probed, not inferred from the traceback: this venv holds CORE deps only,
  # so an extras example is EXPECTED to be unrunnable here, and guessing that
  # from an error message would also swallow a real import bug.
  if [ -n "$needs" ] && ! "$SMOKE/bin/python" -c "import $needs" >/dev/null 2>&1; then
    echo "    $(basename "$example") SKIPPED: needs the $needs extra (CI runs it)"
    continue
  fi
  if output=$("$SMOKE/bin/python" "$example" 2>&1); then
    echo "    $(basename "$example") ran green against the installed wheel"
  else
    printf '%s\n' "$output" | tail -20
    echo "    $(basename "$example") FAILED against the installed wheel" >&2
    exit 1
  fi
done

# An unexecuted document is undelivered work. `pytest` never opens a
# notebook, so without this the loop's own gates cannot see a published book
# that has gone stale against the code (it happened: docs/books/09 asserted a
# connection id 0.1.1 had stopped minting, with the suite green). CI executes
# the books in three jobs; this puts them inside the release gate too.
echo "==> executable documentation (docs/books)"
if [ -x ".venv/bin/jupyter" ] || command -v jupyter >/dev/null 2>&1; then
  bash scripts/run_books.sh
else
  echo "    SKIPPED: no jupyter on PATH and no .venv/bin/jupyter."
  echo "    The books are NOT verified by this run: install the dev extra"
  echo "    (pip install '.[dev]') or rely on the CI 'notebooks' job."
fi

echo "==> release check PASSED"
