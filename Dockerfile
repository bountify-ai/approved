# syntax=docker/dockerfile:1
#
# Approved, try it: the whole `make demo` in ONE container for one public URL (docs/tryit.md).
#
# Build context is the REPOSITORY ROOT:
#   docker build -f images/tryit/Dockerfile -t approved-tryit:local .
# BuildKit reads images/tryit/Dockerfile.dockerignore for this file. Maritime's GitHub route
# builds only a root `Dockerfile`, so the deploy branch made by scripts/tryit-deploy-branch.sh
# carries this file, byte for byte, as its root Dockerfile (and the ignore file as its root
# .dockerignore); every path below is written from the repository root for that reason.
#
# One supervisor (`python -m approved.tryit`) runs four parts, all but the front on loopback:
#   the approval.md daemon   images/daemon/entrypoint.mjs, unchanged, built as images/daemon builds it
#   the fake Telegram        demo/fake-telegram/server.mjs, unchanged
#   the judge                python -m approved serve, console disabled
#   the front                the only listener on 0.0.0.0:$PORT
# and runs demo/agent/agent.py on demand. NO CREDENTIAL IS BUILT IN: the demo's throwaway
# credentials are generated at container start into /data/tryit/secrets (0600).

ARG NODE_IMAGE=node:22-bookworm-slim

# The approval.md runtime, pinned by commit exactly as images/daemon/Dockerfile pins it. The
# build stage below is that file's build stage, verbatim (service/tests/test_tryit_image.py
# keeps the two in step), so this image runs the same gate the demo's daemon image runs.
ARG APPROVAL_MD_REPO=https://github.com/approval-md/approval.md.git
ARG APPROVAL_MD_COMMIT=6b74ca72541917511b77650702f702b079002957

FROM ghcr.io/astral-sh/uv:0.11.26 AS uv

FROM ${NODE_IMAGE} AS build
ARG APPROVAL_MD_REPO
ARG APPROVAL_MD_COMMIT
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git python3 make g++ \
 && rm -rf /var/lib/apt/lists/*

# A one-commit fetch, by sha, verified after checkout. No branch is consulted,
# so a force-push to main cannot change what this image contains.
WORKDIR /src
RUN git init -q . \
 && git remote add origin "${APPROVAL_MD_REPO}" \
 && git fetch -q --depth 1 origin "${APPROVAL_MD_COMMIT}" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${APPROVAL_MD_COMMIT}"

RUN npm ci --no-audit --no-fund
RUN npm run build

# `npm pack` selects exactly the files the package publishes (cli.js, dist/src,
# schema, SPEC.md, the docs the CLI points at), so the runtime tree is the one
# an npm release would install, minus the release. Installing the tarball here
# resolves the runtime dependencies and builds better-sqlite3 against this same
# base image, which is what the runtime stage copies.
RUN npm pack --pack-destination /tmp >/dev/null
RUN mkdir -p /opt/runtime \
 && cd /opt/runtime \
 && npm init -y >/dev/null \
 && npm install --omit=dev --no-audit --no-fund /tmp/approval-md-*.tgz \
 && node /opt/runtime/node_modules/approval-md/cli.js --version

# The Node binary the runtime was built against (same base image, so better-sqlite3's ABI
# matches). Copied into the Python image below rather than installed a second way.
FROM ${NODE_IMAGE} AS node

# The judge's base, as in the root Dockerfile (Debian 13, whose glibc runs the Debian 12
# Node binary and addon: forward compatible).
FROM python:3.11-slim
ARG APPROVAL_MD_COMMIT
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

# The gate: the runtime, the Node binary it was built for, and the daemon image's own
# supervisor, unchanged. loopback.mjs keeps that supervisor and the fake Telegram off
# routable addresses without editing either file.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=build /opt/runtime /opt/runtime
COPY images/daemon/entrypoint.mjs /opt/image/entrypoint.mjs
COPY images/tryit/loopback.mjs /opt/tryit/loopback.mjs

# The demo, unchanged: its init step (which expects the policy at /demo/APPROVAL.md), the
# policy, the fake Telegram and the scripted agent.
COPY demo/init.sh /demo/init.sh
COPY demo/policy/APPROVAL.md /demo/APPROVAL.md
COPY demo/fake-telegram/server.mjs /demo/fake-telegram/server.mjs
COPY demo/agent/agent.py /demo/agent/agent.py

RUN /usr/local/bin/node /opt/runtime/node_modules/approval-md/cli.js --version \
 && mkdir -p /data

ENV NODE_ENV=production \
    APPROVAL_CLI=/opt/runtime/node_modules/approval-md/cli.js \
    APPROVAL_DATA_DIR=/data \
    APPROVAL_MD_COMMIT=${APPROVAL_MD_COMMIT}

LABEL org.opencontainers.image.title="Approved, try it" \
      org.opencontainers.image.description="The Approved demo (approval.md gate, advisory AI judge, fake Telegram, scripted agent) behind one public port" \
      md.approval.runtime.commit="${APPROVAL_MD_COMMIT}"

HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=3 \
  CMD ["/usr/local/bin/python3", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ['PORT'], timeout=4).status == 200 else 1)"]

# A real program in exec form, with an ABSOLUTE interpreter: Maritime's init re-launches
# the image command with its own PATH, and a bare `python` resolved to the system
# interpreter (2026-09-29). Every child the supervisor starts is addressed the same way.
ENTRYPOINT ["/app/.venv/bin/python", "-m", "approved.tryit"]
