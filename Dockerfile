# auradefi — library image.
#
# Stage `test` proves the SPEC §13 acceptance criterion in a container:
# a fresh tree, no API keys, suite green. Stage `runtime` is a minimal
# image with the built wheel installed — the base for the Phase 8 API
# service and for hosts wanting a pinned import environment.
#
#   docker build --target test -t auradefi:test . && docker run --rm auradefi:test
#   docker build -t auradefi:0.1.0 .
#   docker run --rm auradefi:0.1.0

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --quiet .

FROM base AS test
COPY tests ./tests
COPY docs ./docs
RUN pip install --quiet ".[dev]"
CMD ["pytest", "-q"]

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 1000 auradefi
COPY --from=base /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY docs/examples/quickstart.py /home/auradefi/quickstart.py
USER auradefi
WORKDIR /home/auradefi
CMD ["python", "quickstart.py"]
