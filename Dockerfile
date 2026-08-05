# auradefi — library image.
#
# Stage `test` proves the SPEC §13 acceptance criterion in a container:
# a fresh tree, no API keys, no network, suite green. It installs the
# `[dev]` extra, which carries the optional runtime dependencies the suite
# exercises (fastapi for `api/`, sqlmodel for `ledger/backends/`) alongside
# pytest and the notebook toolchain.
#
# Stage `runtime` is a minimal image with only the CORE install — httpx and
# nothing else — plus the quickstart, which is written to degrade
# gracefully when the [sql] and [api] extras are absent. It is the base for
# an API service (add `pip install 'auradefi[api]'`) and for hosts wanting
# a pinned import environment.
#
#   docker build --target test -t auradefi:test .
#   docker run --rm --network none auradefi:test        # the SPEC §13 proof
#   docker build -t auradefi:0.1.0 .
#   docker run --rm --network none auradefi:0.1.0

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --quiet .

FROM base AS test
COPY tests ./tests
COPY docs ./docs
# The suite reads the published surface, not just the source: `examples/` and
# its runner are asserted by tests/style/test_examples_are_published.py and
# tests/style/test_docs_pin_live_values.py, so a container missing them fails
# five gates that are green on the host — the exact host/image divergence the
# CHANGELOG/STATUS copy below was added to close.
COPY examples ./examples
COPY scripts ./scripts
# tests/style/test_release_note_companions.py checks that a version pinned in
# docs/internal/DECISIONS.md owns a CHANGELOG section. STATUS.md moved under
# docs/internal/, which the `COPY docs` above already carries.
COPY CHANGELOG.md ./
# [dev] = pytest + build/twine + nbformat/nbclient/ipykernel + fastapi + sqlmodel.
RUN pip install --quiet ".[dev]"
# Bare `pytest`: pyproject's addopts already carries -q, and a second -q
# would suppress the pass/fail summary line this image exists to print.
CMD ["pytest"]

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 1000 auradefi
COPY --from=base /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY examples/quickstart.py /home/auradefi/quickstart.py
USER auradefi
WORKDIR /home/auradefi
CMD ["python", "quickstart.py"]
