# syntax=docker/dockerfile:1
FROM python:3.12-slim

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
# BuildKit cache mount stores downloaded wheels so re-installs after a lockfile
# change only fetch packages that actually changed.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ── Source layer (fast rebuild — all heavy layers above are cached) ───────────
COPY . .

# Register the project entry-point.  Wheels are already installed; this is
# near-instant because uv only needs to link the package into the venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Use the venv entry-point directly so startup never triggers a uv sync check.
# uv run would rebuild local packages on every restart because the full project
# is volume-mounted; calling the pre-installed script avoids that entirely.
CMD ["/app/.venv/bin/sec-scraper-worker"]
