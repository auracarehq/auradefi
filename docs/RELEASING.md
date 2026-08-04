# Releasing auradefi

Everything below is prepared automatically; the two **publish** steps are
manual because they need your credentials.

## 1. Verify — the full gate

Run all four. Each prints its own PASSED/summary line; none may be
assumed.

```bash
.venv/bin/pytest                      # 3,027 tests, offline, no API keys
bash scripts/release_check.sh         # build -> twine -> wheel contents -> fresh venv
bash scripts/run_books.sh             # every PyBook executed headlessly
```

`release_check.sh` builds the sdist + wheel, runs `twine check`, verifies
the wheel's contents (`py.typed` in, `tests/` out), installs the wheel into
a **fresh** venv with only core dependencies, and runs
`docs/examples/quickstart.py` against it — which is why the quickstart must
degrade gracefully when the `[sql]` and `[api]` extras are absent.

```bash
docker build --target test -t auradefi:test .
docker run --rm --network none auradefi:test          # SPEC §13, machine-checked

docker build -t auradefi:0.1.0 .
docker run --rm --network none auradefi:0.1.0         # quickstart on the core install

docker run --rm --network none auradefi:test \
  bash -c 'for nb in docs/books/*.ipynb; do jupyter execute "$nb" || exit 1; done'
```

The suite green **inside a container with no network interface** is the
SPEC §13 acceptance criterion; running the notebooks there too proves the
docs are genuinely offline rather than merely passing behind the pytest
socket guard.

Equivalently, via compose:

```bash
docker compose run --rm test
docker compose run --rm demo
docker compose run --rm books
```

## 2. Tag

```bash
git tag -a v0.1.0 -m "auradefi 0.1.0"
git push origin v0.1.0
```

## 3. Publish to PyPI (manual — needs your token)

```bash
# TestPyPI first, always:
.venv/bin/python -m twine upload --repository testpypi dist/*
pip install --index-url https://test.pypi.org/simple/ --no-deps auradefi

# then the real thing:
.venv/bin/python -m twine upload dist/*
```

Use a project-scoped API token (`__token__` / `pypi-...`), never a
password. First upload claims the `auradefi` name (verified free
2026-08-02).

## 4. Publish the container (manual — needs registry login)

```bash
docker build -t ghcr.io/auracarehq/auradefi:0.1.0 -t ghcr.io/auracarehq/auradefi:latest .
docker push ghcr.io/auracarehq/auradefi:0.1.0
docker push ghcr.io/auracarehq/auradefi:latest
```

## 5. After

- Move the `[0.1.0]` section in `CHANGELOG.md` from "in progress" to dated.
- Bump `version` in `pyproject.toml` **and** `__version__` in
  `src/auradefi/__init__.py` together — `scripts/release_check.sh` asserts
  the installed wheel reports the expected version, so a half-bumped
  release fails the gate rather than shipping.
- Re-run `bash scripts/run_books.sh` if any public API changed: a notebook
  is a test, and a stale one is a lie.
