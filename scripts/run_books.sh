#!/usr/bin/env bash
# Execute every PyBook headlessly: an unexecuted notebook is undelivered
# work. Each book must run OFFLINE (cassettes or in-memory fixtures) and
# assert its own outputs, so this doubles as a documentation test.
#
#   bash scripts/run_books.sh            # verify (does not rewrite outputs)
#   BOOKS_INPLACE=1 bash scripts/run_books.sh   # re-execute and save outputs
set -euo pipefail
cd "$(dirname "$0")/.."

JUPYTER="jupyter"
if [ -x ".venv/bin/jupyter" ]; then JUPYTER=".venv/bin/jupyter"; fi
if ! command -v "$JUPYTER" >/dev/null 2>&1 && [ ! -x "$JUPYTER" ]; then
  echo "jupyter not found: install the dev extra: pip install '.[dev]'" >&2
  exit 2
fi

INPLACE=""
if [ -n "${BOOKS_INPLACE:-}" ]; then INPLACE="--inplace"; fi

shopt -s nullglob
books=(docs/books/*.ipynb)
if [ ${#books[@]} -eq 0 ]; then
  echo "no notebooks under docs/books/" >&2
  exit 2
fi

failed=0
for nb in "${books[@]}"; do
  printf '==> %-34s' "$nb"
  if log=$("$JUPYTER" execute $INPLACE "$nb" 2>&1); then
    echo "executed clean"
  else
    echo "FAILED"
    printf '%s\n' "$log" | tail -25
    failed=$((failed + 1))
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "==> ${failed} notebook(s) FAILED"
  exit 1
fi
echo "==> all ${#books[@]} notebooks executed clean"
