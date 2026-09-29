# Approved demo

The whole flow on your machine, with no accounts, no secrets and no network beyond the Docker
image builds: an AI agent asks to run commands, the approval.md gate turns the risky ones into
approval requests, you approve or reject them in a fake Telegram chat, and the AI judge posts
its advisory next to each prompt.

```sh
make demo        # build, start, print the URLs, then run the scripted agent
make demo-down   # remove the demo's containers, network and volumes
make demo-smoke  # the same flow headless, with assertions (what CI runs)
```

Requires Docker with Compose v2, `openssl`, and `python3` (for `demo-smoke`). The first
build fetches and compiles the approval.md runtime and takes a few minutes; later runs start
in about 10 seconds.

## What runs

| service | what it is |
|---|---|
| `daemon` | The hosted approval.md daemon image, vendored unchanged in [`images/daemon/`](../images/daemon/): `approval up`, the authenticated facade `approval serve`, and the Telegram channel in **webhook mode**. |
| `fake-telegram` | [`fake-telegram/server.mjs`](fake-telegram/server.mjs): a fake Bot API with a chat page at <http://127.0.0.1:8090/>. The Approve and Reject buttons deliver the tap to the daemon's webhook as the demo approver (Telegram account 4242), with the secret the daemon registered, as Telegram would. |
| `service` | The Approved judge (`python -m approved serve`): the worker, which follows the log with the TENANT credential and posts advisories through its own bot into the same chat, plus the **operator console** at <http://127.0.0.1:8091/> in the same process. |
| `agent` | [`agent/agent.py`](agent/agent.py), a one-shot scripted agent (compose profile `agent`). It posts Hermes-shaped hook envelopes to `/hook/hermes` with the AGENT credential and re-asks while the answer is `hook-timeout`, as the Hermes hook shim does. |
| `init` | The demo init step (see below). |

The agent runs four scenarios under [`policy/APPROVAL.md`](policy/APPROVAL.md):

| scenario | class | policy | offline judge |
|---|---|---|---|
| `cat README.md` | `read.shell` | autonomous: runs, no prompt | none (nothing to review) |
| `git push origin main` | `vcs.push.main` | manual | NEEDS_HUMAN |
| `git push origin feat/checkout-retry` | `vcs.push.branch` | manual | READY |
| `git push --force origin main` | `vcs.history.rewrite` | manual | NEEDS_HUMAN |

The prompts arrive one at a time: the gate shows the next request after you answer the one
in front of you.

## The operator console

<http://127.0.0.1:8091/> is the demo's control room:

- `/`: the live tenant view: follow and chain health, open requests with the judge's verdict,
  recent decisions labelled agree / disagree / escalated, and the running agreement,
  false-READY and escalation counters. It reads the judge's state, never the facade.
- `/policy`: the policy builder, with a "What the judge would say" panel (offline reviewer
  only, so the public page can never spend a model call).
- `/connect`: the connect bundle for a real tenant on Maritime (daemon plus gated Hermes, or
  your own Hermes). It writes placeholders and a local credential generator, never a value.

Sign in with the token in `demo/.state/console_token` (`cat` it; the console never displays
it). `/policy` and `/health` are public.

## Credentials

`make demo` generates fresh random credentials into `demo/.state/` (mode 0600, gitignored)
on every run: the two serve credentials, the webhook secret, two fake bot tokens and the
console token. Each
container gets only its own: the daemon holds both serve credentials, the judge holds the
tenant credential and its bot token, and the agent holds the agent credential. None of them
is a real credential, and only the fake Telegram ever sees a bot token.

## The demo init step attests the policy for you

`init` seeds the store and runs `approval policy attest --as human:demo`, so the gate works
unattended. **In production a human attests**: the approver reads the policy and attests it
as themselves, and every gate verb refuses until they have. The demo does it for you only so
the stack can come up with one command.

## Live judge (optional)

Offline, the judge uses the deterministic rules-based reviewer. With `WANDB_API_KEY` and
`REVIEWER_MODEL` set in your shell (plus `WANDB_ENTITY` and `WANDB_PROJECT` if not
`bountify/judgy`), `make demo` starts it live: W&B Inference reviews each request, the
advisory carries a Weave trace link, and your decisions are attached to the call as Weave
feedback. Those values pass from your shell to the container and are never written to a file.

## Inspecting

```sh
docker compose -p approved-demo logs -f service     # the judge's structured logs
curl -s http://127.0.0.1:8090/api/chat               # the chat, as JSON
KEEP=1 make demo-smoke                               # leave the smoke's stack running
```
