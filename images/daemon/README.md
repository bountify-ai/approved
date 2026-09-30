# The approval.md daemon image (vendored)

Vendored from [bountify-ai/approval-md-hosted](https://github.com/bountify-ai/approval-md-hosted)
(a private repository, so links into it need access)
at commit `3d06a86eec04758645221db9c01183067cc6b00a`:

| here | source | change |
|---|---|---|
| `Dockerfile` | root `Dockerfile` | provenance header added; `COPY image/entrypoint.mjs` became `COPY entrypoint.mjs` because this build context is `images/daemon/` |
| `entrypoint.mjs` | `image/entrypoint.mjs` | none: byte-identical (sha256 `1cb433341f000fbc14e12601a860d631c38a5c05980ec9871d16e60a88745c79`) |
| `.dockerignore` | root `.dockerignore` | the same rule (context is the entrypoint only), re-pathed |
| this README | `image/README.md` | the sections below, excerpted unchanged; links re-pointed or removed |

The runtime inside is approval.md core, pinned by commit in the Dockerfile
(`APPROVAL_MD_COMMIT`) and fetched from the public repository at build time. To refresh
this copy, re-copy both files from a newer approval-md-hosted commit and update the commit
and hash above.

The Approved demo runs this image unchanged in Telegram webhook mode, against the fake Bot
API in `demo/fake-telegram/` (through the image's `APPROVAL_IMAGE_TG_API_BASE` seam). See
`demo/README.md`.

---

## Shape

```
              $PORT (injected)
                   |
        image/entrypoint.mjs            one process, the only listener
          |                    \
   GET /health, POST /chat,     everything else, reverse-proxied unchanged
   GET /schedules                      |
   (answered here)                     |
                              approval serve on 127.0.0.1:4682
                              /verbs /verb/<name> /hook/<harness>  (agent credential)
                              /log/follow /export /status          (tenant credential)

   approval up  ---- the daemon loop, TTL sweep, queue projection, channels
   store        ---- /data/<tenant>

   webhook mode only (APPROVAL_TG_WEBHOOK_SECRET set):
     POST /telegram/webhook  --->  approval channel telegram webhook on 127.0.0.1:4683
                                   (no credential needed or moved: Telegram's secret header)
     approval up             ---- runs with --no-telegram
```

`approval serve` authenticates every path before it routes: there is no
unauthenticated surface on it, not a health check and not a 404. So `/health`
and `/chat`, which Maritime calls without a credential, cannot come from it,
and neither can `/schedules`, which Maritime polls the same way. The supervisor
owns the port and answers those three itself (in webhook mode
`/telegram/webhook`, which Telegram calls without one, goes to the webhook verb;
see below). Everything else
is proxied byte for byte, with the `Authorization` header, the request body, the
status, the headers and the response bytes untouched: the credential split, the
refusal vocabulary and the harness dialects stay the core runtime's. This image
implements no part of the gate.

One exception, forced by the platform: Maritime's public-URL proxy strips
`Authorization` before a request reaches the machine (observed 2026-09-22 on the
first deploy; the same call on loopback succeeded). A client behind that proxy
sends the identical `Bearer <credential>` value in `X-Approval-Authorization`
instead, and the supervisor moves it to `Authorization` on the loopback hop.
The move happens only when no `Authorization` header arrived; if both arrive,
`Authorization` wins and the alternate is dropped, so nothing can smuggle a
second credential past one the proxy forwarded. `approval serve` still sees
exactly one `Authorization` header or none.

`approval serve` binds the LOOPBACK interface inside the container, not
`0.0.0.0`. Nothing outside the machine can reach it except through the
supervisor, and `--allow-non-loopback` (which exists to make an operator say out
loud that bearer credentials are crossing a cleartext hop) is not needed or
passed. The external hop is Maritime's to terminate.

## Build and run locally

```sh
docker build -t approval-daemon .                    # from the repository root
PLATFORM=linux/amd64 scripts/image-smoke.sh          # build and check everything

docker run --rm \
  -e PORT=18789 \
  -e APPROVAL_TENANT=acme \
  -e APPROVAL_AGENT=agent:hermes-acme \
  -e APPROVAL_DAEMON_ID=daemon-acme-1 \
  -e APPROVAL_SERVE_AGENT_TOKEN="$(openssl rand -hex 16)" \
  -e APPROVAL_SERVE_TENANT_TOKEN="$(openssl rand -hex 16)" \
  -v acme-data:/data -p 127.0.0.1:18789:18789 \
  approval-daemon
```

Then:

```sh
curl localhost:18789/health
curl -X POST localhost:18789/chat -d '{}'
curl localhost:18789/schedules
curl -H "Authorization: Bearer $TENANT" localhost:18789/status
curl -H "Authorization: Bearer $AGENT"  -X POST localhost:18789/hook/hermes -d @envelope.json
# through Maritime's public URL, which drops Authorization:
curl -H "X-Approval-Authorization: Bearer $TENANT" https://api.maritime.sh/a/<agent-id>/status
```

Build for the architecture the machine runs, not the one you are typing on:
`docker build --platform linux/amd64 .` when the host is an Apple Silicon Mac.

## Environment

Everything comes from the machine environment. Nothing is baked into a layer,
nothing is read out of the store, and this image never prints, forwards or
defaults a credential.

| variable | required | what it is |
|---|---|---|
| `PORT` | yes | Injected by the platform. There is no fallback: a compiled-in port is a port that is right until the platform changes it. |
| `APPROVAL_TENANT` (or `TENANT_ID`) | yes | The tenant whose store this machine runs. The store is `/data/<tenant>`. Absent, the container refuses: a machine that guessed would be writing one tenant's decisions into another tenant's log. |
| `APPROVAL_AGENT` (or `APPROVAL_SERVE_AS`) | yes | `agent:<id>`, the identity the facade acts as and records every call under. The image will not name it for you. |
| `APPROVAL_SERVE_AGENT_TOKEN` | yes | The credential the sandboxed harness holds: the verbs and the hook. At least 24 characters, distinct from the tenant credential. Read by `approval serve`, never by this image. |
| `APPROVAL_SERVE_TENANT_TOKEN` | yes | The credential the tenant's own tools hold: `/log/follow`, `/export`, `/status`. Never given to the agent. |
| `APPROVAL_DAEMON_ID` | recommended | The id every record this machine writes carries (core APRV-383). Unset, the runtime derives `daemon-<8 hex>` from the store path, which is stable but not a name a person chose. |
| `APPROVAL_HUMAN` | per tenant | `human:<id>`, the approver decisions are recorded against. Required by the Telegram channel in either mode. |
| `HOSTED_<TENANT>_TG_BOT_TOKEN`, `HOSTED_<TENANT>_TG_CHAT` | per tenant | The channel's bot token and approver chat id, under TENANT-PREFIXED names that the tenant's `APPROVAL.md` declares (see below). A channel whose credential is unset is skipped, with a line saying so; the gate still runs and still gates. |
| `APPROVAL_SERVE_HOOK_HARNESS_CAP` | **300s for a Hermes tenant** | Passed to `serve --hook-harness-cap` (core APRV-423). Without it the runtime assumes Hermes's 30-second default callback timeout and denies every manual class `hook-harness-cap-too-short`, because no human could answer in time. With `300s` a manual class gets a 240-second window (the cap minus the runtime's 60-second margin, or the policy's `approval_ttl` if that is shorter). The image does not default it: a cap is a statement about the harness on the other end, and only the tenant's configuration knows it. |
| `APPROVAL_SERVE_HOOK_TIMEOUT` | optional | Passed to `serve --hook-timeout`: how long one hook call waits for a decision before it blocks and the harness retries. Keep it well under the Hermes shim's 25-second ceiling (`APPROVAL_HOOK_MAX_TIME`), itself under Maritime's roughly 30-second public-URL proxy cut (doc 02 section 4.3): **12s recommended**. A 20-second wait plus proxy and classification overhead ran past the shim's ceiling on the live pair (2026-09-25). The shim re-asks after a timed-out first post (see the shim row above), but a wait that fits is the rule. |
| `APPROVAL_TICK_CRON` | optional | The external tick's five-field cron, in UTC, served on `GET /schedules` for the platform to wake the machine on (see [The external tick](https://github.com/bountify-ai/approval-md-hosted/blob/3d06a86eec04758645221db9c01183067cc6b00a/image/README.md#the-external-tick)). Unset or empty: `*/15 * * * *`. The literal `off`: no schedule. Anything else must be five fields of digits and `* , / -` inside cron's ranges; a bad value refuses at startup, exit 2, naming the variable and not repeating the value. |
| `APPROVAL_TG_WEBHOOK_SECRET` | webhook mode | Telegram's `secret_token`, 24 to 256 characters of `A-Z a-z 0-9 _ -` (`openssl rand -hex 32`). Setting it turns webhook mode on. Read by the webhook verb; the image only tests whether it is set. |
| `APPROVAL_PUBLIC_URL` | webhook mode | This machine's public https base URL, set at provisioning. On Maritime: `https://api.maritime.sh/a/<agent-id>`. Not a secret. |
| `TZ` | optional | The machine's timezone. |
| `APPROVAL_IMAGE_TG_API_BASE` | tests only | Passed as `--api-base` to whichever process owns Telegram, so the smoke can point the Bot API at a fake. Leave it unset on a real machine. |
| `APPROVAL_SERVE_INTERNAL_PORT` | optional | Where `approval serve` listens inside the container (default 4682, or 4683 when `PORT` is 4682). |
| `APPROVAL_DATA_DIR` | optional | Default `/data`. |

### Channel credentials: tenant-prefixed names, declared in the policy

The bot token and the chat id are NEVER set under the core defaults
(`APPROVAL_TG_TOKEN`, `APPROVAL_TG_CHAT`). Shared fixed names have collided
before: a value exported for one gate was read by another. Each tenant's values
live under names that carry the tenant, and the tenant's own policy says which
names those are, because that is where the runtime looks
(`channels.telegram.token_env` and `chat_id_env`; the defaults apply only when
the policy declares nothing):

```yaml
# /data/dogfood/APPROVAL.md, inside the approval-policy block
channels:
  telegram:
    token_env: HOSTED_DOGFOOD_TG_BOT_TOKEN
    chat_id_env: HOSTED_DOGFOOD_TG_CHAT
```

and the machine environment carries `HOSTED_DOGFOOD_TG_BOT_TOKEN` and
`HOSTED_DOGFOOD_TG_CHAT`. The image never reads either name: `approval up` and
the webhook verb read the policy and look up whatever it names, and a channel
whose variables are unset reports itself as skipped. The two serve credentials
and `APPROVAL_DAEMON_ID` keep their fixed names, because each machine holds one
tenant and those names are read by `approval serve` and `approval up` only.

**`APPROVAL_TG_WEBHOOK_SECRET` is the one fixed channel name, and it is an
exception on purpose.** Core APRV-424 reads the webhook secret by that
conventional name: declaring a name for it in the policy would widen the
validated `channels.telegram` schema, which is its own task (the core follow-up
for a `secret_env` key is filed). On a one-tenant machine the fixed name cannot
collide with another tenant's; it moves to a tenant-prefixed name once the
policy can declare one.

The smoke script's webhook tenant is seeded with a policy declaring
`HOSTED_SMOKE_TG_BOT_TOKEN` and `HOSTED_SMOKE_TG_CHAT`, and its container sets
only those, so the webhook starting at all is the proof that the image honours
the policy's names.

## Telegram webhook mode

Telegram allows one receiver per bot: a long poll (`approval up`'s channel) or a
registered webhook, never both. A machine that sleeps cannot hold a long poll
open across the snapshot, so a sleeping tenant uses webhook mode (doc 02
section 4.3 and section 7).

With `APPROVAL_TG_WEBHOOK_SECRET` and `APPROVAL_PUBLIC_URL` set, the supervisor
runs three children:

- `approval channel telegram webhook --dir /data/<tenant> --url
  <APPROVAL_PUBLIC_URL>/telegram/webhook --path <that url's path> --listen
  127.0.0.1:4683`. It registers the URL with `setWebhook`, serves the callback on
  loopback, and runs the dispatch cycle that sends pending requests to the
  approver. Port 4683 moves to 4684 only when `PORT` or `serve`'s internal port
  already holds it.
- `approval up --no-telegram`: the daemon loop, with its poller off. The gate's
  transport lease (`.approval/daemon/telegram-transport.lock`) would refuse a
  second taker anyway; the flag means `up` never asks.
- `approval serve`, unchanged.

`POST /telegram/webhook` on `$PORT` is proxied to the webhook verb with the
request body byte for byte and every header as it arrived (hop-by-hop headers
dropped and `Host` set to the loopback address, as on every proxied path). No credential is
required and none is moved: the `X-Approval-Authorization` rewrite belongs to
the facade's bearer credentials and does not apply to this path. Telegram's
`X-Telegram-Bot-Api-Secret-Token` is the authentication, and the verb checks it
before the path, the method or the body; a post without it is refused
`401 webhook-secret-mismatch` and appends nothing. Every other path keeps
today's behaviour.

**One path rewrite, forced by the platform.** Maritime's public-URL proxy
removes the `/a/<agent-id>` prefix before a request reaches the machine, so
Telegram's post to `https://api.maritime.sh/a/<agent-id>/telegram/webhook`
arrives as `/telegram/webhook`. The verb serves the registered URL's own path and
refuses a `--path` that differs (core APRV-424 review, finding 3: "rewriting
belongs in the proxy"), so the supervisor asks it on
`/a/<agent-id>/telegram/webhook`. With a public URL that has no path of its own
the two are the same string.

`/health` gains `"webhook":"running"` or `"down"` in this mode, and a down
webhook child makes it 503, as a down daemon or facade does: a machine whose
approver cannot be reached is not healthy.

A secret set without `APPROVAL_PUBLIC_URL` refuses at startup, exit 2: the
operator asked for webhook mode, and neither guessing a URL nor falling back to
long polling is what they asked for. HTTPS, the port (443, 80, 88 or 8443) and
userinfo in the URL are the verb's to refuse, in its own vocabulary.

A clean stop (`docker stop`, a Maritime redeploy's SIGTERM) removes the
registration. After an unclean kill the verb re-registers the same URL on its
own only when the lease it reclaims belonged to a runner that is gone; in a
fresh container the reused pid number can instead read as a live holder, and
the verb then refuses until an operator runs it once with `--reclaim` (core
cli-reference, "One transport per bot").

## Health, and what the fields mean

`GET /health` reads process state only: no store read, no log read, nothing
appended. A health check with a side effect is a side effect on a schedule.

```json
{"status":"ok","daemon":"running","facade":"listening","uptime_s":4}
```

- `200` when both children are alive and the facade has bound, and for a short
  grace window after the facade is spawned, so a booting machine answers the
  contract's fast 2xx rather than a 503 it will contradict in half a second.
- `facade: "starting"` is that window. A facade request arriving inside it gets
  the fail-closed refusal below rather than a queued connection, so a client
  that needs the facade should wait for `"listening"`.
- `503` when either child is down, or when the facade never bound.

`POST /chat` answers a fixed refusal: this machine is a gate, not an assistant.
The body names nothing about the tenant, because the endpoint carries no
credential and so may disclose nothing. The platform's scheduled delivery
(`"source": "scheduled"`) gets a fixed acknowledgement instead; see
[The external tick](https://github.com/bountify-ai/approval-md-hosted/blob/3d06a86eec04758645221db9c01183067cc6b00a/image/README.md#the-external-tick).

**A non-200 from a `/hook/<harness>` path is a BLOCK.** When `approval serve` is
unreachable the proxy answers 503 with `exit_code: 2`, an empty `stdout` and
`error.code: image-facade-unavailable`. It does not invent a harness dialect: a
second implementation of the gate's vocabulary would be a second gate. A hook
client must therefore treat any non-200 as a refusal, which is the same rule the
harness adapters already follow for a hook that fails.

