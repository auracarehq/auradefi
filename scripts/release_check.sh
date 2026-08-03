#!/usr/bin/env bash
# Release readiness gate. Exits non-zero unless the package is genuinely
# pip-installable from its own artifacts. Run locally or in CI; needs a
# python with pip OR uv on PATH.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-python3}"
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; fi

run_pip() { "$1" -m pip "${@:2}"; }

echo "==> building sdist + wheel"
rm -rf dist
"$PY" -m build --outdir dist

echo "==> twine check"
"$PY" -m twine check dist/*

echo "==> wheel contents sanity"
"$PY" - <<'EOF'
import glob, sys, zipfile
wheel = glob.glob("dist/*.whl")[0]
names = zipfile.ZipFile(wheel).namelist()
assert any(n.endswith("auradefi/py.typed") for n in names), "py.typed missing from wheel"
assert not any(n.startswith("tests/") for n in names), "tests leaked into wheel"
print(f"    {wheel}: {len(names)} files ok")
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
"$SMOKE/bin/python" - <<'EOF'
import auradefi
assert auradefi.__version__ == "0.1.0", auradefi.__version__
print(f"    import auradefi {auradefi.__version__} ok (wheel, fresh venv)")
EOF
if [ -f docs/examples/quickstart.py ]; then
  "$SMOKE/bin/python" docs/examples/quickstart.py >/dev/null
  echo "    quickstart.py ran green against the installed wheel"
fi

echo "==> release check PASSED"
