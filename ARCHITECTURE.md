# Architecture

Approved runs the approval.md gate for a tenant and adds an advisory AI judge beside it. This
document covers the components, the trust boundaries, which credential goes where, how data
moves, why the judge is advisory, and how state and idempotency work.

## Components

| component | what it is | code |
|---|---|---|
| **Agent + hook** | The tenant's agent (Hermes). Its `pre_tool_call` hook posts each tool call to the facade and replays the answer. The hook shim fails closed: anything but a clean allow blocks. | approval-md-hosted `hermes-image/`; shim vendored in `service/src/approved/console/downloads/` |
| **Daemon machine** | approval.md core, unchanged: `approval up` (the daemon loop, TTL sweep, Telegram channel) and `approval serve` (the authenticated HTTP facade) under one supervisor. It holds the tenant's store: policy, hash-chained log, keys. | `images/daemon/` (vendored) |
| **Telegram approval bot** | The tenant's own bot. It sends each request to the approver's chat with Approve and Reject buttons and delivers the tap back through a secret-checked webhook. | core |
| **Judge worker** | Follows the tenant's log through the facade, runs the reviewer on each new `approval.requested`, sends one advisory, and records the eventual decision as Weave feedback. | `worker.py`, `follow.py`, `judge.py`, `reviewer.py`, `notify.py`, `feedback.py`, `state.py` |
| **Operator console** | FastAPI in the same process as the worker (`python -m approved serve`): live view, policy builder, connect bundle, `/health`, `/metrics`. | `console/` |
| **Operator CLI** | `approved provision / connect / ask / status` against Maritime. | `ops/` |
| **Weave / W&B Inference** | Traces, feedback and evaluation; the reviewer model. | external |

## Trust boundaries

1. **Agent machine → facade.** The agent is the party under oversight. It holds only the
   agent credential, which reaches the hook and the verbs a harness needs to ask and to act on
   a grant, and nothing that reads or writes the log directly.
2. **Facade → judge.** The judge is a tenant tool. It holds the tenant credential, uses one
   route (`GET /log/follow`), and treats every record's agent-supplied text as untrusted.
3. **Human → gate.** Only a tap from the approver's mapped Telegram account, delivered with
   the webhook secret, decides anything. The judge's message sits in the same chat and has
   no buttons.
4. **Model output → human.** Reviewer text is untrusted: collapsed to one line, capped at 300
   characters, redacted of token-shaped strings, and HTML-escaped before Telegram.
