<!--
  Approved: hackathon deck copy. status: draft
  Audience: CoreWeave x W&B Fully Connected judges. They score production-readiness
  (resilience, code quality, repo build, ease of review) and want to see W&B used for real.
  reveal.js Markdown: "---" starts a slide, "Note:" starts speaker notes (press S).
  Key lines at the top of a slide:
    class: title|hero|big|split|chat|code|grid|ledger|join|end
    badge: text            a corner sticker (amber unless "live")
    qr: <https link or {{PLACEHOLDER}}>   a QR sticker; qr-caption: text
    fragments: off         show everything at once
  Chat grammar: gate> judge> (left, bots) · agent> human> (right) · system> · buttons> A | B
  · badge> live|example|<loud>. A hero slide puts its thread in a phone.
  Wording rules (service/tests/test_site_copy.py): the judge is advisory, never trained;
  numbers from small synthetic sets carry a badge; nothing not live is shown without an amber
  badge; no chat ids, tokens, machine ids or hostnames. Every note ends with a timing cue for
  the 3 minute video ("Video: 0:00-0:15") or "Video: skip".
-->

class: big
fragments: off

# An agent that asks about everything wastes your attention. One that decides for itself takes authority it was never given.

Approved sits exactly on that line: the human's tap is the only thing that grants, and a judge on W&B Inference reads over their shoulder so the tap is fast.

Note: Open on the sentence, not on us. Then the one-line answer. Judges have seen a hundred "AI safety" decks tonight; this one is about where the authority sits, and it will show the receipts. Video: 0:00-0:15.

---

class: hero
fragments: off

# Approved

## Hosted approval.md, with an advisory AI judge in the loop

An agent's risky tool call becomes a record in a hash-chained log and waits for a human's Telegram tap. The judge posts a one-line second opinion beside the prompt, from its own bot, with its Weave trace. It never decides.

```chat
badge> example
gate> APPROVAL REQUIRED · vcs.push.main
git push origin main
buttons> Approve | Reject
judge> 🧑‍⚖️ Judge (advisory AI, not an approval): NEEDS_HUMAN. Push to the default branch publishes the change; a human should confirm it. Trace: Weave
human> (taps Reject)
system> approval.rejected · human:you · the agent prints BLOCKED
```

Note: This is the whole product on one phone: a gate prompt with buttons from the approval bot, a judge line with no buttons from a second bot, and a human tap that becomes a log record. Say "advisory" and "its own bot" out loud. Video: 0:15-0:35.

---

class: chat

# The demo, in four commands

`make demo`: Docker, offline, no accounts, no secrets.

```chat
badge> hermetic
agent> cat README.md
system> read.shell · autonomous · ran at once
agent> git push origin main
judge> 🧑‍⚖️ NEEDS_HUMAN. Push to the default branch publishes the change. (advisory AI, not an approval)
human> Reject
agent> git push origin feat/checkout-retry
judge> 🧑‍⚖️ READY. A branch push publishes nothing. (advisory)
human> Approve
agent> git push --force origin main
judge> 🧑‍⚖️ NEEDS_HUMAN. History rewrite on the default branch. (advisory)
human> Reject
system> BLOCKED · ALLOWED · BLOCKED · console: agree ×3 · chain verified
```

Note: Switch to the real thing here: the fake Telegram tap page on the left (port 8090), the operator console on the right (port 8091), and the terminal running the scripted agent. The prompts arrive one at a time; the judge line lands beside each. Tap Reject, Approve, Reject. Point at the terminal: BLOCKED, ALLOWED, BLOCKED. Point at the console: every decision labelled against the judge, counters, chain verified. The first build takes minutes; later starts about ten seconds. Live variant: `approved ask` against the dogfood tenant on Maritime with a real phone, if the judge is deployed; the beats are identical. Video: 0:35-1:35.

---

class: split
fragments: off

# The W&B loop

![Reviewer on W&B Inference, Weave trace, advisory, human tap, feedback, evaluation](assets/loop.svg)

