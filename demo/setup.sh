#!/usr/bin/env bash
# Generate fresh, throwaway credentials for the local demo into demo/.state/ (mode 0600,
# gitignored). Nothing here is a real credential: every value is random per run and only
# ever reaches the local containers and the fake Telegram.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
state="$here/.state"
umask 077
mkdir -p "$state"
chmod 700 "$state"

rand() { openssl rand -hex "${1:-16}"; }
agent_token="$(rand 24)"
tenant_token="$(rand 24)"
webhook_secret="$(rand 24)"
gate_bot="7001:$(rand 18)"   # the approval gate's bot
judge_bot="7002:$(rand 18)"  # the judge's own bot, posting into the same approver chat
console_token="$(rand 24)"   # the operator console's sign-in token

# The daemon holds both serve credentials (it checks each caller against them), its bot
# token and the webhook secret.
cat >"$state/daemon.env" <<EOF
APPROVAL_SERVE_AGENT_TOKEN=${agent_token}
APPROVAL_SERVE_TENANT_TOKEN=${tenant_token}
APPROVAL_TG_WEBHOOK_SECRET=${webhook_secret}
HOSTED_DEMO_TG_BOT_TOKEN=${gate_bot}
EOF
# The judge holds the TENANT credential only, and its own bot token.
cat >"$state/judge.env" <<EOF
TENANT_TOKEN=${tenant_token}
TG_BOT_TOKEN=${judge_bot}
CONSOLE_TOKEN=${console_token}
EOF
# The agent holds the AGENT credential only.
cat >"$state/agent.env" <<EOF
AGENT_TOKEN=${agent_token}
EOF
# For demo/smoke.sh only: the tenant credential, to read the facade the way the judge does.
printf '%s' "$tenant_token" >"$state/tenant_token"
# The console sign-in token: paste it at the console's /login (never shown by the console).
printf '%s\n' "$console_token" >"$state/console_token"
chmod 600 "$state"/*
echo "demo: fresh credentials written to demo/.state/ (mode 0600)"
