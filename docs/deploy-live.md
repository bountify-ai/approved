# Live deploy: the judge on Maritime

Step by step, for putting the Approved judge (and its console) beside an existing approval.md
daemon on Maritime. The judge only reads the daemon's log with the tenant credential, so
deploying, restarting or removing it never changes what the gate decides.

Placeholders: `<judge-id>` is the judge machine's agent id, `<daemon-url>` the daemon's public
URL (`https://api.maritime.sh/a/<daemon-id>`).

## 0. Before you start

- The Maritime GitHub App must be able to read `bountify-ai/approved` (a private repository).
  Without it the build fails with `Deploy failed: Docker BuildKit CLI build failed with exit
  code 1` right after `Build started from https://github.com/bountify-ai/approved` (seen on
  2026-09-29). Grant the app access, then redeploy.
- You need the daemon's **tenant** credential (the value the daemon holds as
  `APPROVAL_SERVE_TENANT_TOKEN`). Never the agent credential and never the approval bot's
  token: the judge refuses to start with either in its environment.

## 1. Create the machine (non-secret env only)

```sh
printf '\n' | maritime --json create approved-judge \
  --repo https://github.com/bountify-ai/approved --branch main --public --port 18789 \
  -e FACADE_URL=<daemon-url> \
  -e WANDB_PROJECT=bountify/judgy \
  -e REVIEWER_MODEL=<W&B Inference model id> \
  -e CONSOLE_ENABLED=1 \
  -e TRUSTED_PROXY_HOPS=1
```

The empty line answers the template prompt ("press Enter to skip"), which gives a custom
framework built from the root `Dockerfile`. Note the `id` in the output, then:

```sh
maritime --json env set approved-judge PUBLIC_BASE_PATH=/a/<judge-id> --no-secret
```

`approved provision <tenant> --judge ...` does all of this, and the steps below, for you.

## 2. Make the judge's own bot

The judge posts through a **second** bot, never the approval bot.

1. In Telegram, message `@BotFather`, send `/newbot`, and pick a name such as
   "Acme approval judge". BotFather replies with the bot's token: save it to a file with mode
   0600 (for example `~/secrets/judge-bot`). Do not paste it anywhere else.
2. From the **approver's** Telegram account, open the new bot and send `/start`. A bot cannot
   message someone who has not started it.
3. The chat id is the approver's Telegram user id (the same chat the approval bot uses).

## 3. Import the secrets

Build the import file from your local secret files, so no value is typed or echoed:

```sh
umask 077
{
  printf 'TENANT_TOKEN=%s\n'       "$(cat ~/secrets/acme-tenant-token)"
  printf 'JUDGE_TG_BOT_TOKEN=%s\n' "$(cat ~/secrets/judge-bot)"
  printf 'CONSOLE_TOKEN=%s\n'      "$(openssl rand -hex 24)"
  printf 'WANDB_API_KEY=%s\n'      "$(cat ~/secrets/wandb-key)"
  # only if W&B Inference uses a different key than Weave:
  # printf 'INFERENCE_API_KEY=%s\n' "$(cat ~/secrets/inference-key)"
} > judge-secrets.env
maritime --json env import approved-judge judge-secrets.env
maritime --json env set approved-judge TG_CHAT_ID=<approver user id> --no-secret
```

Keep the console token (you sign in with it): `grep CONSOLE_TOKEN judge-secrets.env` on your
own machine only, then store the file somewhere safe or delete it.

(`TENANT_TOKEN_FILE` and `JUDGE_TG_BOT_TOKEN_FILE` name files inside the container; Maritime
cannot upload files, so on Maritime the values are imported as the plain variables above.)

## 4. Restart once

Env set after boot reaches the processes only after a restart. The judge holds no approval
log, so it can be stopped at any time:

```sh
maritime --json stop approved-judge && maritime --json start approved-judge
```

## 5. Checks

1. **Health.** `curl -s https://api.maritime.sh/a/<judge-id>/health` answers
   `{"status":"ok","worker":{"running":true,"chain":"verified"}}`. A `503` with
   `"status":"degraded"` names the reason (for example `chain-break`).
