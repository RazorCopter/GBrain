# syntax=docker/dockerfile:1.7
FROM node:24.12.0-bookworm-slim@sha256:7326fb2dbdce998edd72140946851be64ef4a643e8715e138ca467e8e9d92c99 AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/ ./
RUN npm run build

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS python-builder
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /build
RUN pip install --no-cache-dir uv==0.12.19
COPY pyproject.toml uv.lock README.md ./
COPY backend/ ./backend/
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GBRAIN_FRONTEND_DIR=/app/frontend/dist
WORKDIR /app
RUN addgroup --system --gid 10001 gbrain \
    && adduser --system --uid 10001 --ingroup gbrain --home /var/lib/gbrain gbrain \
    && mkdir -p /var/lib/gbrain/data /var/lib/gbrain/cache /var/lib/gbrain/import /var/lib/gbrain/backup \
    && chown -R gbrain:gbrain /var/lib/gbrain
COPY --from=python-builder /opt/venv /opt/venv
COPY alembic.ini ./
COPY backend/migrations ./backend/migrations
COPY scripts ./scripts
COPY --from=frontend-builder /build/frontend/dist ./frontend/dist
RUN chmod 0555 /app/scripts/entrypoint.sh \
    && chown -R gbrain:gbrain /app
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["web"]
