FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.11-slim AS builder

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock* README.md ./
COPY src/ ./src/

RUN uv sync --frozen --no-dev

FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/
COPY pyproject.toml ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

EXPOSE 8080

ENTRYPOINT ["python", "-m", "intervals_icu_mcp.remote_server"]
