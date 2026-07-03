FROM python:3.12-slim

# System deps for Arelle (XBRL) and lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files + local normalization package first for layer caching.
# These change far less often than application code.
COPY pyproject.toml uv.lock ./
COPY normalization/ ./normalization/

# Install dependencies only (project source not copied yet — maximises cache)
RUN uv sync --frozen --no-dev --no-install-project

# Copy the rest of the source
COPY . .

# Install the project itself (registers sec-scraper-worker entry point)
RUN uv sync --frozen --no-dev

CMD ["uv", "run", "sec-scraper-worker"]
