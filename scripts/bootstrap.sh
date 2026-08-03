#!/usr/bin/env bash
# Dev environment bootstrap for machines without pip/ensurepip (stock
# Ubuntu python3). Uses the uv standalone installer — no sudo required.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
  echo "installing uv to ~/.local/bin ..."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME/.local/bin" sh
  export PATH="$HOME/.local/bin:$PATH"
fi

uv venv .venv --seed
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest -q
echo "bootstrap complete — activate with: source .venv/bin/activate"
