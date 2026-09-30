# Get Approved: hosted demo contract

This contract covers the public hackathon experience linked from
`https://approval.md/approved/`. It is a demo of real approval.md enforcement with a
scripted agent and a live advisory model. It is not production tenant onboarding.

## Visitor experience

- Get Approved appears before the existing deck and policy-builder links. One click
  starts a private browser session and the fixed demonstration, without an account,
  console token, Telegram account, or local software.
- The embedded experience shows agent requests, the gate holding actions, live judge
  advisories when available, human Approve/Reject controls, and the recorded outcomes.
- The agent and browser messaging transport are explicitly labelled as demo components.
  Approval decisions and log verification come from the real pinned core runtime.
- A judge timeout or failure is shown as absence. It never grants, rejects, delays,
  or disables a human decision. An offline reviewer is never presented as live AI.
- Starting, busy, failed, expired, and complete states explain the next available action.
  Keyboard, narrow-screen, and reduced-motion use remain supported.

## Authority and isolation

- Every visitor session has a distinct store, log, runtime processes, credentials, and
  agent run identity. Capacity is bounded to two simultaneous sessions initially.
- An unguessable visitor capability authorizes only that session. Another session's
  capability, guessed IDs, replayed buttons, and expired capabilities confer no authority.
- Session capabilities appear only in the issuing response and subsequent request headers.
  They never enter URLs, logs, model prompts, or another visitor's response.
- A dedicated-origin gateway holds the backend credential. Direct calls to the shared
  Maritime origin cannot create sessions, see visitor state, or approve anything.
- The gateway forwards only explicitly listed routes and methods to its fixed backend.
  It never follows redirects with credentials or forwards arbitrary paths or headers.
- The judge receives the read-only tenant credential and its separate demo messaging
  credential. It receives neither the agent credential nor the approval-bot credential.
- The script runs only fixed demonstration scenarios. Visitor text cannot become a
  shell command, model prompt, policy, path, or provisioning parameter.

## Lifecycle and spending

- A session lasts at most 15 minutes. Capacity reservation and release are atomic.
  Expiry stops only that session's processes and revokes its capability.
- Expiry/reset never truncates, edits, or fabricates approval-log records. A later
  session gets a new store. Completed stores remain unavailable to public requests.
- Live inference admission has a shared persistent cap across sessions and restarts.
  The cost calculation includes provider retries and maximum completion tokens.
- The owner authorized at most $25 of incremental overnight cloud/inference spending.
  Deployment must use verified rates and bounded resources before enabling public runs.
  Exhaustion prevents further paid admission and is displayed explicitly.
- Existing dogfood, Hermes, and judge deployments are outside the mutation scope.

## Visitor telemetry

Authenticated session state may include a versioned, bounded view of that visitor's
runtime. It identifies a session by a short fingerprint derived from its random ID,
never by its capability. Lifecycle times and elapsed session age use measured clocks.
The live demo uses isolated processes and stores on one Maritime deployment, not a new
machine per visitor. The provider label appears only when the deployment sets
`TRYIT_MARITIME_DEPLOYMENT=1`; local image runs omit it. Process running and health fields come from the supervisor and
loopback health probes; a missing judge is reported as absent and does not stop a human
decision. An allowlisted lifecycle event list contains only timestamps and fixed event
names, capped to the latest 20 entries. No raw process logs, credentials, internal paths,
provider usage estimates, or another visitor's state reach this view. CPU, memory, cost,
and model call progress must not be inferred from a started process or a pending scenario.

The visitor may separately request the current APPROVAL.md file for their own ready
session. This fixed authenticated read returns at most 16 KiB of UTF-8 plaintext, its
actual VPS path, and a SHA-256 digest of those exact bytes. The path is shown because
the visitor explicitly asked to inspect the live file; it is never accepted as input,
included in general telemetry, or available across sessions. The display does not
claim that the current bytes match a historic policy attestation. A symlink, non-regular
file, oversize file, or unavailable store is refused without exposing file contents.

## Release evidence

Before public exposure, run service lint/typecheck/tests, gateway security tests,
the real image smoke, and independent contract conformance and adversarial review.
Recheck fixes at the affected boundaries. Record exact exit codes and unresolved findings.

Use two independent browser sessions to prove isolation and exercise both Approve and
Reject through the real daemon. Verify the append-only chain, a real live advisory and
trace, judge failure behavior, expiry, capacity, and refresh/retry behavior. Inspect the
published entry on desktop and mobile and verify that its deck and builder remain usable.
Health responses alone are not evidence that this complete experience works.
