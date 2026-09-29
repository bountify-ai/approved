# Approved

Hosted [approval.md](https://github.com/bountify-ai/approval-md) with an AI judge in the loop.
approval.md turns an agent's risky tool call (a `git push`, a payment, a delete) into an
`approval.requested` record in a hash-chained, append-only log, and a human answers it on
Telegram. Approved runs that gate for you and adds a second opinion: a reviewer model, traced
in Weights & Biases Weave, reads each pending request and sends the approver a one-line
advisory verdict (READY, REVISE, NEEDS_HUMAN or ABORT) with a link to its trace. The judge only
reads the log and never decides anything: the human's tap is still the only thing that grants,
and the human's eventual decision is fed back to Weave as feedback on the judge's call.

The full README (architecture, setup, demo) lands in a later unit.

## Quick start

```sh
make setup      # uv sync in service/
make test       # pytest (network sockets disabled)
make lint       # ruff check + ruff format --check
make typecheck  # pyright
```

License: MIT.
