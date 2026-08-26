# Multi-stage build.
#
#   docker build .                  -> `full`: the server WITH the bundled corpus baked in, so
#                                      `docker run -p 8000:8000 <image>` serves immediately. The
#                                      corpus ships inside the wheel, so this needs no network and
#                                      adds seconds, not minutes, to the build.
#   docker build --target serve .   -> the server WITHOUT any corpus, for a deployment that mounts
#                                      its own data dir over /app/data. Build that corpus on the
#                                      host with `backlot import …` — importing anything the image
#                                      does not already contain belongs there, not in a build arg,
#                                      since the COPYs below are all this build can see.
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
# Both are named by pyproject (`readme`, `license-files`). Neither is required to build — setuptools
# tolerates either being absent, silently — but the installed dist-info then loses it: without
# LICENSE the image redistributes MIT-licensed code carrying no copy of the notice, and without
# README.pypi.md `pip show backlot` in the container has an empty description.
COPY pyproject.toml README.pypi.md LICENSE ./
COPY backlot ./backlot
# .dockerignore keeps .git out of the build context, so setuptools-scm finds no tag to read and the
# image carries the fallback version pyproject names. Pass the version being built to stamp it:
#     docker build --build-arg VERSION=0.0.1 .
ARG VERSION
RUN SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION} pip install --no-cache-dir .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"
# `backlot serve` is the console script `pip install .` above put on PATH; it passes these through
# to uvicorn unchanged. --forwarded-allow-ips=* so that, behind a TLS-terminating proxy/ALB, the app
# honors X-Forwarded-Proto/Host and emits https self-URLs (PyGithub follows those URLs). Proxy
# headers are honoured by default, so there is no flag for them here.
CMD ["backlot", "serve", "--host", "0.0.0.0", "--port", "8000", "--forwarded-allow-ips", "*"]


# ---------------------------------------------------------------- builder (bakes the corpus)
FROM serve AS builder

# `--bundled` is the corpus that ships inside the wheel, so this needs no path of its own and
# cannot drift from wherever the install puts the file. Its own stage so the final image starts from
# a clean `serve` and carries only the two runtime files the COPY below names.
RUN backlot import --bundled


# ---------------------------------------------------------------- full (default target)
FROM serve AS full

# Only the runtime data (the DB + the roster it generated); everything else the import wrote stays
# behind in the builder.
COPY --from=builder /app/data/mock.sqlite /app/data/mock.sqlite
COPY --from=builder /app/data/tokens.yaml /app/data/tokens.yaml
