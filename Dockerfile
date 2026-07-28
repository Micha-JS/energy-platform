FROM python:3.12-slim AS base

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

# ---------------------------------------------------------------------------------------------
# M9's Streamlit dashboard. A separate stage rather than one fatter image: Streamlit and its tail
# are pure weight in the webserver and daemon containers, which never render a page. The same
# argument pyproject.toml makes for keeping `dashboard` an extra rather than a runtime dependency.
FROM base AS dashboard
USER root
RUN uv sync --frozen --no-dev --extra dashboard
COPY dashboard ./dashboard
RUN chown -R dagster:dagster /app
USER dagster
EXPOSE 8501
# The slim image has no curl, and installing one to run a healthcheck would be a package for a
# one-liner. Streamlit's own health endpoint, hit with the stdlib.
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
    CMD python -c "import urllib.request as r; r.urlopen('http://localhost:8501/_stcore/health')"

# ---------------------------------------------------------------------------------------------
# The application image, LAST ON PURPOSE. An untargeted `docker build .` resolves to the final
# stage, so keeping this here means that build stays the app image it has always been instead of
# silently becoming the dashboard. Docker only builds the stages a target needs, so asking for
# this one never builds the stage above.
FROM base AS app
