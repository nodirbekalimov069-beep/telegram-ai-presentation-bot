# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS node-runtime

FROM python:3.12-slim-bookworm AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /bin/
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/home/app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        curl \
        fontconfig \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-core \
        libreoffice-impress \
        libreoffice-writer \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY apps ./apps
COPY core ./core
COPY modules ./modules
COPY migrations ./migrations
COPY scripts ./scripts
COPY alembic.ini ./

RUN uv sync --frozen --no-dev --no-editable \
    && npm ci --omit=dev --prefix /app/apps/presentation_engine \
    && npm cache clean --force \
    && addgroup --system --gid 10001 app \
    && adduser --system --uid 10001 --gid 10001 --home /home/app app \
    && mkdir -p /app/storage /app/tmp/generation /home/app/.cache \
    && chown -R app:app /app /home/app

USER app

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
