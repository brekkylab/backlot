# Multi-stage build.
#
#   docker build .                       -> `full`: the server WITH a corpus baked in, so
#                                           `docker run -p 8000:8000 <image>` serves immediately.
#   docker build --target serve .        -> the server WITHOUT any corpus. For a deployment that
#                                           mounts its own data dir over /app/data, where a baked
#                                           corpus is downloaded, imported, shipped and then
#                                           discarded at startup. Builds in a minute instead of
#                                           downloading ~1GB and importing half a million docs.
#   docker build --build-arg BUILD_ARGS=--all .   -> bake the full corpus rather than a slice.
#
# Dependencies come from pyproject.toml, NOT a list repeated here. A hand-kept copy silently went
# stale every time a runtime dep was added — the image ended up missing jsonschema, pyjwt and
# httpx, and each was papered over with a `docker exec pip install` that a container recreate
# would have thrown away. Installing the package makes that class of drift impossible.

# ---------------------------------------------------------------- serve (no corpus)
FROM python:3.13-slim AS serve

ENV PATH="/opt/venv/bin:$PATH" \
    BACKLOT_DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN python -m venv /opt/venv
# pyproject declares `readme`, so README.md has to be present for the install to resolve.
COPY pyproject.toml README.md ./
COPY backlot ./backlot
RUN pip install --no-cache-dir .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"
# --proxy-headers + --forwarded-allow-ips=* so that, behind a TLS-terminating proxy/ALB, the
# app honors X-Forwarded-Proto/Host and emits https self-URLs (PyGithub follows those URLs).
CMD ["python", "-m", "uvicorn", "backlot.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]


# ---------------------------------------------------------------- builder (bakes a corpus)
FROM serve AS builder

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG BUILD_ARGS=""
RUN python -m backlot.importer.erb ${BUILD_ARGS}


# ---------------------------------------------------------------- full (default target)
FROM serve AS full

# Only the runtime data (baked DB + rosters); the raw tarball stays in the builder.
COPY --from=builder /app/data/mock.sqlite /app/data/mock.sqlite
COPY --from=builder /app/data/tokens.yaml /app/data/tokens.yaml
