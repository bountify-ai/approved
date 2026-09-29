# syntax=docker/dockerfile:1
# The Approved judge worker. No credential is built in: every secret arrives at runtime
# through the environment or a *_FILE path (see .env.example).
FROM ghcr.io/astral-sh/uv:0.11.26 AS uv

FROM python:3.11-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    WANDB_ERROR_REPORTING=false
WORKDIR /app

# Dependencies first (cached until the lockfile changes), then the package.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --home-dir /app approved \
 && mkdir -p /state && chown approved /state
USER approved
ENV PATH=/app/.venv/bin:$PATH \
    STATE_DIR=/state
VOLUME ["/state"]
ENTRYPOINT ["python", "-m", "approved"]
CMD ["worker"]
