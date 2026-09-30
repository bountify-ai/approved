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

## Release evidence

Before public exposure, run service lint/typecheck/tests, gateway security tests,
the real image smoke, and independent contract conformance and adversarial review.
Recheck fixes at the affected boundaries. Record exact exit codes and unresolved findings.

Use two independent browser sessions to prove isolation and exercise both Approve and
Reject through the real daemon. Verify the append-only chain, a real live advisory and
trace, judge failure behavior, expiry, capacity, and refresh/retry behavior. Inspect the
published entry on desktop and mobile and verify that its deck and builder remain usable.
Health responses alone are not evidence that this complete experience works.
