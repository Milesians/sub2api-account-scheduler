FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY --from=builder /app/.venv /app/.venv
COPY config.yaml .
HEALTHCHECK --interval=5m --timeout=10s --start-period=5m CMD \
  python -c "import os,sys,time; p=os.getenv('HEARTBEAT_FILE','/data/last_tick'); sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p)<7200 else 1)"
ENTRYPOINT ["python", "-m", "scheduler"]
