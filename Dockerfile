# syntax=docker/dockerfile:1
# The Approved judge + console, for Maritime's GitHub source route, which builds only a file
# named exactly `Dockerfile` at the repository root (approval-md-hosted doc 02 section 4.3).
# Same build as service/Dockerfile (kept in step by tests/test_dockerfiles.py), with paths
# from the repository root, `serve` as the command, and state under Maritime's /data volume.
# Maritime mounts /data at runtime with root ownership and runs containers as root, so this
# variant does not switch to an unprivileged user (the local demo image does).
# No credential is built in: `approved provision --judge` imports them as machine env.
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
COPY service/pyproject.toml service/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY service/src ./src
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH \
    STATE_DIR=/data/judge
# Absolute interpreter path: Maritime's init re-launches the image command
# with its own PATH, so a bare `python` resolved to the system interpreter,
# which has no `approved` module (seen 2026-09-29).
ENTRYPOINT ["/app/.venv/bin/python", "-m", "approved"]
CMD ["serve"]
