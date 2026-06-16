FROM ghcr.io/astral-sh/uv:python3.10-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

COPY app.py coramail_db.py ./
COPY static ./static
COPY templates ./templates
COPY reference_html ./reference_html
COPY pipeline/*.py ./pipeline/

EXPOSE 8011

CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8011"]
