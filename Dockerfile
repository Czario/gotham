# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

# ── System packages ─────────────────────────────────────────────────────────
# Cache mount keeps downloaded .deb files between rebuilds.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libxml2 \
    libxslt1.1

# ── uv (pinned minor — avoids surprise cache invalidation from upstream) ─────
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /usr/local/bin/uv

WORKDIR /app

# ── Dependency layer (rebuilt only when pyproject.toml / uv.lock change) ─────
# normalization/ is a local editable dep; copy it alongside the lockfile so
# this layer is only invalidated when deps or normalization code changes.
COPY pyproject.toml uv.lock ./
COPY normalization/ ./normalization/

# Install all third-party packages WITHOUT the project itself.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ── Worker 1: 10-K / 10-Q / 20-F XBRL worker (lean — no Chromium) ───────────
FROM base AS worker-10kq

COPY . .

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

CMD ["/app/.venv/bin/sec-scraper-worker"]

# ── Worker 2: 8-K / 6-K / IR PDF earnings worker (includes Playwright/Chromium)
FROM base AS worker-8k

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libcairo2 libpangocairo-1.0-0 libx11-6 libx11-xcb1 libxcb1 \
    libxext6 fonts-liberation

# CRITICAL: placed BEFORE "COPY . ." so a code-only edit never re-downloads Chromium.
RUN uv run playwright install chromium

COPY . .

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

CMD ["/app/.venv/bin/earnings-8k-worker"]
