#!/usr/bin/env bash
# demo-smoke: the whole Approved flow, headless, offline, asserted end to end.
#
#   stack up -> scripted agent (read, push main, branch push, force push)
#   -> the demo approver taps through the fake Telegram API:
#        approve the branch push, reject the push to main and the force push
#   -> assert: agent outcomes; exactly one judge advisory per manual request; the decisions
#      reached the judge's state with feedback marked not-applicable (offline: no Weave call);
#      `approval log verify` is clean; no generated credential appears in any container log
#   -> tear down this project's containers, network and volumes (by project name).
#
# Needs docker (with compose v2), curl, openssl and python3. No network beyond image builds.
# KEEP=1 leaves the stack running for inspection; SKIP_BUILD=1 uses images already built.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project=approved-demo
tg_port="${DEMO_TG_PORT:-8090}"
console_port="${DEMO_CONSOLE_PORT:-8091}"
facade_port="${DEMO_FACADE_PORT:-8088}"
compose=(docker compose --project-directory "$here" -f "$here/compose.yaml" -p "$project")
scratch="$(mktemp -d "${TMPDIR:-/tmp}/approved-smoke.XXXXXX")"
started=$(date +%s)
pass=0
fail=0

ok() { printf '  PASS  %s\n' "$1"; pass=$((pass + 1)); }
no() { printf '  FAIL  %s\n' "$1"; fail=$((fail + 1)); }
say() { printf '\n== %s (t+%ss)\n' "$1" "$(($(date +%s) - started))"; }

cleanup() {
  local code=$?
  if [ "$fail" != "0" ] || [ "$code" != "0" ]; then
    echo "-- last log lines (for diagnosis) --"
    "${compose[@]}" logs --tail 25 daemon service fake-telegram 2>&1 | cut -c1-240 || true
  fi
  if [ "${KEEP:-0}" = "1" ]; then
    echo "KEEP=1: stack left running (make demo-down to remove it)"
  else
    "${compose[@]}" --profile agent down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  rm -rf "$scratch"
}
trap cleanup EXIT

chat() { curl -fsS "http://127.0.0.1:${tg_port}/api/chat"; }

say "credentials and a clean project"
bash "$here/setup.sh"
"${compose[@]}" --profile agent down -v --remove-orphans >/dev/null 2>&1 || true

say "build and start (daemon, judge, fake Telegram)"
if [ "${SKIP_BUILD:-0}" = "1" ]; then
  # CI builds both images first with a layer cache; use them as they are.
  "${compose[@]}" up -d --wait --no-build
else
  "${compose[@]}" build --quiet
  "${compose[@]}" up -d --wait
fi
ok "stack healthy"

say "scripted agent, with the demo approver tapping through the fake Telegram API"
export AGENT_RUN_ID="smoke$(date +%s)"
"${compose[@]}" --profile agent run --rm -T -e AGENT_RUN_ID="$AGENT_RUN_ID" agent \
  >"$scratch/agent.out" 2>&1 &
agent_pid=$!

# The decision the demo approver makes for each scenario, keyed by the action key's
# session part (hook:demo-<run>-<scenario>:...).
python3 - "$tg_port" "$agent_pid" "$scratch/taps.log" <<'PY'
import json, os, sys, time, urllib.request

port, agent_pid, log_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
decide = {"push-main": "r", "push-branch": "g", "force-push": "r"}
tapped = set()
deadline = time.time() + 240
log = open(log_path, "w")

def chat():
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/chat", timeout=5) as r:
        return json.load(r)["messages"]

def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

while time.time() < deadline and alive(agent_pid):
    messages = chat()
    for index, message in enumerate(messages):
        if not message["buttons"] or message["message_id"] in tapped:
            continue
        # The prompt's header message names the action key; it precedes the button message.
        header = next(
            (m["text"] for m in reversed(messages[:index]) if "APPROVAL REQUIRED" in m["text"]), ""
        )
        scenario = next((s for s in decide if f"-{s}:" in header), None)
        if scenario is None:
            continue
        want = decide[scenario]
        button = next(b for b in message["buttons"] if b["data"].startswith(want + ":"))
        body = json.dumps({"message_id": message["message_id"], "data": button["data"]}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/tap", data=body, method="POST",
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            answer = json.load(r)
        tapped.add(message["message_id"])
        print(f"tap {scenario} -> {'approve' if want == 'g' else 'reject'} {answer}", file=log, flush=True)
    time.sleep(0.5)
PY
wait "$agent_pid" || true
sed 's/^/    /' "$scratch/taps.log"
grep -E '^(->|   OUTCOME)' "$scratch/agent.out" | sed 's/^/    /' || true

outcome() { grep -E "OUTCOME $1: " "$scratch/agent.out" | tail -1 | sed "s/.*OUTCOME $1: //"; }
[ "$(outcome read)" = "ALLOWED" ] && ok "read ran without a prompt" || no "read: $(outcome read)"
case "$(outcome push-main)" in BLOCKED:*) ok "push to main was rejected by the approver" ;; *) no "push-main: $(outcome push-main)" ;; esac
[ "$(outcome push-branch)" = "ALLOWED" ] && ok "branch push was approved" || no "push-branch: $(outcome push-branch)"
case "$(outcome force-push)" in BLOCKED:*) ok "force push was rejected by the approver" ;; *) no "force-push: $(outcome force-push)" ;; esac

say "the log, read the way the judge reads it (tenant credential, /log/follow)"
tenant_token="$(cat "$here/.state/tenant_token")"
curl -fsS -H "X-Approval-Authorization: Bearer ${tenant_token}" \
  "http://127.0.0.1:${facade_port}/log/follow?from=0&limit=1000" >"$scratch/log.json"
