FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM node:24-alpine@sha256:d32cdf619f63fe0471182d08996dd516c6275bb5fd31ae06e55a570bd9e1ad43 AS codex
RUN npm install --prefix /opt/codex --ignore-scripts --no-audit --no-fund @openai/codex@0.167.0 \
    && find /opt/codex/node_modules -type d -path '*/vendor/*-unknown-linux-musl' -exec cp -a '{}' /codex-runtime ';' \
    && test -x /codex-runtime/bin/codex \
    && /codex-runtime/bin/codex --version

FROM node:24-alpine@sha256:d32cdf619f63fe0471182d08996dd516c6275bb5fd31ae06e55a570bd9e1ad43 AS frontend
WORKDIR /ui/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend ./
RUN node build.mjs

FROM python:3.14-slim@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY --from=frontend /ui/src/gas_web/static/ ./src/gas_web/static/
RUN uv sync --frozen --no-dev --no-editable --compile-bytecode

FROM python:3.14-slim@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/data/home \
    XDG_DATA_HOME=/data \
    CODEX_BIN=/usr/local/bin/codex
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --home-dir /data/home --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app /data/home \
    && chown -R app:app /data \
    && chmod 0700 /data /data/home \
    && ln -s /opt/codex/bin/codex /usr/local/bin/codex
COPY --from=build /opt/venv /opt/venv
COPY --from=codex /codex-runtime /opt/codex
WORKDIR /app
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).close()"]
CMD ["iacpaas-web", "--host", "0.0.0.0", "--port", "8080", "--db", "/data/chat.sqlite3"]
