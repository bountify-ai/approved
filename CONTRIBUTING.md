# Contributing

## Setup and checks

```sh
make setup       # uv sync in service/
make check       # lint, typecheck, test
make docs-check  # relative links and test references in the Markdown
make demo-smoke  # the Docker demo end to end (needs Docker)
```

CI runs all of these on every pull request; a PR merges only when they pass.

## Rules

- **Never commit a secret.** Credentials reach processes through the environment or a
  `*_FILE` path. `.env`, `private/`, `state/`, `.approved/` and `demo/.state/` are gitignored.
- **Tests never touch the network.** pytest runs with `--disable-socket`. Use the fakes in
  `service/tests/fakes.py` (facade, Telegram) and `service/tests/fake_maritime.py`.
- **The judge is advisory.** It must not write to the log, hold the agent credential, or
  delay an approval. A change that weakens any invariant in [SECURITY.md](SECURITY.md) needs a
  test that shows what it gives up, and a reviewer who agrees.
- **Untrusted text stays untrusted.** Agent-supplied fields and model output are escaped at
  the point they are rendered.
- **Stage files by name** (`git add path/to/file`), write a commit message that says why, and
  keep one unit of work per pull request.
- **Docs are part of the change.** If behaviour moves, update the README, ARCHITECTURE,
  SECURITY or RESILIENCE section that describes it; `make docs-check` catches broken links
  and renamed tests.

## Prose

Plain and concrete: say what a thing does and what proves it. Few em dashes, no hype.
