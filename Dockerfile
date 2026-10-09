FROM python:3.13-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/

RUN uv sync --frozen --no-dev --no-cache

# Run as an unprivileged user; own /app so the venv is readable.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# stdio only — there is no HTTP transport (the token acts as YOU). Run it with stdin
# attached, passing the env from a file so the token never lands in shell history:
#   docker run -i --rm --env-file .env <image>
CMD ["amazing-slack-mcp"]