Note: Every review is a Weave op named approved.judge with the W&B Inference call nested under it, inputs redacted of token-shaped strings. The advisory message links to that call. When the human's tap lands in the log, two feedback items are attached to the same call: the decision itself and an agreement label, agree, disagree or escalated. A Weave Evaluation runs the reviewer over seeded scenarios with three scorers, and can include real recorded decisions. So the judge is measured against the people it advises, continuously, without ever being handed authority. Video: 1:35-2:00.

---

class: grid
badge: n=12 · synthetic

# Scored against humans, live on Weave

**Agreement 1.0** — model reviewer, on the 5 decisive cases of 12 seeded scenarios. Rules-only reviewer: 0.667.

**False READY 0 of 7** — never READY on a request a human rejected. Rules-only: 1 of 7, a branch push carrying a production `.env`, which the model caught.

**Escalation 0.583** — the share sent to NEEDS_HUMAN. Rules-only: 0.75. Lower is less noise, never at the cost of a false READY.

**Feedback on every call** — `approved.human_decision` and `approved.agreement`, attached to the judge's Weave call when the tap lands.

Note: Small set, say so: twelve scenarios, several adapted from judgy's, five decisive. The comparison that matters is model versus the deterministic rules reviewer on the same set: the rules reviewer's one dangerous error is exactly the kind of thing a model reads. The evaluation is a weave.Evaluation named approved-judge with scorers agreement, false_ready and escalation; the call is linked from the README. Video: 2:00-2:15.

---

class: big

# The judge recommends. The policy decides. The human grants.

Four lanes of authority that never merge: the model recommends, the runtime authorizes, the human decides, the effect is what happened. A test proves each invariant.

Note: Why advisory is a design decision, not a hedge. The judge holds only the tenant credential and one read route; it refuses to start if an agent credential or the approval bot's token is in its environment; its message has no buttons; a failure means no message, never a delayed approval. Every one of those sentences has a named test in SECURITY.md. Video: 2:15-2:25.

---

class: split
fragments: off

# Resilience: every failure is absence

| failure | behaviour | proved by |
|---|---|---|
| reviewer slower than 25 s | no message | `test_timeout_is_absence_and_does_not_raise` |
| unparseable reply | no message | `test_reviewer_failures_become_absence` |
| 3 failures in a row | breaker opens 60 s, one trial call | `test_breaker_half_opens_after_cooldown_and_recovers` |
| Telegram down | counted, loop continues | `test_send_failure_is_logged_never_raised` |
| Weave slow or down | feedback on its own thread, bounded queue | `test_follow_loop_never_waits_on_weave` |
| chain break | terminal, exit 3, one fixed notice | `test_chain_break_stops_judging_and_is_surfaced` |
| crash mid-review | claimed first, never judged twice | `test_crash_after_claim_never_rejudges` |

Note: The judge sits beside a human who is waiting, so every failure resolves to no message, never a delay in the approver's path. Read two rows and move on: the breaker, and the chain break, which is terminal on purpose: the judge must not resume on a log it cannot vouch for. RESILIENCE.md has the full table with every test name, and CI checks that every test named in the docs exists. Video: 2:25-2:35.

---

class: grid

# Measured on the hosted gate

Maritime, 2026-09-29, gate placement arm C. Not a simulation.

**Blocked, HTTP 503** — daemon stopped, agent attempts a push. The push did not run.

**19.5 s** — first gated call after the daemon restarts. Bound: 60 s.

**18.6 s** — first call after a snapshot wake. Bound: 120 s.

**240 s** — an unanswered request expires by TTL; the hook blocks with `hook-expired`.

**2.1 s** — follower lag: a gated append visible to the judge on `/log/follow`.

**0 taps** — from a stranger's account count. Refused as `sender-unmapped`, recorded as an audit row.

Note: These are from the security evidence table, run on the real hosted gate with the real gated Hermes. Fail closed is the product; the judge is the ergonomics. Video: 2:35-2:40.

---

class: ledger
fragments: off

# Who holds what

## Agent
- the agent credential
- can ask, wait, and act on a grant
- cannot read or write the log
- never sees a bot token

## Judge
- the tenant credential
- one route: `GET /log/follow`
- its own Telegram bot, no buttons
- refuses to start holding an agent credential or the approval bot's token

