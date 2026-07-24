FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /bin/uv

ENV DAGSTER_HOME=/opt/dagster/home \
    PATH="/app/.venv/bin:$PATH" \
    UV_COMPILE_BYTECODE=1

WORKDIR /app
RUN mkdir -p /opt/dagster/home

# Layer 1: locked dependencies only (cache-friendly, reproducible via --frozen).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Layer 2: source + install the package itself.
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Run as a non-root user so an exploit in the exposed webserver can't act as root.
RUN useradd --create-home --uid 1000 dagster \
    && chown -R dagster:dagster /opt/dagster /app
USER dagster
