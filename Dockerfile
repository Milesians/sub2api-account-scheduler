FROM node:22-bookworm-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
WORKDIR /app/backend
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/src/ src/
COPY --from=frontend-builder /app/backend/src/scheduler/frontend src/scheduler/frontend
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=builder /app/backend/.venv /app/.venv
COPY config.yaml .
HEALTHCHECK --interval=5m --timeout=10s --start-period=5m CMD \
  python -c "import os,sys,time; p=os.getenv('HEARTBEAT_FILE','/data/last_tick'); sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<7200 else 1)"
ENTRYPOINT ["python", "-m", "scheduler"]
