# Resilience

How each part behaves when something fails, and what proves it.

## The gate fails closed

The gate is approval.md core. The agent's hook shim treats anything that is not a clean allow
from the facade (a transport error, a non-200, an unparseable body) as a block, and the daemon
image answers 503 while its facade is down. Measured on Maritime on 2026-09-29 (gate placement,
arm C, case 2; see [SECURITY.md](SECURITY.md)):

| event | result |
|---|---|
| Daemon stopped, agent attempts a gated push | blocked (`approval facade unreachable: HTTP 503`); the push did not run |
| Daemon started again | first gated call succeeded in 19.5 s (bound 60 s) |
| Daemon asleep (snapshot), agent calls | the call woke the daemon; first call succeeded in 18.6 s (bound 120 s) |
| Unanswered manual request | expired at 240 s by the daemon's TTL sweep; the shim blocked with `hook-expired` |

## The judge fails toward absence

The judge sits beside a human who is waiting, so every failure resolves to *no message*, never
to a delay, a retry in the approver's path, or a placeholder.

| failure | behaviour | proved by |
|---|---|---|
| Reviewer slower than `JUDGE_TIMEOUT_S` (25 s) | abandoned; no message; `judge.absent.timeout` | `test_timeout_is_absence_and_does_not_raise` |
| Unparseable model reply | no message; `judge.absent.parse` | `test_reviewer_failures_become_absence` |
| Inference error (transport, HTTP, empty or truncated reply after one re-ask) | no message; `judge.absent.inference` | `test_live_reviewer_second_truncation_is_an_inference_error` |
| Repeated failures | circuit breaker opens after 3 consecutive failures, skips the reviewer for 60 s, then admits one trial call | `test_breaker_half_opens_after_cooldown_and_recovers`, `test_half_open_failure_reopens_immediately` |
| Telegram down or refusing | logged and counted; the verdict is recorded as `silent`; the loop continues | `test_send_failure_is_logged_never_raised`, `test_telegram_failure_is_recorded_silent_and_loop_continues` |
| Weave down or slow | tracing is skipped; feedback runs on its own thread from a bounded queue, so the follow loop never waits on Weave; a full queue drops the submission (counted, `feedback.dropped`) and the decision stays pending for the next idle pass; at most 3 attempts | `test_failure_is_isolated_counted_and_bounded`, `test_follow_loop_never_waits_on_weave`, `test_a_full_feedback_queue_drops_and_counts` |
| Facade unreachable or 5xx | logged; the cursor does not move; retried at the poll interval for 5 failures, then with capped exponential backoff. `approval serve` answers one call at a time, so it is briefly unavailable while a hook call holds it; the follow timeout (20 s) exceeds the facade's hook wait so a queued follow is answered when that wait ends | `test_transport_errors_back_off_without_moving_the_cursor`, `test_follow_failures_retry_quickly_before_backing_off` |
| Credential refused (401/403) | logged as `follow.credential-refused`; retried with backoff | `test_unauthenticated_facade_is_a_credential_error_not_a_crash` |
| Chain break (a link, a recomputed hash or the cursor does not match) | terminal: persisted, one fixed-text notice to the approver, exit code 3; a restart refuses to follow until an operator investigates; nothing is skipped | `test_chain_break_stops_judging_and_is_surfaced`, `test_broken_links_are_chain_breaks` |
| A record's links hold but its content does not recompute (or cannot be encoded) | not terminal: the record is marked `record-unverifiable`, never judged, counted and shown in the console; the judge follows past it | `test_content_failures_are_not_terminal`, `test_an_unverifiable_record_is_skipped_surfaced_and_followed_past` |
| The worker thread ends on its own under `serve` (chain break, or an uncaught error) | `/health` answers 503 `{"status": "degraded", "reason": ...}`; about 3 s later the process exits non-zero (3 for a chain break, 1 otherwise) so the platform restarts it; a chain break stays terminal across restarts through state | `test_chain_break_degrades_and_shuts_down`, `test_a_crashed_worker_degrades_with_a_nonzero_exit`, `test_health_answers_503_when_degraded` |
| Crash mid-review | the action key was claimed before the reviewer ran, so the restart does not judge it again (at most once) | `test_crash_after_claim_never_rejudges` |
| SIGTERM | the in-flight judgement finishes, state is saved, the loop exits 0 | `test_graceful_stop_finishes_the_inflight_judgement` |
| Corrupt state file, or state from another facade | refuses to start (exit 4) rather than re-judge | `test_corrupt_state_refuses_to_load`, `test_state_for_another_facade_refuses` |

### Chain break and restarts

A chain break exits `serve` non-zero, so the platform restarts it, and each restart exits again
at once because the break is persisted. That restart loop is deliberate: the judge must not
resume on a log it cannot vouch for. The way out is an operator's: investigate the log (with
`approval log verify` on the daemon), and only then clear `STATE_DIR` so the judge starts a
fresh follow.

## Cursor persistence

The judge follows the log from an exclusive `(seq, hash)` cursor, saved atomically (temp file,
fsync, rename, directory fsync) after each page and at every claim within it. A restart
resumes from the saved cursor with `cursor_hash`, so the facade itself checks that the log
still continues from the point the judge consumed. Replaying records after a crash is safe,
because every effect is keyed by action key (`test_restart_resumes_from_persisted_cursor`).

## Maritime `stop` does not flush

In an earlier gate-placement run, the daemon's TTL sweep began an `approval.expired` append 20
seconds before an operator's `maritime stop`, and the stop did not flush it: the log was left
with a zero-filled tail after record 14. Records 1 to 14 still verify; that store was
quarantined, not repaired. Durability under a platform stop rests on core fsync-on-append.

How the operations avoid it:

- `approved provision` stops and starts a machine only right after creating it, when it has
  never held credentials and no request can be open. A reused machine is never stopped.
- The connect bundle says to stop then start only while no approval request is open.
- The gate-placement runner refuses any lifecycle action while a request is open or a
  detached job is unresolved.

## What CI covers

| job | what it runs |
|---|---|
| `service` | `uv sync --locked`, `ruff check`, `ruff format --check`, `pyright`, `pytest` (230+ tests, sockets disabled), offline evaluation with Weave disabled, and `approved connect` |
| `demo-smoke` | builds both images (layer-cached), brings up the demo stack, runs the scripted agent, taps approve and reject through the fake Telegram, and asserts: the agent's outcomes, exactly one advisory per manual request, decisions and feedback in the judge's state, the console's live view behind its token, `approval log verify` clean, and no generated credential in any container log; then tears the stack down |
| `docs` | every relative link in the Markdown resolves, and every test named in these documents exists |