2. **Console.** `https://api.maritime.sh/a/<judge-id>/` redirects to `/a/<judge-id>/login`;
   sign in with the console token and the live view shows the follow as `ok`. Maritime's proxy
   drops the `Cookie` request header, so the console's script keeps the session in the tab's
   `sessionStorage` and sends it in the `X-Approved-Session` header instead. Consequences: the
   console needs JavaScript there (a plain form post answers "Session expired; try again."),
   each new tab signs in again, and page URLs carry a page name after `#` (for example
   `#/connect`), never a credential.
3. **A live advisory.** Trigger a gated request (for example
   `approved ask <tenant>-hermes "push the README fix to main"`; for a protected machine such as
   dogfood's, name it exactly: `approved ask --allow-target approval-hermes-gated
   approval-hermes-gated "..."`). The approver's chat gets the
   gate's prompt and, from the judge bot, a message starting "Judge (advisory AI, not an
   approval)" with a `Trace:` link. Weave is initialised when the worker starts; the review
   itself can still take tens of seconds with a reasoning model on W&B Inference (the first
   live call outlived the old 25 s default). The reviewer's deadline, `JUDGE_TIMEOUT_S`,
   defaults to 60 s; a call that outlives it sends no message and is shown as
   `silent: timeout` in the console. Raise it if a slow model is often silent, knowing that a
   review holds the judge's follow while it runs (see
   [RESILIENCE.md](../RESILIENCE.md#the-judge-reads-between-hook-calls)).
4. **A Weave trace.** The link opens an `approved.judge` call in
   <https://wandb.ai/bountify/judgy>. After the approver taps, the call gains
   `approved.human_decision` and `approved.agreement` feedback.

**Shared origin.** `api.maritime.sh/a/<id>` is shared with every public agent on Maritime, and
a page from any other agent there is same-origin with the console. It can use a live session
(it can read the one the console keeps in the tab's `sessionStorage` and send the session
header), and it can capture the console token itself as you type it, so signing out and the
12-hour expiry do not end its access. If you suspect that, rotate `CONSOLE_TOKEN` (import a
new one and restart the judge). Use the console there for demos only; in production put it
on its own domain or set `CONSOLE_ENABLED=0` (only `/health` is served). See
[SECURITY.md](../SECURITY.md#known-gaps).

**Keep the judge awake.** Maritime sleeps an idle machine after 900 seconds, and the judge's
outbound polling does not count as activity. A sleeping judge posts nothing. Use the always-on
add-on, or a platform cron trigger that hits `/health` every few minutes.

**Check the credential.** If `/health` stays at `"chain":"unknown"` for more than a few
seconds, the follow is failing. The usual cause is a `TENANT_TOKEN` the daemon does not
accept (HTTP 401 on `/log/follow`).

## 6. Switch the gated Hermes to W&B Inference (optional)

The gated Hermes image reads its model settings from these variables (approval-md-hosted
`hermes-image/README.md`, "Environment"):

```sh
maritime --json env set <tenant>-hermes --no-secret \
  APPROVAL_HERMES_MODEL=<W&B Inference model id> \
  APPROVAL_HERMES_PROVIDER=openai \
  APPROVAL_HERMES_BASE_URL=https://api.inference.wandb.ai/v1 \
  APPROVAL_HERMES_KEY_ENV=WANDB_API_KEY
printf 'WANDB_API_KEY=%s\n' "$(cat ~/secrets/hermes-wandb-key)" > hermes-model.env
maritime --json env import <tenant>-hermes hermes-model.env
maritime --json stop <tenant>-hermes && maritime --json start <tenant>-hermes
```

`APPROVAL_HERMES_PROVIDER=openai` selects the OpenAI-compatible client; the base URL points it
at W&B Inference. **Known gap:** Maritime writes a machine's environment, values included, to
world-readable files, so any tool the agent runs can read this key. Use a separate W&B key
for the agent machine, scoped and rotated as if the agent held it, because it effectively
does (see [SECURITY.md](../SECURITY.md#known-gaps)).

Stop and start the Hermes machine only while no approval request is open.

To read a protected daemon's status (for example dogfood's), name the machine exactly:
`approved status dogfood --allow-target approval-dogfood`. `--allow-target` exists only on the
read-only `ask` and `status`; `provision` refuses every protected name and has no such flag.

## Removing it

`maritime --json stop approved-judge` stops the judge; the gate keeps working unchanged.
