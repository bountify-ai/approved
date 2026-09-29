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
make demo       # the whole flow locally in Docker, offline (see demo/README.md)
                # console: http://127.0.0.1:8091/  chat: http://127.0.0.1:8090/
```

## Operator CLI

`approved` (installed with the service; `uv run approved ...` from `service/`) drives Maritime
through its `maritime` CLI. It shares one definition of every machine's environment with
the console's connect page, so the two cannot disagree.

```sh
approved provision acme --policy APPROVAL.md --approver carter --tg-chat 4242 \
    --tg-bot-token-file ~/secrets/acme-bot [--hermes --hermes-model gpt-5.4] [--judge]
approved connect acme --facade-url https://api.maritime.sh/a/<daemon-id>   # your own Hermes
approved ask acme-hermes "push the README fix"   # prints the reply; exit 3 = waiting for approval
approved status acme                             # machine, public /health, log verify head
```

- `provision` creates the public daemon (`--framework custom --public --port 18789`), then
  the optional gated Hermes and judge, reusing any machine that already exists. Credentials
  are generated into `./.approved/<tenant>/` (0700 dir, 0600 files) and reach a machine only
  through `maritime env import <agent> <file>`, never argv. It waits for `/health` 200 and an
  unauthenticated `/status` 401, writes the policy and verifies its sha256 remotely, then
  prints the attest command. **It never attests: a human does.**
- Refused outright: the dogfood tenant, `approval-hermes*` and `approval-x16-*` machines.
- `ask` runs detached inside the machine (the 120 s `exec` ceiling), polls, and on a timeout
  prints the `--job` id to collect the answer later.

License: MIT.
