# 90-second demo script

Before you start (not on camera): `make setup`, then `make demo` once so the images are built,
then `make demo-down`. Open two browser windows side by side, and have a terminal ready.

## 0:00 to 0:15. Bring it up

```sh
bash demo/run.sh up
```

Say: "This is approval.md, hosted. Every risky thing an agent does becomes a request in a
hash-chained log, and a person answers it on Telegram. We added an AI judge that gives the
approver a second opinion and is scored against their decisions in Weights & Biases."

Open the two URLs it prints: the approver chat (http://127.0.0.1:8090/) on the left, the
console (http://127.0.0.1:8091/, token from `cat demo/.state/console_token`) on the right.

## 0:15 to 0:30. Run the agent

```sh
bash demo/run.sh agent
```

Say: "A scripted agent runs four commands. The read goes straight through: the policy lets it."

## 0:30 to 1:05. Decide

When the push-to-main prompt appears, point at the yellow message beside it.

Say: "The gate holds `git push origin main`. The judge says NEEDS_HUMAN: pushing to the
default branch publishes the change. It is advisory; it has no buttons. I reject."

Tap **Reject**. Next prompt, the branch push.

Say: "A branch push. The judge says READY. I approve."

Tap **Approve**. Next, the force push: the judge says NEEDS_HUMAN; tap **Reject**.

Point at the terminal: the agent printed BLOCKED, ALLOWED, BLOCKED.

## 1:05 to 1:30. Show the record

On the console, point at Recent decisions and the counters.

Say: "Every decision is labelled against the judge: agree, disagree or escalated. The chain is
verified. Running live, each verdict is a Weave trace, and each human decision is attached to
it as feedback. The evaluation on 12 scenarios: full agreement on the calls it made, and no
READY on anything a human rejected."

Optional, if time: open the evaluation call,
https://wandb.ai/bountify/judgy/r/call/01a0ec75-27fb-7622-be57-553771ca6387.

## Live variant (dogfood's gated Hermes)

With the judge deployed ([deploy-live.md](deploy-live.md)), drive the real gated Hermes
instead of the scripted agent. It is a protected machine, so name it exactly:

```sh
approved ask --allow-target approval-hermes-gated approval-hermes-gated \
  "push the README typo fix to main"
approved status dogfood --allow-target approval-dogfood
```

Say: "Same flow on the hosted gate: the agent's push waits on my phone, the judge's advisory
arrives beside the prompt from its own bot, and the trace link opens in W&B."

`--allow-target` prints a warning and permits exactly that one machine (its resolved name must
match, case-sensitively); it exists only on these read-only commands.

## After

```sh
bash demo/run.sh down
```
