# Approval Policy: Approved demo tenant

A demo policy for the local stack in `demo/compose.yaml`. Reads run on their own; every
push is a human decision on Telegram. The approver `demo` taps from Telegram account 4242,
which is the fake Telegram's demo sender. The bot token and chat id are read from the
tenant-prefixed variables named below, which `make demo` generates fresh per run.

```yaml approval-policy
version: "0.1"
defaults:
  autonomy: manual
  channel: telegram
  approval_ttl: 10m
  on_expiry: reject
classes:
  read.*:              { autonomy: autonomous }
  vcs.push.branch:     { autonomy: manual }
  vcs.push.main:       { autonomy: manual }
  vcs.history.rewrite: { autonomy: manual }
approvers:
  demo:
    channels: [telegram, cli]
    senders:
      telegram: "4242"
channels:
  telegram:
    token_env: HOSTED_DEMO_TG_BOT_TOKEN
    chat_id_env: HOSTED_DEMO_TG_CHAT
```
