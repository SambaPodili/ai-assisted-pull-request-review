# ── Build stage ────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.13-slim

# git is needed by ingestion layer (clone/diff)
RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY . .

# Runtime directories (SQLite DB + log files)
RUN mkdir -p data logs

# Non-root user, OpenShift-compatible.
# OpenShift's restricted SCC runs the container with an ARBITRARY UID that is
# always a member of the root group (GID 0). Making /app owned by group 0 and
# group-writable (g=u) lets both a fixed UID (plain Docker) and a random UID
# (OpenShift) read/write data/ and logs/.
RUN useradd -u 1001 -r -g 0 -d /app -s /sbin/nologin appuser \
    && chown -R 1001:0 /app \
    && chmod -R g=u /app
USER 1001

EXPOSE 8080

# /live is a lightweight liveness check (always 200 while the process runs)
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:8080/live || exit 1

CMD ["python", "main.py"]
