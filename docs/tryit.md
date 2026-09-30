# Try it: the whole demo behind one public URL

`make demo` needs Docker and a terminal. The try-it image is the same demo packaged as ONE
container, so a reviewer with no accounts opens one URL, presses **Run the agent**, watches the
gate hold the agent's pushes, reads the AI judge's advisory beside each prompt, taps Approve or
Reject, and sees the outcomes and the recorded, chain-verified decisions. It is built to run on
a single Maritime machine and to be embedded in <https://approval.md/approved/>.

Nothing in it is new gate logic. It runs the demo's own parts, unchanged:

| part | what runs | listens on |
|---|---|---|
| approval.md daemon | the daemon image's own supervisor, [`images/daemon/entrypoint.mjs`](../images/daemon/entrypoint.mjs), built exactly as [`images/daemon/Dockerfile`](../images/daemon/Dockerfile) builds it (same pinned approval.md commit), with the environment [`demo/compose.yaml`](../demo/compose.yaml) gives the `daemon` service | 127.0.0.1:8080 (and serve/webhook on 4682/4683) |
| fake Telegram | [`demo/fake-telegram/server.mjs`](../demo/fake-telegram/server.mjs) | 127.0.0.1:8081 |
| AI judge | `python -m approved serve` with the console **off** (`CONSOLE_ENABLED=0`), holding the tenant credential and its own fake bot token only | 127.0.0.1:8000 (`/health` only) |
| scripted agent | [`demo/agent/agent.py`](../demo/agent/agent.py), started per run, one scenario per invocation | nothing |
| front | `python -m approved.tryit` ([`service/src/approved/tryit/`](../service/src/approved/tryit/)), which also supervises the rest | **0.0.0.0:$PORT** |

The two Node programs bind `0.0.0.0` in their own code; [`images/tryit/loopback.mjs`](../images/tryit/loopback.mjs),
loaded with `node --import`, moves their listen to 127.0.0.1 without editing either file. The
smoke reads `/proc/net/tcp` and fails if anything but the front listens off loopback.

## Run it locally

```sh
make tryit         # build and run at http://127.0.0.1:18789/ (TRYIT_PORT=... to change)
make tryit-smoke   # the image, headless, asserted through its public port only
make tryit-down    # remove exactly the container and volume make tryit created
```

`make tryit-smoke` ([`images/tryit/smoke.py`](../images/tryit/smoke.py)) runs two phases, about
four minutes in all, with no network beyond the image build:

