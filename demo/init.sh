#!/bin/sh
# DEMO INIT STEP. Seeds the demo tenant's store and attests its policy AS human:demo.
#
# In production a human attests: they read the policy and run `approval policy attest` as
# themselves, and every gate verb refuses until they have. This step does it for the demo so
# the stack comes up unattended. It runs in the daemon image, against the daemon's volume,
# through the runtime's own verbs: nothing here writes a log record by hand.
set -eu

store="${APPROVAL_DATA_DIR:-/data}/demo"
cli="node ${APPROVAL_CLI}"

if [ -f "$store/APPROVAL.md" ] && cmp -s /demo/APPROVAL.md "$store/APPROVAL.md"; then
  echo "demo-init: store already seeded with this policy; nothing to do"
  exit 0
fi

mkdir -p "$store/backlog/tasks"
cd "$store"
if [ ! -d "$store/.approval" ]; then
  $cli init --dir "$store" --json >/dev/null
fi
cp /demo/APPROVAL.md "$store/APPROVAL.md"
echo "demo-init: attesting the demo policy as human:demo (DEMO ONLY: in production a human attests)"
$cli policy attest --as human:demo --json
echo
