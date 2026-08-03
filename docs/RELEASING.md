# Releasing auradefi

Everything below is prepared automatically; the two **publish** steps are
manual because they need your credentials.

## 1. Verify

```bash
bash scripts/release_check.sh
```

Builds sdist + wheel, runs `twine check`, verifies wheel contents
(`py.typed` in, `tests/` out), installs the wheel into a fresh venv, and
runs the quickstart against it.

```bash
docker build --target test -t auradefi:test . && docker run --rm --network none auradefi:test
```

The suite green inside a network-less container is the SPEC §13 criterion,
machine-checked.

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
  `src/auradefi/__init__.py` together.