5. **Operator → console.** One shared console token; the session is a timestamped HMAC of it
   (12-hour server-side lifetime, invalidated when the token rotates), carried in a cookie or,
   behind a proxy that drops the Cookie header (Maritime's), in the `X-Approved-Session`
   header from the tab's `sessionStorage` (see [SECURITY.md](SECURITY.md#secret-handling)).
   `/policy` and `/health` are public; the policy page's judge preview runs the offline rules
   only, so an unauthenticated page can never spend a model call.

## Which credential goes where

| credential | held by | opens | never held by |
|---|---|---|---|
| Agent (`APPROVAL_SERVE_AGENT_TOKEN`) | daemon (to check it), the agent's hook | `/hook/hermes`, the agent verb subset | the judge (it refuses to start if one is in its environment) |
| Tenant (`APPROVAL_SERVE_TENANT_TOKEN`) | daemon (to check it), the judge, operator tools | `/log/follow`, `/export`, `/status`, tenant verbs | the agent |
| Console (`CONSOLE_TOKEN`) | the judge service | console sign-in | the browser (it sends the token once to sign in and keeps only the HMAC-derived session value, as a cookie or in the tab's `sessionStorage`) |
| Approval bot token (`HOSTED_<TENANT>_TG_BOT_TOKEN`) | daemon | Telegram, as the approval bot | the judge (it refuses to start with one in its environment; `provision --judge` refuses a judge bot file equal to it), the agent |
| Judge bot token (`JUDGE_TG_BOT_TOKEN`) | judge | Telegram `sendMessage`, as the judge: a second bot the approver has `/start`ed | the daemon, the agent |
| Webhook secret | daemon, Telegram | the daemon's `/telegram/webhook` | everyone else |
| W&B key | judge | Weave, W&B Inference | the agent machine (see the known gap in [SECURITY.md](SECURITY.md)) |

Every credential enters a process through its environment or a `*_FILE` path, and
`approved provision` passes them to Maritime only through `maritime env import <agent> <file>`.

## Data flow

1. The agent's hook posts a tool call. The facade classifies it against the attested policy.
   A read-class call is allowed at once. A manual class appends `task.registered` and
   `approval.requested` to the log and waits for a decision (up to the hook timeout; the shim
   re-asks until the human window closes).
2. The daemon's channel sends the prompt to the approver's chat.
3. The judge worker, following the log from a persisted `(seq, hash)` cursor, sees the new
   `approval.requested`. It checks each page's continuity (every `prev` is the previous
   `hash`, `seq` is contiguous from the cursor), claims the action key, runs the reviewer
   with a deadline, and sends one advisory to the same chat.
4. The human taps. The daemon appends `approval.granted` or `approval.rejected` (or the TTL
   sweep appends `approval.expired`), and the agent's next re-ask gets the answer.
5. The judge sees the terminal event, records it, and attaches `approved.human_decision` and
   `approved.agreement` feedback to its Weave call.
6. `python -m approved evaluate` replays seeded scenarios, and optionally the recorded
   decisions, through the same reviewer as a `weave.Evaluation`.

## Why the judge is advisory

approval.md's rule is that the attested policy and the human's decision are the only things
that move the gate. Core SPEC section 11 invariant 10 ("Guidance never reaches enforcement")
says that no path computing "a verdict, a classification, a sampling draw, a budget, a
token, an obligation, or a harness allow" reads human-authored guidance. A model's opinion
has even less standing than a human's guidance, so the same rule applies with more force: no
code path in the gate reads the judge's output, and the judge has no route to write one.

The design follows core's model-gloss precedent (`src/cli/gloss.ts`), which attaches a model's
one-sentence reading aid to a prompt with the rule "The gate never sees it". Like the gloss,
the judge:

- is never load-bearing: nothing branches on its content;
- fails toward absence: a timeout, a parse failure, an inference error or an open circuit
  means no message, never a placeholder, a retry in the approver's path, or a delay;
- is untrusted text: one line, capped, escaped.

What the judge adds over a gloss is accountability: every verdict is traced, every human
decision is attached to it as feedback, and the evaluation measures the dangerous error
(a READY the human rejected) directly.

## State and idempotency

The judge keeps one JSON file, `judge-state.json`, in `STATE_DIR`. It is a cache of what the
judge has done, never the truth about the log.

- **Atomic writes.** Each save writes a temp file, fsyncs it, `os.replace`s it and fsyncs the
  directory. A crash leaves the old state or the new one.
- **Cursor.** `(seq, hash)` of the last consumed record. A restart resumes from it, and the
  facade itself refuses a cursor whose hash does not match its log (`cursor-mismatch`).
- **At most one judgement per action key.** The key is claimed and saved *before* the reviewer
  runs, so a crash mid-review leaves it claimed and a restart does not judge it again. The
  judge fails toward silence, never toward a duplicate message.
- **Skips.** A request already decided later in the same page, or older than
  `JUDGE_MAX_AGE_S` (900 s), is recorded as skipped instead of judged.
- **Decisions and feedback.** Each terminal event is recorded once. Feedback is `pending`
  while there is a Weave call to attach it to, then `sent`; failures retry on idle ticks, at
  most three times, and the client skips feedback already present on the call.
- **Task context.** A task's summary is kept until its request closes, so a restart between
  registration and request does not lose the reviewer's context.
- **Unverifiable record.** A record whose links hold but whose content does not recompute
  (SHA-256 over JCS, core SPEC section 8) is skipped and recorded, not judged; the judge
  follows past it.
- **Chain break.** A broken link is persisted and terminal: the worker exits 3 and refuses to
  follow until an
  operator investigates.
- **Fail closed on bad state.** A state file that does not parse, or that belongs to a
  different facade, refuses to load.

The console reads the same file (atomic writes make that safe without a lock) plus the
worker's in-process follow status; no page view calls the facade.
