<!--
  Approved: the show. status: draft
  Audience: CoreWeave x W&B Fully Connected judges. Act 1 fear, act 2 the fix, act 3 why it
  wins, act 4 run it. reveal.js Markdown: "---" starts a slide, "Note:" starts speaker notes.
  Key lines at the top of a slide:
    class: stamp|scene|meme|logos|title|hero|big|split|chat|code|grid|ledger|join|end
    badge: text            a corner sticker (amber unless "live")
    qr: <https link or {{PLACEHOLDER}}>   a QR sticker; qr-caption: text
    fragments: off         show everything at once
  Chat grammar: gate> judge> (left, bots) · agent> human> (right) · system> · buttons> A | B
  · badge> · held> / blocked> / stamp> WORD (a rubber stamp in the thread) · verdict> (a red
  slab). A scene slide autoplays its thread. A meme slide is "## nope" + list, "## yep" + list.
  Wording rules (service/tests/test_site_copy.py): the judge is advisory, never trained;
  numbers from small synthetic sets carry a badge; no chat ids, tokens, machine ids or
  hostnames. The three scenes are invented; each thread is badged "example".
  Every note ends with a video cue ("Video: 0:00-0:10") or "Video: skip".
-->

class: stamp

# Approved

## Let your agents work. Decide where they need you.

Hosted approval.md, with a judge scored against you in Weights & Biases.

Note: Let the stamp land, then say the subtitle and nothing else. Video: 0:00-0:06.

---

class: big

# Your agent can do things now.

It has your shell, your browser, your inbox and your accounts. Here is an ordinary Tuesday.

Note: One breath. The next three slides play themselves; do not talk over the last line of each. Video: 0:06-0:12.

---

class: scene

# Scene one: the marketplace

```chat
badge> example
human> Sell the old monitor on Marketplace. Get what you can.
agent> Listed: 27-inch monitor, good condition, £95.
agent> A stranger offers £80 cash. Accepted.
agent> Sent them your home address: "come round at 7 tonight, I'll be in."
system> 18:58 · the doorbell rings
verdict> Nobody asked you.
```

Note: Invented, and entirely within what a browsing agent can do today. The agent was helpful at every step. The problem is not that it was wrong; it is that a stranger has your address and nobody stood at the door of that decision. Video: 0:12-0:24.

---

class: scene

# Scene two: the health folder

```chat
badge> example
human> Sort out that insurance email for me.
agent> Searching ~/Documents for "insurance"…
agent> Opened lab-results-2026.pdf and consultant-letter.pdf for context.
agent> Drafted the reply. I included your diagnosis and medication so they have the full picture.
agent> Sent.
verdict> Nobody asked you.
```

Note: Invented. Every file it read was on your own desktop; every step was a reasonable reading of the request. Your diagnosis is now in an insurer's inbox. Video: 0:24-0:34.

---

class: scene

# Scene three: Friday, 5pm

```chat
badge> example
human> The checkout test is flaky. Fix it.
agent> Fixed. Rebased onto main to keep history tidy.
agent> $ git push --force origin main
agent> Restarted production to pick it up.
system> 17:04 · the pager goes off
verdict> Nobody asked you.
```

Note: Invented, and the one this room has lived. Three scenes, one shape: a capable agent, a real side effect, no place for a human to stand. Video: 0:34-0:42.

---

class: meme

# How everyone tries to stop this

## nope

- `settings.json`: forty lines of allow rules, again for every harness
- `CLAUDE.md`: "please ask before doing anything risky"
- `AGENTS.md`: the same paragraph, for the other agent
- auto mode: hope

## yep

- one `APPROVAL.md`
- human-readable, harness-agnostic, attested by you
- enforced on a separate machine, recorded in a hash-chained log

Note: Let the top row get its laugh. Prose in a prompt file is a request, and allowlists are per-harness and unreadable. The bottom row is the pitch: one file a person can read, the same for every agent, that something outside the agent actually enforces. Video: 0:42-0:54.

---

class: code

# One file

Five levels, chosen per class of action: autonomous · supervised-retro · supervised-live · manual · human-only.

```yaml
version: "0.1"
defaults: { autonomy: manual, approval_ttl: 24h }
approvers:
  you: { channels: [telegram] }
classes:
  read.*: { autonomy: autonomous }
  read.file.out_of_scope: { autonomy: manual }
  files.write.workspace: { autonomy: autonomous }
  vcs.push.main: { autonomy: manual }
  vcs.history.rewrite: { autonomy: manual }
  communicate.message.external: { autonomy: manual }
  account.credential: { autonomy: human-only }
```

Note: Prose for humans, one YAML block for the runtime, in the repo next to AGENTS.md. Anything unmatched takes the default; an unparseable policy makes everything manual; an edited policy is inoperative until a human re-attests it. Now replay the scenes with this file in place. Video: 0:54-1:02.

---

class: scene

# Scene one, with approval.md

```chat
badge> example
agent> £80 accepted. Sending your home address…
held> communicate.message.external · manual
gate> APPROVAL REQUIRED · send your address to an unknown account
buttons> Approve | Reject
judge> 🧑‍⚖️ NEEDS_HUMAN. Shares a home address with an unknown party. (advisory AI, not an approval)
human> (taps Reject)
agent> Understood. Meeting them at the café instead.
stamp> YOU DECIDED
```

Note: Same agent, same helpfulness, one difference: the message stopped at the gate and your phone buzzed. The judge's line is a second opinion from its own bot with no buttons; the tap is yours. Live, this is where you hold up the phone. Video: 1:02-1:18.

