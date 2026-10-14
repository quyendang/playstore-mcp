FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md UPSTREAM_README.md LICENSE ./
COPY src/ src/
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.14-slim
LABEL org.opencontainers.image.source="https://github.com/quyendang/playstore-mcp"
WORKDIR /app
RUN groupadd --gid 10001 mcp && useradd --uid 10001 --gid mcp --no-create-home --shell /usr/sbin/nologin mcp
COPY --from=builder /app/.venv /app/.venv
COPY docker_entrypoint.py /app/docker_entrypoint.py
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BRIDGE_DATA_DIR=/data
EXPOSE 8000
# The entrypoint initializes the mounted volume as root, then drops to UID 10001.
ENTRYPOINT ["python", "/app/docker_entrypoint.py"]
