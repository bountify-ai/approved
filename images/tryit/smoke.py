#!/usr/bin/env python3
"""Exercise two private sessions through the public port and the real pinned gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

IMAGE = os.environ.get("TRYIT_IMAGE", "approved-tryit:local")
NAME = os.environ.get("TRYIT_SMOKE_NAME", "approved-tryit-smoke")
VOLUME = f"{NAME}-data"
HOST_PORT = int(os.environ.get("TRYIT_SMOKE_PORT", "18799"))
PORT = 18789
BASE = f"http://127.0.0.1:{HOST_PORT}"
GATE = "smoke-gateway-credential-0123456789-0123456789"
DEAD = "http://127.0.0.1:9"
FAILURES: list[str] = []


def docker(*args: str, required: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    if required and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr[-500:]}")
    return result.stdout


def check(condition: bool, label: str) -> None:
    print(f"{'PASS' if condition else 'FAIL'} {label}", flush=True)
    if not condition:
        FAILURES.append(label)


def request(
    method: str, path: str, token: str = "", body: dict[str, Any] | None = None, gate: str = GATE
) -> tuple[int, dict[str, Any], dict[str, str]]:
    headers: dict[str, str] = {}
    if path != "/health":
        headers["X-Approved-Gateway"] = gate
    if token:
        headers["X-Approved-Session"] = token
    data = None
    if method == "POST":
        headers["Content-Type"] = "application/json"
        data = json.dumps(body or {}).encode()
    req = urllib.request.Request(  # noqa: S310 - fixed loopback URL
        BASE + path, data=data, method=method, headers=headers
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=25) as response:
            status, raw, hdrs = response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        status, raw, hdrs = exc.code, exc.read(), dict(exc.headers.items())
    except (OSError, TimeoutError):
        return 0, {}, {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}, hdrs


def wait_for(fn: Any, seconds: float, gap: float = 0.8) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        answer = fn()
        if answer:
            return answer
        time.sleep(gap)
    return None


def state(token: str) -> dict[str, Any]:
    return request("GET", "/api/state", token)[1]


def prompt(token: str, run_id: str, name: str) -> tuple[int, dict[str, str]] | None:
    messages = request("GET", "/approver/api/chat", token)[1].get("messages", [])
    marker = f"hook:demo-{run_id}-{name}:"
    seen = False
    for message in messages:
        text = message.get("text", "")
        if "APPROVAL REQUIRED" in text:
            seen = marker in text
        buttons = message.get("buttons", [])
        if buttons and (seen or marker in text):
            return message["message_id"], {b["data"][0]: b["data"] for b in buttons}
    return None


def main() -> int:
    docker("rm", "-f", NAME, required=False)
    docker("volume", "rm", VOLUME, required=False)
    env = {
        "PORT": str(PORT),
        "TRYIT_GATEWAY_SECRET": GATE,
        "TRYIT_LIVE": "1",
        "WANDB_API_KEY": "smoke-not-a-real-wandb-key",
        "REVIEWER_MODEL": "smoke/unreachable",
        "WANDB_BASE_URL": DEAD,
        "INFERENCE_BASE_URL": DEAD + "/v1",
        "JUDGE_TIMEOUT_S": "3",
        "TRYIT_HOOK_HARNESS_CAP_S": "180",
        "TRYIT_RUN_MIN_INTERVAL_S": "0",
        "TRYIT_LIVE_LIFETIME_CAP": "80",
        "TRYIT_LIVE_DAILY_CAP": "80",
        "TRYIT_LIVE_CALLS_PER_HOUR": "20",
    }
    command = [
        "run",
        "-d",
        "--name",
        NAME,
        "--memory",
        "2g",
        "--cpus",
        "1",
        "-p",
        f"127.0.0.1:{HOST_PORT}:{PORT}",
        "-v",
        f"{VOLUME}:/data",
    ]
    for key, value in env.items():
        command += ["-e", f"{key}={value}"]
    try:
        docker(*command, IMAGE)
        check(wait_for(lambda: request("GET", "/health")[0] == 200, 45), "front health")
        check(
            request("POST", "/api/session", gate="wrong")[0] == 403,
            "direct shared-origin request cannot create session",
        )
        check(request("GET", "/")[0] == 200, "gateway-authenticated static page")
        status1, first, headers = request("POST", "/api/session")
        status2, second, _ = request("POST", "/api/session")
        check(status1 == status2 == 202, "two sessions reserved")
        check(headers.get("Cache-Control") == "no-store", "capability response is uncached")
        token1, token2 = first.get("session_token", ""), second.get("session_token", "")
        check(bool(token1 and token2 and token1 != token2), "unique private capabilities")
        check(request("POST", "/api/session")[0] == 429, "third session refused at capacity")
        check(request("GET", "/api/state")[0] == 401, "missing capability refused")
        check(
            request("GET", "/approver/api/chat", token1, gate="wrong")[0] == 403,
            "gateway credential required for chat",
        )
        ready1 = wait_for(lambda: state(token1).get("session", {}).get("state") == "ready", 180)
        ready2 = wait_for(lambda: state(token2).get("session", {}).get("state") == "ready", 180)
        check(bool(ready1 and ready2), "both real runtimes provisioned")
        if not ready1 or not ready2:
            return 1
        check(
            state(token1).get("run") is None and state(token2).get("run") is None,
            "both sessions begin with empty runs",
        )
        status, _, _ = request("POST", "/api/run", token1)
        check(status == 202, "scripted agent started against real core")
        check(state(token2).get("run") is None, "another visitor cannot see the run")
        run = wait_for(lambda: state(token1).get("run"), 15)
        if not run:
            check(False, "run appears in state")
            return 1
        run_id = run["id"]
        for name, action in (("push-main", "r"), ("push-branch", "g"), ("force-push", "r")):
            found = wait_for(lambda n=name: prompt(token1, run_id, n), 120)
            check(found is not None, f"{name} prompt from real daemon")
            if found is None:
                break
            message_id, buttons = found
            check(
                request(
                    "POST",
                    "/approver/api/tap",
                    token2,
                    {"message_id": message_id, "data": buttons[action]},
                )[0]
                == 409,
                "other session cannot tap this prompt",
            )
            check(
                request(
                    "POST",
                    "/approver/api/tap",
                    token1,
                    {"message_id": message_id, "data": buttons[action]},
                )[0]
                == 200,
                f"{name} human decision delivered",
            )
        done = wait_for(
            lambda: (s := state(token1)).get("run", {}).get("state") == "done" and s, 120
        )
        check(bool(done), "agent run completed")
        if done:
            got = {x["name"]: x["status"] for x in done["run"]["scenarios"]}
            check(
                got
                == {
                    "read": "allowed",
                    "push-main": "rejected",
                    "push-branch": "allowed",
                    "force-push": "rejected",
                },
                f"real gate outcomes: {got}",
            )
            verified = wait_for(
                lambda: (
                    (s := state(token1)).get("record", {}).get("log_verify", {}).get("status")
                    == "clean"
                    and s
                ),
                30,
            )
            check(bool(verified), "append-only log verifies clean")
        check(state(token2).get("run") is None, "second visitor still has no run")
        output = docker(
            "exec", NAME, "sh", "-c", "find /data/tryit/sessions -name events.jsonl -type f | wc -l"
        )
        check(int(output.strip()) == 2, "two disjoint persistent approval logs")
        # A machine restart reuses the same fake Bot API port and bot id for a new
        # visitor while preserving the old session's store. Core's bot registry
        # must live under the session, or the next webhook refuses this slot.
        docker("restart", NAME)
        check(bool(wait_for(lambda: request("GET", "/health")[0] == 200, 45)), "front restarted")
        status3, third, _ = request("POST", "/api/session")
        check(status3 == 202, "new session reuses a slot after restart")
        token3 = third.get("session_token", "")
        reused = wait_for(
            lambda: (
                (s := state(token3)).get("session", {}).get("state") == "ready"
                and s.get("health", {}).get("ok")
                and s
            ),
            180,
        )
        check(bool(reused), "reused slot starts a healthy real gate and approver webhook")
        if reused:
            check(request("POST", "/api/run", token3)[0] == 202, "reused slot starts a real run")
            fresh_run = wait_for(lambda: state(token3).get("run"), 15)
            check(bool(fresh_run), "reused slot run appears")
            if fresh_run:
                held = wait_for(lambda: prompt(token3, fresh_run["id"], "push-main"), 120)
                check(held is not None, "reused slot receives a real approval prompt")
                if held is not None:
                    message_id, buttons = held
                    check(
                        request(
                            "POST",
                            "/approver/api/tap",
                            token3,
                            {"message_id": message_id, "data": buttons["r"]},
                        )[0]
                        == 200,
                        "reused slot delivers the human rejection",
                    )
                    decided = wait_for(
                        lambda: next(
                            (
                                item
                                for item in state(token3).get("run", {}).get("scenarios", [])
                                if item.get("name") == "push-main"
                                and item.get("status") == "rejected"
                            ),
                            None,
                        ),
                        30,
                    )
                    check(bool(decided), "reused slot records the real gate decision")
    except Exception as exc:  # noqa: BLE001
        check(False, f"smoke error: {type(exc).__name__}: {exc}")
    finally:
        if FAILURES:
            print(docker("logs", "--tail", "80", NAME, required=False)[-8000:])
        if os.environ.get("KEEP") != "1":
            docker("rm", "-f", NAME, required=False)
            docker("volume", "rm", VOLUME, required=False)
    print(f"{len(FAILURES)} smoke failures", flush=True)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