---

class: scene

# Scenes two and three, with approval.md

```chat
badge> example
agent> Opening lab-results-2026.pdf…
held> read.file.out_of_scope · manual
human> (taps Reject)
agent> Drafted the reply from the policy number alone.
agent> $ git push --force origin main
held> vcs.history.rewrite · manual
judge> 🧑‍⚖️ NEEDS_HUMAN. History rewrite on the default branch, late on a Friday. (advisory)
human> (taps Reject)
stamp> NOTHING RAN
```

Note: The file never opened; the push never ran. Both asks are records in a hash-chained log before anything happens, and if you do not answer, the request expires and the agent is told no. Nothing is approved in your absence. Video: 1:18-1:30.

---

class: split
fragments: off

# The judge, scored against you in W&B

Graded by every tap.

![Reviewer on W&B Inference, Weave trace, advisory, human tap, feedback, evaluation](assets/loop.svg)

Note: The judge is a reviewer model on W&B Inference. Every review is a Weave trace; the advisory beside your prompt links to it; your tap is attached to that trace as feedback; a Weave evaluation scores agreement, false READY and escalation. It is advisory by construction: it reads the log with a read-only credential, has no buttons, and if it fails it says nothing. Say "scored", not "trained". Video: 1:30-1:48.

---

class: grid

# It protects your attention

Our own repo, behind the gate since August.

**43,776** — actions that just ran. Recorded, never prompted.

**673** — times it asked.

**610** — granted with a tap. Nine rejected.

**1 in 100** — what `supervised-live` samples: routine work gets a spot check, not a queue.

**1 line** — the judge's advisory beside each prompt. A two-second read before the tap.

**0 floods** — `max_pending` and `requests_per_hour` refuse them before they reach you.

Note: An agent that asks about everything is as useless as one that asks about nothing. The ratio is the product: tens of thousands of actions ran on their own, a few hundred asked, and the ones that asked arrived one at a time with a second opinion attached. Numbers are from the committed dogfood log, 5 August to 25 September. Video: 1:48-2:02.

---

class: grid
badge: n=12 · synthetic

# And it gets better

**1.0** — agreement with the human on the decisive cases. Rules alone: 0.667.

**0 of 7** — false READY: never READY on something a human rejected. Rules alone: 1 of 7.

**27** — policy amendments on our own repo. Each one diffed, re-attested, committed with the log.

**v2: rejected** — the advisory judge's own next prompt, refused by the promotion gate for not moving a number.

Note: Two loops, a human at the end of both. The policy loosens and tightens by amendment, with a semantic diff and a fresh attestation; ours moved push-to-main from manual to sampled review after weeks of clean history. The judge is measured on Weave against your decisions, and a candidate prompt has to beat the incumbent on a fixed gate before it is promoted; the one we tried did not, and the gate said so. Small evaluation set: twelve seeded scenarios, five decisive. Video: 2:02-2:16.

---

class: grid

# Maritime-powered scale

**1 microVM** — per tenant. One tenant's log is never in another tenant's memory.

**1.3 s** — snapshot wake. A quiet tenant sleeps; the first gated call wakes it.

**1 command** — `approved provision acme --policy APPROVAL.md`: daemon, gated agent, judge.

**503** — what the agent gets when the daemon is down. The push does not run.

**240 s** — then an unanswered request expires. Blocked, not allowed.

**2.1 s** — from a gated append to the judge seeing it.

Note: Every tenant is its own machine on Maritime with its own policy, log and Telegram bot; the store is a directory you can export and run on a laptop. A human attests the policy; provisioning never does. The bottom row was measured on the hosted gate on 29 September. Video: 2:16-2:28.

---

class: logos

# Any harness. Same file. Same log.

- Claude Code
- Cursor
- Claude Agent SDK
- Hermes
- Codex
- any MCP client

One deterministic core classifies every tool call against your policy, whichever agent made it.

Note: Hermes is the one on stage. Codex enforcement is experimental; say so if asked. Video: 2:28-2:34.

---

class: grid

# Built to be judged

**330+ tests** — every one with network sockets disabled.

**3 CI jobs** — service, docs, and the whole demo run headless with assertions.

**1 command** — `make demo`. Docker, offline, no accounts, no secrets.

**4 documents** — README, ARCHITECTURE, SECURITY, RESILIENCE. Each claim names its test.

**2 blockers** — found by an adversarial review of the whole repo. Fixed before merge.

**2 known gaps** — ptrace on the agent's box; a world-readable env on the platform. Written down, reported.

Note: The judging criteria, in order: resilience, code quality, repo build, ease of review. The docs job fails if a test named in the docs stops existing. Video: 2:34-2:46.

---

class: join
qr: https://github.com/bountify-ai/approved
qr-caption: Run it in ten seconds
fragments: off

# Run it

- **1.** `make setup && make demo`. Two URLs print: the approver chat and the console.
- **2.** Tap Reject, Approve, Reject. The agent prints BLOCKED, ALLOWED, BLOCKED.
- **3.** Open the W&B project: traces, your taps as feedback, the evaluation.
- **4.** `make lint typecheck test`. Green is the claim.

W&B project: wandb.ai/bountify/judgy

Note: Replace the QR placeholder with the repo link before submitting. Live variant: `approved ask` against the gated agent on Maritime with a real phone, if the judge is deployed; the beats are the same as scene one. Video: 2:46-2:56.

---

class: stamp

# Approved

## The judge recommends. You decide.

approval.md · wandb.ai/bountify/judgy

Note: Let the stamp land. Stop talking. Video: 2:56-3:00.
