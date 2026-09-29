# Vendored from https://github.com/bountify-ai/approval-md-hosted at commit 3d06a86eec04758645221db9c01183067cc6b00a
# (root Dockerfile). Behaviour is unchanged; the only edit is the entrypoint COPY path,
# because this build context is images/daemon/ rather than that repository root.
#
# syntax=docker/dockerfile:1
#
# The per-tenant approval.md daemon machine (HOSTED-9).
#
# One tenant, one machine, one store under /data. The image runs `approval up`
# (the daemon loop and the channels) and `approval serve` (the authenticated
# facade) under one supervisor, `image/entrypoint.mjs`, which owns the injected
# $PORT and answers the two endpoints Maritime's custom-code contract requires.
# In Telegram webhook mode (HOSTED-12) the supervisor runs `approval channel
# telegram webhook` as a third child and `approval up` without its poller.
# docs/02-hosted-daemon-architecture.md section 4.3 is the contract; section 5
# is the facade.
#
# THIS FILE LIVES AT THE REPOSITORY ROOT ON PURPOSE. Maritime's GitHub source
# route reads a file named exactly `Dockerfile` at the root of the repository
# and nowhere else, so the image's own parts stay under image/ and only this
# file sits here. `.dockerignore` keeps the build context to what the image
# needs.
#
# NO CREDENTIAL IS BUILT IN. The two serve credentials, the channel token and
# chat id (under the tenant-prefixed names the tenant policy declares), the
# webhook secret and the daemon id are read from the machine environment at
# runtime, by the children, and never appear in a build argument, a layer or an
# ENV default.

ARG NODE_IMAGE=node:22-bookworm-slim

# The approval.md runtime is pinned BY COMMIT rather than by an npm version: no
# published release carries `approval serve` yet (core APRV-421, merged
# 2026-09-21). Moving this pin is a deliberate act, and the build verifies that
# the commit it fetched is the commit named here. 6b74ca7 is core origin/main on
# 2026-09-23: it adds `serve --hook-harness-cap` (APRV-423), `approval channel
# telegram webhook` with the per-gate transport lease (APRV-424), and
# `approval up --no-telegram`, all of which the entrypoint uses.
ARG APPROVAL_MD_REPO=https://github.com/approval-md/approval.md.git
ARG APPROVAL_MD_COMMIT=6b74ca72541917511b77650702f702b079002957

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

FROM ${NODE_IMAGE}
ARG APPROVAL_MD_COMMIT

# ca-certificates is the contract's one named package: the daemon talks to the
# Telegram Bot API and to whatever adapters a tenant configures, over TLS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && update-ca-certificates

COPY --from=build /opt/runtime /opt/runtime
COPY entrypoint.mjs /opt/image/entrypoint.mjs

# The volume. Everything that has to survive sleep, restart and redeploy lives
# under here and nowhere else: the tenant's policy, log, queue, keys and
# channel state, at /data/<tenant>.
RUN mkdir -p /data
VOLUME ["/data"]
WORKDIR /data

ENV NODE_ENV=production \
    APPROVAL_CLI=/opt/runtime/node_modules/approval-md/cli.js \
    APPROVAL_DATA_DIR=/data \
    APPROVAL_MD_COMMIT=${APPROVAL_MD_COMMIT}

LABEL org.opencontainers.image.title="approval.md hosted daemon" \
      org.opencontainers.image.description="One tenant's approval.md daemon and authenticated facade, for Maritime's custom-code contract" \
      org.opencontainers.image.source="https://github.com/bountify-ai/approval-md-hosted" \
      md.approval.runtime.commit="${APPROVAL_MD_COMMIT}"

# A fast, side-effect-free liveness probe for `docker run` and `docker
# inspect`. Maritime uses GET /health over HTTP for the same purpose; this one
# asks the same endpoint so there is one answer and not two.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["node", "-e", "const p=process.env.PORT;if(!p){process.exit(1)}fetch('http://127.0.0.1:'+p+'/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]

# A REAL PROGRAM, in exec form. Maritime's init re-executes a shell-string
# command as one flattened string, so `CMD "node entrypoint.mjs"` is a machine
# that does not start. There is no shell in this process tree.
ENTRYPOINT ["node", "/opt/image/entrypoint.mjs"]
