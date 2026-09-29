"""A fake ``maritime`` CLI for tests: records every argv and stdin, answers from a JSON state.

Installed on PATH as ``maritime`` by the ``fake_maritime`` fixture. It never touches a
network. State (``FAKE_MARITIME_STATE``): ``agents`` (list), ``remote_policy_sha``,
``ask`` ("allowed" | "blocked" | "timeout" | "failed"), ``verify`` (the log verify JSON), and
``env`` (agent -> imported KEY names; values are never recorded).
Log (``FAKE_MARITIME_LOG``): one JSON object per call: ``{"argv": [...], "stdin": "..."}``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_MARITIME_STATE"])
log_path = Path(os.environ["FAKE_MARITIME_LOG"])
argv = sys.argv[1:]
stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
with log_path.open("a") as log:
    log.write(json.dumps({"argv": argv, "stdin": stdin}) + "\n")
state = json.loads(state_path.read_text())


def save() -> None:
    state_path.write_text(json.dumps(state))


def ok(value: object) -> None:
    print(json.dumps(value))
    save()
    sys.exit(0)


def fail(code: int, error: str) -> None:
    sys.stderr.write(json.dumps({"ok": False, "error": {"code": error, "message": error}}))
    sys.exit(code)


def reply(content: str) -> str:
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    return json.dumps({"http": 200, "body": json.dumps(body)})


# Option lists copied from `maritime <cmd> --help` (maritime-cli 1.7.0). An option the real
# CLI does not have is a usage error (exit 4), so a test proves we pass only real flags.
GLOBAL = {"--json", "--verbose"}
OPTIONS = {
    "create": {
        "-t",
        "--template",
        "-e",
        "--env",
        "--count",
        "-r",
        "--repo",
        "-b",
        "--branch",
        "--public",
        "--port",
    },
    "env import": {"--plain", "-r", "--reload"},
    "exec": set(),
    "stop": set(),
    "start": set(),
    "status": set(),
    "list": set(),
}
while argv and argv[0] in GLOBAL:
    argv.pop(0)
if not argv:
    fail(4, "usage")
cmd = argv[0]
key = "env import" if argv[:2] == ["env", "import"] else cmd
if key not in OPTIONS:
    fail(4, f"unknown command {key}")
rest = argv[2:] if key == "env import" else argv[1:]
if key == "exec":
    rest = rest[: rest.index("--")] if "--" in rest else rest
    # `--json` after `exec` is accepted by the real CLI although exec's own --help does not
    # list it (a global option, parsed anywhere): Carter ran `maritime exec --json <agent> --
    # sh -c ...` against maritime-cli 1.7.0 on 2026-09-29 and got JSON back, and
    # approval-md-hosted's scripts/gate-placement/maritime-exec-adapter.mjs has used
    # `exec --json MACHINE -- argv` since its PR #17. The printed attest command uses it.
    rest = [t for t in rest if t != "--json"]
in_env_pairs = False
for token in rest:
    if token.startswith("-"):
        if token not in OPTIONS[key]:
            fail(4, f"unknown option {token} for {key}")
        in_env_pairs = token in ("-e", "--env")
    elif in_env_pairs and "=" not in token:
        fail(4, f"-e expects KEY=value, got {token}")
agents = state.setdefault("agents", [])
find = {a["name"]: a for a in agents}
find.update({a["id"]: a for a in agents if "id" in a})

if cmd == "list":
    ok(agents)
if cmd == "create":
    name = argv[1]
    if name in find:
        fail(1, "name_taken")
    agent = {"id": f"id-{name}", "name": name, "status": "deploying"}
    agents.append(agent)
    ok(agent)
if cmd == "env" and argv[1] == "import":
    agent, path = argv[2], argv[3]
    keys = [line.split("=", 1)[0] for line in Path(path).read_text().splitlines() if "=" in line]
    state.setdefault("env", {}).setdefault(agent, []).extend(keys)
    ok({"ok": True, "uploaded": keys, "failed": [], "reloaded": False})
if cmd in ("stop", "start"):
    if argv[1] not in find:
        fail(3, "not_found")
    ok({"ok": True})
if cmd == "status":
    if argv[1] not in find:
        fail(3, "not_found")
    ok({**find[argv[1]], "status": "running"})
if cmd == "exec":
    script = argv[argv.index("--") + 3]
    out = ""
    written = re.search(r"printf %s '([A-Za-z0-9+/=]+)' \| base64 -d", script)
    if written:
        sha = hashlib.sha256(base64.b64decode(written.group(1))).hexdigest()
        state["remote_policy_sha"] = sha
        out = f"{sha}  /data/x/APPROVAL.md\n"
    elif "sha256sum" in script:
        sha = state.get("remote_policy_sha")
        out = f"{sha}  /data/x/APPROVAL.md\n" if sha else ""
    elif "queue --json" in script:
        out = json.dumps(state.get("queue", {"ok": True, "pending": []}))
    elif "log verify" in script:
        out = json.dumps(state.get("verify", {"status": "clean", "records": 0}))
    elif "setsid" in script:
        out = "4242\n"
    elif "/status; then cat" in script:
        outcome = state.get("ask", "allowed")
        out = "pending\n" if outcome == "timeout" else ("13\n" if outcome == "failed" else "0\n")
    elif script.startswith("cat /tmp/approved-ask-"):
        outcome = state.get("ask", "allowed")
        if outcome == "blocked":
            out = reply(
                "I tried to run `git push origin main` but it was blocked: hook-timeout, "
                "the approval facade says it is waiting for a human."
            )
        elif outcome == "failed":
            out = json.dumps({"http": 500, "body": "{}"})
        else:
            out = reply("Done: the README lists three services.")
    ok({"exit_code": 0, "stdout": out, "stderr": ""})
fail(4, "usage")
