# CLAUDE.md: house rules for Approved

Approved is hosted approval.md plus an advisory AI judge. These rules bind every agent and
every human working in this repo.

## Never

- Read, print, copy, log or commit a secret value. Credentials reach processes through the
  environment or a `*_FILE` path; code reads them, people and agents do not. `.env`,
  `private/` and `state/` are gitignored and stay that way.
- Give the judge the AGENT credential, or any route that writes to the approval log. The judge
  reads `GET /log/follow` with the TENANT credential and nothing else.
- Let the judge decide anything. Its verdict is advisory text for a human. A judge failure
  (timeout, parse error, inference error, open circuit) means NO message, never a blocked or
  delayed approval.
- Mutate, reorder or fabricate approval-log records. The log is the truth; the judge's state
  file is a cache of what it has already done.
- Use `git stash`.

## Always

- Tests before a PR: `make lint typecheck test` green locally, and CI green on the PR.
- Stage files by explicit filename (`git add path/to/file`), never `git add -A` or `.`.
- Treat model output as untrusted text: one line, capped, HTML-escaped before it reaches a
  channel.
- Keep the core deterministic: follow paging, chain continuity, idempotency and the circuit
  breaker are plain code with exhaustive tests. The model is confined to the verdict text.

## Layout

- `service/`: the Python package `approved` (uv project, Python 3.11).
- `.github/workflows/ci.yml`: ruff, pyright and pytest on every push and PR.
