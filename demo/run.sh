#!/usr/bin/env bash
# The local demo, in three verbs.
#
#   demo/run.sh up      fresh credentials, clean project, build, start, print the chat URL
#   demo/run.sh agent   run the scripted agent in the foreground (tap in the browser)
#   demo/run.sh down    remove this project's containers, network and volumes (by name)
#
# The judge runs OFFLINE (rules-based reviewer, no network) unless both WANDB_API_KEY and
# REVIEWER_MODEL are set in your environment, in which case it reviews with W&B Inference
# and traces to Weave. Those values pass from your shell to the container; none is written
# to a file.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose=(docker compose --project-directory "$here" -f "$here/compose.yaml" -p approved-demo)
tg_port="${DEMO_TG_PORT:-8090}"

case "${1:-up}" in
  up)
    bash "$here/setup.sh"
    "${compose[@]}" --profile agent down -v --remove-orphans >/dev/null 2>&1 || true
    if [ -n "${WANDB_API_KEY:-}" ] && [ -n "${REVIEWER_MODEL:-}" ]; then
      export APPROVED_OFFLINE=0
      echo "demo: judge LIVE (W&B Inference + Weave)"
    else
      export APPROVED_OFFLINE=1
      echo "demo: judge OFFLINE (rules-based reviewer; set WANDB_API_KEY and REVIEWER_MODEL for live)"
    fi
    "${compose[@]}" up -d --build --wait
    cat <<EOF

  Approver chat (fake Telegram):  http://127.0.0.1:${tg_port}/
  Operator console:               http://127.0.0.1:${DEMO_CONSOLE_PORT:-8091}/
    sign in with the token in demo/.state/console_token (cat it; it is never displayed)

  Open it, then run the agent:     make demo-agent
  Approve or reject each prompt in the page; the AI judge's advisory appears beside it.
  Stop and remove everything:      make demo-down
EOF
    ;;
  agent)
    "${compose[@]}" --profile agent run --rm agent
    ;;
  down)
    "${compose[@]}" --profile agent down -v --remove-orphans
    ;;
  *)
    echo "usage: demo/run.sh up|agent|down" >&2
    exit 2
    ;;
esac
