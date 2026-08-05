#!/usr/bin/env bash
# Run every example offline. An example that no longer runs is worse than no
# example at all: it is a published lie about the package's surface, and the
# reader debugging it has no way to know whether they or the docs are wrong.
# So CI runs this, exactly as `scripts/run_books.sh` runs the notebooks.
#
#   bash scripts/run_examples.sh              # all of them, quiet
#   VERBOSE=1 bash scripts/run_examples.sh    # with each example's output
#
# An example needing an optional extra that is not installed is SKIPPED and
# said so: never silently counted as passing.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="python"
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; fi
if ! command -v "$PY" >/dev/null 2>&1 && [ ! -x "$PY" ]; then
  echo "python not found" >&2
  exit 2
fi

# example -> the import that must succeed for it to be runnable.
requires_for() {
  case "$1" in
    04_persist_to_your_database.py) echo "sqlmodel" ;;
    05_serve_the_http_api.py) echo "fastapi" ;;
    *) echo "" ;;
  esac
}

extra_for() {
  case "$1" in
    04_persist_to_your_database.py) echo "[sql]" ;;
    05_serve_the_http_api.py) echo "[api]" ;;
    *) echo "" ;;
  esac
}

shopt -s nullglob
examples=(examples/quickstart.py examples/[0-9][0-9]_*.py)
if [ ${#examples[@]} -eq 0 ]; then
  echo "no examples under examples/" >&2
  exit 2
fi

failed=0
skipped=0
ran=0
for example in "${examples[@]}"; do
  name="$(basename "$example")"
  printf "==> %-42s" "$example"

  module="$(requires_for "$name")"
  if [ -n "$module" ] && ! "$PY" -c "import $module" >/dev/null 2>&1; then
    echo "SKIPPED: needs 'pip install auradefi$(extra_for "$name")'"
    skipped=$((skipped + 1))
    continue
  fi

  if output=$("$PY" "$example" 2>&1); then
    echo "ran clean"
    ran=$((ran + 1))
    if [ -n "${VERBOSE:-}" ]; then printf '%s\n' "$output" | sed 's/^/    /'; fi
  else
    echo "FAILED"
    printf '%s\n' "$output" | tail -25 | sed 's/^/    /'
    failed=$((failed + 1))
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "==> ${failed} example(s) FAILED"
  exit 1
fi
echo "==> ${ran} example(s) ran clean, ${skipped} skipped for missing extras"