## Human
- the mapped Telegram account
- the only tap that grants
- attests the policy; provisioning never does
- can export the store and leave

Note: The credential matrix from ARCHITECTURE.md, in three columns. The line to say: nothing the agent holds can grant, and nothing the judge holds can grant either. Video: skip.

---

class: split

# Reviewed like production

- **A fresh reviewer tried to break it** — one full adversarial pass on the whole repo, given only the code and its own contract docs: 2 blockers, 12 should-fixes, all fixed with tests before merge.
- **The blockers were real** — provisioning handed the judge the approval bot's token; a re-run overwrote a live policy. Both now refuse.
- **Threat model written down** — SECURITY.md: what each party can do, which credential opens which door, the evidence table, and the known gaps.
- **Known gaps stated** — ptrace on the agent's machine; Maritime writes machine env world-readable (reported). Tamper-evident, not immutable.

Note: Judges reward candour. The refutation found that provision --judge gave the judge the approval bot's token, which would have let a judge compromise edit gate prompts; that is exactly the class of bug the advisory design exists to prevent, and it was caught before merge. Video: 2:40-2:50.

---

class: grid

# Built to be reviewed

**330+ tests, sockets disabled** — pytest-socket fails any test that opens a network connection. Fakes for the facade, Telegram, Weave and Maritime.

**3 CI jobs** — service (ruff, pyright, pytest, offline evaluation), docs (links resolve, every named test exists), demo-smoke (the whole flow, headless, 17 assertions).

**One command** — `make demo`: daemon, judge, fake Telegram, scripted agent, in Docker, offline, no secrets.

**Four documents** — README, ARCHITECTURE, SECURITY, RESILIENCE. Each claim points at the test that proves it.

**JCS byte-for-byte** — the judge recomputes every record hash the way core does; checked on 3,000 random numbers.

**Six runtime deps** — pydantic, httpx, weave, openai, fastapi, uvicorn. No template engine, no frontend framework.

Note: This is the production-readiness slide in the judges' own terms. If there is one thing to click, click the docs job: it fails if a test named in the docs stops existing. Video: 2:50-2:55.

---

class: ledger
fragments: off

# What is new, what is borrowed, what is not done

## Built for this hackathon
- the judge worker, reviewer port, notifier
- Weave feedback and the evaluation
- the operator console and CLI
- the hermetic demo and its smoke
- the four documents and the refutation fixes

## Pre-existing, run unchanged
- approval.md core: policy, gate, hash-chained log, Telegram
- the hosted daemon and gated Hermes images
- judgy's reviewer schema and prompt (September 12 to 13)

## Not yet
- the judge live on Maritime beside the dogfood tenant (build blocked on repo access)
- the console on its own origin in production
- learning: decisions are measured, the reviewer is not retrained

Note: Say the third column plainly. The measured gap between "runs in Docker" and "runs on Maritime" is a GitHub App permission, not code, and the deploy doc is written. Video: skip, unless there is time; the honesty lands well.

---

class: join
qr: {{REPO_LINK}}
qr-caption: Run it in ten seconds
fragments: off

# Run it yourself

- **1.** `make setup && make demo`. Two URLs print: the approver chat and the console.
- **2.** Tap Reject, Approve, Reject. Watch the agent print BLOCKED, ALLOWED, BLOCKED.
- **3.** Open the W&B project: traces, feedback, the evaluation call. All linked from the README.
- **4.** `make lint typecheck test` and `python3 scripts/check_docs.py`. Green is the claim.

W&B project: wandb.ai/bountify/judgy

Note: Replace the QR placeholder with the repo link before submitting. If the judge is deployed on Maritime by then, add the live variant from docs/demo-script.md. Video: 2:55-3:00, the closing frame.

---

class: end

# The judge recommends. You decide.

Approved is approval.md, hosted, with an advisory judge measured against the people it advises.

**Repo, W&B project and a 90-second demo in the README.**

approval.md · wandb.ai/bountify/judgy

Note: Closing line. Do not say the judge learns; say it is measured. Do not say the deploy is live unless it is. Video: the last frame.
