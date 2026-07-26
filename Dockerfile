# Multi-stage build: the builder downloads a small per-source subset of the
# EnterpriseRAG-Bench corpus and bakes it into a SQLite DB, so the runtime image
# starts instantly with data already present — `docker run -p 8000:8000 <image>`.
#
# Build the full corpus instead with:  docker build --build-arg BUILD_ARGS=--all .

FROM python:3.13-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir \
    fastapi "uvicorn[standard]" pydantic pydantic-settings pyyaml python-multipart graphql-core

WORKDIR /app
COPY app ./app

ARG BUILD_ARGS=""
RUN python -m app.importer.erb ${BUILD_ARGS}


FROM python:3.13-slim

ENV PATH="/opt/venv/bin:$PATH" \
    MOCK_DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY app ./app
# Copy only the runtime data (baked DB + tokens); raw zips are left in the builder.
COPY --from=builder /app/data/mock.sqlite /app/data/mock.sqlite
COPY --from=builder /app/data/tokens.yaml /app/data/tokens.yaml

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"
# --proxy-headers + --forwarded-allow-ips=* so that, behind a TLS-terminating proxy/ALB, the
# app honors X-Forwarded-Proto/Host and emits https self-URLs (PyGithub follows those URLs).
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
