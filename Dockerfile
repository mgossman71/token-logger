# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first (better layer caching).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code, reporter, healthcheck, and pricing config.
COPY app ./app
COPY report.py ./
COPY healthcheck.py ./
COPY pricing.json ./
COPY static ./static

# Test suite + pytest config so `docker compose run ... python -m pytest` works
# inside the image (the live integration test is excluded via .dockerignore).
COPY tests ./tests
COPY pytest.ini ./

# Data directory for usage.jsonl (bind-mounted at runtime).
ENV DATA_DIR=/data \
    LISTEN_PORT=11435 \
    PRICING_FILE=/app/pricing.json
RUN mkdir -p /data

# Primary service listens on 11435; the AMD instance overrides LISTEN_PORT to 11436.
EXPOSE 11435

# Pure liveness healthcheck: the ASGI server accepts TCP connections.
# (Upstream Ollama reachability is covered by the /api/version passthrough,
#  which is what HAProxy already uses as its health check.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

# `sh -c` so the runtime LISTEN_PORT env is honoured for both instances.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${LISTEN_PORT:-11435}"]