unset tenant_token

say "judge advisories and state"
# Give the judge a moment to follow the last decisions.
sleep 3
chat >"$scratch/chat.json"
"${compose[@]}" exec -T service cat /state/judge-state.json >"$scratch/state.json"
python3 - "$scratch/log.json" "$scratch/chat.json" "$scratch/state.json" >"$scratch/checks.txt" <<'PY'
import json, sys
log = json.load(open(sys.argv[1]))["records"]
chat = json.load(open(sys.argv[2]))["messages"]
state = json.load(open(sys.argv[3]))
requests = [r for r in log if r["event"] == "approval.requested"]
decided = {r["action_key"]: r["event"] for r in log if r["event"] in ("approval.granted", "approval.rejected")}
advisories = [m for m in chat if m["text"].startswith("Judge (advisory, AI)")]
by_class = {}
for m in advisories:
    klass = m["text"].rsplit("Request class: ", 1)[-1]
    by_class[klass] = by_class.get(klass, 0) + 1
req_class = {}
for r in requests:
    req_class[r["payload"]["class"]] = req_class.get(r["payload"]["class"], 0) + 1
print("requests", len(requests))
print("advisories", len(advisories))
print("per_class_match", by_class == req_class, json.dumps(by_class, sort_keys=True))
print("judged_all", all(r["action_key"] in state["judged"] for r in requests))
print("verdicts", json.dumps(sorted({v.get("decision") for v in state["judged"].values()})))
print("decisions_in_state", sum(1 for k in decided if k in state["decisions"]), len(decided))
print("feedback", json.dumps(sorted({d["feedback"] for d in state["decisions"].values()})))
print("granted", sum(1 for e in decided.values() if e == "approval.granted"))
PY
sed 's/^/    /' "$scratch/checks.txt"
val() { grep "^$1 " "$scratch/checks.txt" | cut -d' ' -f2-; }
requests="$(val requests)"; advisories="$(val advisories)"
[ "$requests" -ge 3 ] && ok "${requests} manual requests reached the log" || no "only ${requests} manual requests in the log"
[ "$advisories" = "$requests" ] && ok "exactly one judge advisory per manual request (${advisories})" || no "advisories ${advisories} vs requests ${requests}"
[ "$(val per_class_match | cut -d' ' -f1)" = "True" ] && ok "advisories match requests class by class" || no "advisory classes: $(val per_class_match)"
[ "$(val judged_all)" = "True" ] && ok "every request is in the judge's ledger" || no "a request is missing from the judge's ledger"
decisions="$(val decisions_in_state)"
[ "${decisions% *}" = "${decisions#* }" ] && [ "${decisions% *}" -ge 3 ] && ok "all ${decisions% *} human decisions reached the judge's state" || no "decisions in state: ${decisions}"
[ "$(val feedback)" = '["not-applicable"]' ] && ok "feedback recorded as not-applicable (offline: no Weave call)" || no "feedback states: $(val feedback)"
[ "$(val granted)" = "1" ] && ok "exactly one grant (the branch push)" || no "grants: $(val granted)"

say "operator console"
console="http://127.0.0.1:${console_port}"
jar="$scratch/cookies"
code="$(curl -s -o /dev/null -w '%{http_code}' "$console/")"
[ "$code" = "303" ] && ok "console / redirects to /login without a session" || no "console / without a session: HTTP ${code}"
curl -fsS -c "$jar" "$console/login" >"$scratch/login.html"
csrf="$(sed -n 's/.*name="csrf" value="\([^"]*\)".*/\1/p' "$scratch/login.html" | head -1)"
# The token goes to curl from its 0600 file, never on the command line.
code="$(curl -s -o /dev/null -w '%{http_code}' -b "$jar" -c "$jar" \
  --data-urlencode "token@$here/.state/console_token" --data-urlencode "csrf=${csrf}" "$console/login")"
curl -fsS -b "$jar" "$console/" >"$scratch/console.html" || true
if [ "$code" = "303" ] && grep -q "Live tenant view" "$scratch/console.html" \
  && grep -q 'chip agree' "$scratch/console.html" && grep -q 'chip escalated' "$scratch/console.html"; then
  ok "console / with the demo console token shows the live view, with agree and escalated labels"
else
  no "console live view (login HTTP ${code})"
fi
[ "$(curl -s -o /dev/null -w '%{http_code}' "$console/policy")" = "200" ] && ok "console /policy is public" || no "console /policy"

say "log integrity"
verify="$("${compose[@]}" exec -T -w /data/demo daemon sh -c 'node "$APPROVAL_CLI" log verify --json' || true)"
python3 -c 'import json,sys; b=json.loads(sys.argv[1]); sys.exit(0 if b.get("status")=="clean" else 1)' "$verify" \
  && ok "approval log verify: clean" || no "approval log verify: ${verify:0:200}"

say "secrets"
logs="$("${compose[@]}" logs --no-color 2>&1 || true)"
leaked=""
while IFS='=' read -r name value; do
  [ -n "$value" ] || continue
  secret="${value#*:}"  # bot tokens: only the part after the public id is secret
  if printf '%s' "$logs" | grep -qF -- "$secret"; then leaked="$leaked $name"; fi
done < <(cat "$here/.state/daemon.env" "$here/.state/judge.env" "$here/.state/agent.env")
[ -z "$leaked" ] && ok "no generated credential appears in any container's log" || no "leaked:${leaked}"

elapsed=$(($(date +%s) - started))
printf '\n== summary: %s passed, %s failed, %ss\n' "$pass" "$fail" "$elapsed"
[ "$fail" = "0" ]