1. Live reviewer configured but unreachable (every W&B and inference URL points at a dead
   loopback port), live cap of one run, two runs an hour, and a PATH without `/usr/local/bin`
   or the venv (as Maritime's init gives it). It checks `/health`, the listeners, the embed
   headers, that no response sets a cookie, 21 forbidden paths, a refused concurrent run, run 1
   with the judge **absent** while the taps still decide (reject, approve, reject), run 2 falling
   back to the **offline** reviewer because the cap is spent (the page says so), the rate limit,
   the chain and `approval log verify`, Reset leaving `events.jsonl` untouched, and that no
   generated credential (nor the W&B key) appears in any response, the container log, or any
   process environment other than the supervisor's and the judge's.
2. After `docker kill -s KILL`, a cold start on the same `/data` with a 30-second approval
   window: healthy again, the log continues and verifies, and a run nobody taps ends by the
   gate's own expiry and is shown as expired.

CI runs it as the `tryit-smoke` job, beside `demo-smoke` and in the same shape: no secret, the
image built with a GitHub Actions layer cache (its runtime stage is the daemon image's, so the
`daemon` cache scope is read too), then `SKIP_BUILD=1 bash images/tryit/smoke.sh`.

## Deploy it on Maritime

Maritime's GitHub route builds a file named exactly `Dockerfile` at the root of the branch it
is given, and on `main` that file is the judge image. The try-it image is deployed from a
**separate branch, `deploy/tryit`, whose root `Dockerfile` is `images/tryit/Dockerfile` byte for
byte** (and whose `.dockerignore` is `images/tryit/Dockerfile.dockerignore`). This needs no new
account: it is the same repository, and the Maritime GitHub App already reads it. The other
routes were weighed and dropped: Maritime's CLI exposes no build argument or Dockerfile path
(so one root Dockerfile cannot serve both images), and a registry image needs a registry
account and push credentials that nothing else here uses.

`scripts/tryit-deploy-branch.sh` makes that branch with git plumbing on a temporary index, so
the working tree and the current branch are never touched, and each regeneration is a
fast-forward. It never pushes.

```sh
# 1. Build the deploy branch from main, then push it (a human step).
git switch main && git pull --ff-only
make tryit-deploy-branch                # prints: git push origin deploy/tryit
git push origin deploy/tryit

# 2. Create the machine: public, port 18789, custom framework from the root Dockerfile.
printf '\n' | maritime --json create approved-tryit \
  --repo https://github.com/bountify-ai/approved --branch deploy/tryit --public --port 18789
```

Note the `id` in the output. The page works at `https://api.maritime.sh/a/<id>/` with no more
configuration: every URL it uses is relative. Setting
`PUBLIC_BASE_PATH=/a/<id>` (`maritime --json env set approved-tryit PUBLIC_BASE_PATH=/a/<id>
--no-secret`) also makes it work when someone drops the trailing slash.

**Live reviewer (the deployed mode).** Offline is the default. For W&B Inference with Weave
traces in the project:

```sh
maritime --json env set approved-tryit --no-secret \
  TRYIT_LIVE=1 REVIEWER_MODEL=<W&B Inference model id> WANDB_PROJECT=bountify/judgy \
  TRYIT_LIVE_DAILY_CAP=300
umask 077; printf 'WANDB_API_KEY=%s\n' "$(cat ~/secrets/tryit-wandb-key)" > tryit.env
maritime --json env import approved-tryit tryit.env && rm tryit.env
maritime --json stop approved-tryit && maritime --json start approved-tryit
```

Use a W&B key made for this machine only: Maritime writes machine environment, values included,
to world-readable files on the box ([SECURITY.md](../SECURITY.md#known-gaps)). Inside the
container the supervisor passes the key to the judge process only; the scripted agent, the
daemon and the fake Telegram never get it in their environment.

**Checks.** `curl -s https://api.maritime.sh/a/<id>/health` answers
`{"status": "ok", "parts": {"daemon": true, "fake_telegram": true, "judge": true, "front": true}}`;
open `https://api.maritime.sh/a/<id>/` and press Run the agent.

**Refreshing.** Regenerate and push the branch from the new `main`, then redeploy the machine
the way Maritime redeploys a GitHub-sourced agent. `/data` keeps the log, the judge's state and
the live budget across redeploys, restarts and sleep.

### Embedding it in approval.md

```html
<iframe src="https://api.maritime.sh/a/<id>/" title="Approved: live demo"
        width="100%" height="960" loading="lazy" referrerpolicy="no-referrer"
        sandbox="allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox"
        style="border:0;max-width:1000px"></iframe>
```

- `allow-scripts allow-same-origin`: the page polls its own origin with `fetch`; without
  `allow-same-origin` its origin is opaque and every request is refused.
- `allow-popups allow-popups-to-escape-sandbox`: only so a Weave trace link (live mode) opens
  in a new tab as a normal page. The page never calls `window.open`.
- Deliberately absent: `allow-top-navigation*` (the page never navigates its parent),
  `allow-forms` (it has no form), and an `allow=` permissions list (it uses no device API).
- It works from about 360 px wide (one column) to 1000 px and wider (two columns), sets and
  needs no cookie, and polls; nothing waits on a held request.

The page (`/`) and the approver chat inside it (`/approver/`) are sent with
`Content-Security-Policy: ...; frame-ancestors 'self' https://approval.md` and no
`X-Frame-Options`; `TRYIT_FRAME_ANCESTORS` changes the allowed origins (https origins only).
Every other response carries `frame-ancestors 'none'` and `X-Frame-Options: DENY`.

## Configuration

| variable | default | what |
|---|---|---|
| `PORT` | required | injected by the platform; the front binds `0.0.0.0:$PORT` (not 4682-4685, 8000, 8080 or 8081, which the children hold) |
| `TRYIT_LIVE` | off | `1` with a W&B key and `REVIEWER_MODEL`: the live reviewer, traced to Weave |
| `WANDB_API_KEY` (or `_FILE`), `REVIEWER_MODEL`, `WANDB_ENTITY`, `WANDB_PROJECT`, `INFERENCE_API_KEY`, `INFERENCE_BASE_URL`, `JUDGE_TIMEOUT_S`, `WANDB_BASE_URL`, `WEAVE_DISABLED` | unset | passed to the judge only, and only in live mode |
| `TRYIT_LIVE_DAILY_CAP` | 300 | hard cap on live reviewer calls per UTC day, persisted in `/data/tryit/live-budget.json` |
| `TRYIT_LIVE_CALLS_PER_HOUR` | 60 | the same, per rolling hour |
| `TRYIT_RUN_MIN_INTERVAL_S` | 20 | minimum gap between run starts (global) |
| `TRYIT_RUNS_PER_HOUR` | 60 | runs per rolling hour (global) |
| `TRYIT_HOOK_HARNESS_CAP_S` | 300 | the demo's harness cap; the approval window is this minus 60 s (240 s) |
| `TRYIT_FRAME_ANCESTORS` | `https://approval.md` | origins allowed to frame the page, besides its own |
| `PUBLIC_BASE_PATH` | unset | `/a/<id>`: the page's base when a visitor drops the trailing slash |

## Public routes

| route | method | exposes |
|---|---|---|
| `/` | GET | the page (static HTML; inline script and style pinned by hash) |
| `/health` | GET | 200 only when the daemon (facade listening, webhook running), fake Telegram, judge and front are all up; which parts are up |
| `/api/state` | GET | the current or last run (its id, the four fixed commands, each outcome, the agent's printed lines), the reviewer mode and why, the judge's counters and last eight decisions from its state file, the judge's chain status, the cached `approval log verify` result, the run limits |
| `/api/run` | POST | starts one run; 409 while one runs, 429 when rate-limited, 503 while starting |
| `/api/reset` | POST | hides the chat and the last run from the page; 409 during a run; never touches the log |
| `/approver/` | GET | the fake Telegram's own chat page |
| `/approver/api/chat` | GET | the chat messages since the view was last cleared: id, text, buttons, edited |
| `/approver/api/tap` | POST | one tap, delivered only for a prompt the current run opened, once per prompt |
| `/chat` | POST | the platform contract's chat endpoint: a fixed refusal |

Everything else is 404; a listed path with another method is 405.

## Limits

- **One shared tenant.** Every visitor sees and can tap the same run. The page says so.
- **One run at a time**, and runs are rate-limited globally. The run limiter lives in memory, so a
  restart resets it; the live budget is on disk and does not.
- **Prompts are slow to appear.** The gate's Telegram webhook verb dispatches pending requests
  every 30 seconds (core's default; the vendored daemon entrypoint has no knob for it), so a
  prompt shows up 0 to 30 s after the agent asks, exactly as in `make demo`.
- **The judge may be absent.** It reads the log through the facade, which answers one call at a
  time and is busy while the agent's hook call waits (10 s), so an advisory usually lands within
  seconds, and a visitor may tap first. The page then shows "judge absent" and the tap decides
  as always.
- **An unanswered prompt ends the run** when the gate expires it: `approval serve` refuses the
  agent with `hook-expired` once the 240 s window lapses, and the daemon's sweep appends
  `approval.expired` to the log within the next 30 s. A visitor who walks away holds the run
  slot for about four minutes, and the next run's first prompt can wait for that sweep.
- **The live budget is charged up front**: four calls per live run (one per scenario, though the
  autonomous read is never reviewed), so the cap is never exceeded.
- **The chat lives in memory.** A cold start (or a fake Telegram restart) empties the chat view;
  the log, the judge's state and the counters persist under `/data`.

## Security model

The machine is public and unauthenticated, and holds only throwaway demo state.

- **No real credential.** The two serve credentials, the webhook secret and the two fake bot
  tokens are generated fresh at every container start into `/data/tryit/secrets` (0700, files
  0600) and reach each child the way compose gives them: the daemon in its environment, the
  judge and the agent through `*_FILE` paths. Each child's environment is built from an
  allowlist; none inherits the supervisor's. The judge still refuses to start with an agent
  credential in reach.
- **Fixed commands only.** The agent runs its own four `SCENARIOS`; the front reads that list
  from `agent.py` itself and passes only a scenario name and a generated run id. No visitor text
  reaches a process, a command line or the log.
- **No proxying.** The front asks the fake Telegram for exactly its page, its chat JSON and a
  tap, and filters each answer. No route reaches the facade, the log export, the tenant or agent
  credential, the webhook or the judge's console (which is off).
- **Taps.** Accepted only while a run is in progress, only for a message that arrived after the
  run started, carries the tapped button, and follows the gate's `APPROVAL REQUIRED` header
  naming this run's action keys; one tap per prompt. The gate still authenticates the tap
  itself (the webhook secret, the approver's mapped Telegram id), as in `make demo`.
- **The log is append-only.** Nothing in the image writes `events.jsonl`: the gate does, through
  its own verbs. Reset changes only what the page shows. A fresh store happens only on a fresh
  `/data`.
- **Browser.** No cookie, strict CSP with hashed inline code and `connect-src 'self'`, framing
  only by the configured origins, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
  Model text reaches the page only through the fake Telegram's tag allowlist renderer and
  `textContent`; trace links are shown only when they match `https://wandb.ai/.../r/call/...`.
- **Abuse.** Bodies are capped per route before they are read (256 B for run and reset, 1 KiB for
  a tap, 64 KiB for `/chat`, none on GET), chunked bodies are refused, POSTs must be
  `application/json` (so another origin needs a CORS preflight, which is never granted), at most
  64 requests are served at once and a slow client is cut after 15 s.
- **Shared origin.** On Maritime every public agent shares `https://api.maritime.sh`, so other
  agents' pages are same-origin with this one. Nothing here is protected by origin: there is no
  session and nothing to steal.

The tests are `service/tests/test_tryit.py` (for example
`test_tryit_front_exposes_only_its_own_routes`,
`test_tryit_taps_only_reach_prompts_the_current_run_opened`,
`test_tryit_live_budget_caps_per_day_and_hour_and_persists` and
`test_tryit_image_builds_the_daemon_images_runtime_verbatim`) and the image smoke above.

## Assumptions about Maritime (not verified by this repository)

- The proxy strips `/a/<id>` and forwards the rest of the path and the query unchanged, drops
  `Cookie` and `Authorization`, passes `Content-Type`, and cuts requests held past about 30 s.
  The page depends on none of the dropped headers and holds no request.
- `/data` persists across sleep, restart and redeploy; the rest of the filesystem may not.
- A resume from snapshot keeps processes as they were; a cold start runs the whole boot (about
  2 to 5 s locally, including after `docker kill -s KILL`, where core reclaims the Telegram
  transport lease itself because the recorded pid now belongs to a newer process). `/health` answers 503 while booting and during a reviewer switch (well under a
  second locally); Maritime tolerating that is assumed.
- `maritime create --branch` builds the named branch's root `Dockerfile` (the judge deploy uses
  `--branch main`).
