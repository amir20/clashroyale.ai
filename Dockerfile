# syntax=docker/dockerfile:1

# Build the Svelte UI.
FROM oven/bun:1-slim AS web
WORKDIR /web
COPY web/package.json web/bun.lock ./
RUN bun install --frozen-lockfile
COPY web/ ./
RUN bun run build

# Python app, serving the API and the built UI.
FROM ghcr.io/astral-sh/uv:python3.15-trixie-slim AS app
WORKDIR /app
ENV UV_LINK_MODE=copy
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev
COPY --from=web /web/dist ./web/dist

RUN useradd --create-home --uid 10001 rook && mkdir data && chown -R rook:rook data
USER rook
ENV PATH="/app/.venv/bin:$PATH"
VOLUME /app/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/', timeout=4)"]
CMD ["crai", "serve", "--host", "0.0.0.0", "--port", "8000"]
