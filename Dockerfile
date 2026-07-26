# syntax=docker/dockerfile:1
# Multi-stage build: compile the React SPA, then assemble the Python image.

# --- Stage 1: build the web frontend ---------------------------------------
FROM node:22-alpine AS web-build
WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# --- Stage 2: production image ---------------------------------------------
FROM python:3.12-slim AS production

# curl is needed for the container healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv

WORKDIR /app

# Python project metadata + lockfile + alembic config.
COPY pyproject.toml uv.lock alembic.ini ./
# Alembic migration scripts (copied as a directory so version files land in
# /app/migrations/versions/, not flattened into /app/).
COPY migrations ./migrations
# Postgres init scripts shipped alongside the image for compose parity.
COPY scripts/postgres-init ./scripts/postgres-init
# Backend application source.
COPY backend/src/bidscope ./backend/src/bidscope
# Built SPA assets copied into the static dir the FastAPI app serves.
COPY --from=web-build /app/dist ./backend/src/bidscope/static
# Evaluation fixtures.
COPY data/ ./data/

RUN uv pip install --system -e .

# Run as a non-root user. Create the local object-store root (used when
# object_store_type=local) and hand it to the runtime user before the USER
# switch so the app can write DOCX outputs and snapshot payloads without a
# permission error. The S3 backend writes to MinIO, but the directory is
# harmless to pre-create and keeps the image usable in local mode.
RUN useradd --uid 1000 --create-home bidscope \
    && mkdir -p /app/data/objects \
    && chown -R bidscope:bidscope /app/data
USER bidscope

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

CMD ["bidscope", "api", "serve", "--host", "0.0.0.0", "--port", "8000"]
