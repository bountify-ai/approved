#!/usr/bin/env python3
"""tryit-smoke: the try-it image, headless, asserted end to end through its public port only.

Phase 1, live reviewer configured but unreachable (every W&B and inference URL points at a
dead loopback port, so nothing leaves the machine), live cap of one run, two runs an hour:
  health; only $PORT listens off loopback; embed headers; no cookie anywhere; forbidden routes
  404; run 1 LIVE (a second press is refused; the judge is ABSENT and the taps still decide:
  reject, approve, reject); run 2 falls back to the OFFLINE reviewer because the cap is spent
  (the page says so; advisories arrive; reject, approve, reject); a third run is rate-limited;
  chain verified and `approval log verify` clean; Reset clears the view and leaves the log
  alone; no generated credential in any response or container log.
Phase 2, after an UNCLEAN kill, on the same /data, offline, with a 30-second approval window:
  healthy again; the log continues and verifies; a run nobody taps ends by the gate's own
  expiry and is shown as expired.

Needs docker, python3. Removes only the container and volume it names (never a prune).
KEEP=1 leaves the phase-2 container running. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
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
#: Maritime re-launches the image command with its own PATH; this one has no /usr/local/bin
#: and no venv, so any child found through PATH would fail to start.
MARITIME_LIKE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
FAKE_WANDB_KEY = "smoke-not-a-real-wandb-key-5f1e0c"
DEAD = "http://127.0.0.1:9"
EXPECTED_ANCESTORS = "frame-ancestors 'self' https://approval.md"
DECIDE = {"push-main": "r", "push-branch": "g", "force-push": "r"}
FORBIDDEN = [
    "/log/follow?from=0",
    "/export",
    "/status",
    "/verbs",
    "/verb/queue",
    "/hook/hermes",
    "/telegram/webhook",
    "/schedules",
    "/metrics",
    "/login",
    "/connect",
    "/policy",
    "/partials/live",
    "/downloads/hermes-hook-shim.sh",
    "/static/console.js",
    "/api/chat",
    "/api/tap",
    "/approver/healthz",
    "/approver/api/../../log/follow",
    "/approver/bot7001:x/sendMessage",
    "/healthz",
]

started = time.monotonic()
passed = failed = 0
responses: list[tuple[str, int, dict[str, str], bytes]] = []


def say(text: str) -> None:
    print(f"\n== {text} (t+{int(time.monotonic() - started)}s)", flush=True)


def check(ok: bool, text: str) -> bool:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {text}", flush=True)
    else:
        failed += 1
        print(f"  FAIL  {text}", flush=True)
    return ok


def docker(*args: str, check_rc: bool = True, capture: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=capture, text=True, check=False)  # noqa: S603, S607
    if check_rc and result.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()[:300]}")
    return result.stdout if capture else ""


def remove_ours() -> None:
    docker("rm", "-f", NAME, check_rc=False)


def run_container(env: dict[str, str]) -> None:
    args = ["run", "-d", "--name", NAME, "-p", f"127.0.0.1:{HOST_PORT}:{PORT}", "-v", f"{VOLUME}:/data"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    docker(*args, IMAGE)


def request(
    method: str, path: str, body: bytes | None = None, ctype: str | None = "application/json"
) -> tuple[int, dict[str, str], bytes]:
    headers = {}
    if body is not None and ctype:
        headers["content-type"] = ctype
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=20) as r:
            status, hdrs, raw = r.status, dict(r.headers.items()), r.read()
    except urllib.error.HTTPError as exc:
        status, hdrs, raw = exc.code, dict(exc.headers.items()), exc.read()
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return 0, {}, b""
    responses.append((f"{method} {path}", status, hdrs, raw))
    return status, hdrs, raw


def get_json(path: str) -> Any:
    status, _h, raw = request("GET", path)
    return json.loads(raw) if status == 200 else None


def post(path: str, payload: Any = None, ctype: str | None = "application/json") -> tuple[int, dict[str, Any], dict[str, str]]:
    status, hdrs, raw = request("POST", path, json.dumps(payload or {}).encode(), ctype)
    try:
        body = json.loads(raw)
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}, hdrs


def wait_health(limit_s: float) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit_s:
        status, _h, _b = request("GET", "/health")
        if status == 200:
            return time.monotonic() - t0
        time.sleep(0.5)
    return None


def state() -> dict[str, Any]:
    return get_json("/api/state") or {}


def scenario(s: dict[str, Any], name: str) -> dict[str, Any]:
    return next(x for x in (s.get("run") or {}).get("scenarios", []) if x["name"] == name)


def prompt_for(run_id: str, name: str) -> tuple[int, dict[str, str]] | None:
    """The button message of this run's prompt for ``name``: the gate's APPROVAL REQUIRED
    header names hook:demo-<run>-<scenario>:..., and the button message follows it."""
    chat = get_json("/approver/api/chat") or {"messages": []}
    marker = f"hook:demo-{run_id}-{name}:"
    header_seen = False
    for message in chat["messages"]:
        text = message["text"]
        if "APPROVAL REQUIRED" in text:
            header_seen = marker in text
        if message["buttons"] and (header_seen or marker in text):
            return message["message_id"], {b["data"][0]: b["data"] for b in message["buttons"]}
    return None


def wait_for(fn, limit_s: float, every: float = 1.0):  # type: ignore[no-untyped-def]
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit_s:
        value = fn()
        if value:
            return value
        time.sleep(every)
    return None


def wait_run_done(limit_s: float) -> dict[str, Any]:
    return wait_for(lambda: (s := state()).get("run", {}) and s["run"]["state"] == "done" and s, limit_s) or {}


def drive_run(expect_judge: str) -> dict[str, Any]:
    """Tap this run's three prompts through the front once the judge has had its say."""
    s = state()
    run_id = s["run"]["id"]
    for name, want in DECIDE.items():
        found = wait_for(lambda n=name: prompt_for(run_id, n), 120)
        if not check(found is not None, f"{name}: the gate's prompt reached the approver chat"):
            return {}
        message_id, buttons = found

        def judged(n: str = name) -> dict[str, Any] | None:
            info = scenario(state(), n).get("judge") or {}
            return info if info.get("state") in ("verdict", "absent") else None

        info = wait_for(judged, 60) or {}
        if expect_judge == "absent":
            check(
                info.get("state") == "absent" and "reviewer" in str(info.get("reason")),
                f"{name}: live reviewer unreachable, the page shows 'judge absent' ({info.get('reason')})",
            )
        else:
            check(info.get("state") == "verdict", f"{name}: offline advisory {info.get('verdict')} before the tap")
        if name == "push-main":
            status, body, _h = post("/approver/api/tap", {"message_id": message_id, "data": "g:forged:0"})
            check(status == 403 and body.get("code") == "not-a-button", "a forged button is refused (403 not-a-button)")
        status, body, _h = post("/approver/api/tap", {"message_id": message_id, "data": buttons[want]})
        check(status == 200 and body.get("ok") is True, f"{name}: {'approve' if want == 'g' else 'reject'} tap delivered through the front")
        if name == "push-main":
            status, body, _h = post("/approver/api/tap", {"message_id": message_id, "data": buttons[want]})
            check(status == 409, "a second tap on the same prompt is refused (409)")
    return wait_run_done(120)


def assert_outcomes(s: dict[str, Any], label: str) -> None:
    got = {x["name"]: x["status"] for x in s.get("run", {}).get("scenarios", [])}
    want = {"read": "allowed", "push-main": "rejected", "push-branch": "allowed", "force-push": "rejected"}
    check(got == want, f"{label}: outcomes read ALLOWED, push-main REJECTED, push-branch ALLOWED, force-push REJECTED ({got})")


def secrets_of_container() -> list[str]:
    out = docker(
        "exec", NAME, "sh", "-c",
        "for f in /data/tryit/secrets/*; do cat \"$f\"; echo; done",
    )
    values = [line.strip() for line in out.splitlines() if line.strip()]
    # Bot tokens: only the part after the public bot id is secret.
    return [v.split(":", 1)[1] if re.match(r"^\d+:", v) else v for v in values]


def assert_no_secret_leaks(label: str, extra: list[str]) -> None:
    secrets = secrets_of_container() + extra
    check(len(secrets) >= 5, f"{label}: read {len(secrets)} generated credential values to look for")
    leaked = []
    for what, _status, hdrs, raw in responses:
        blob = raw.decode("utf-8", "replace") + json.dumps(hdrs)
        leaked += [what for s in secrets if s in blob]
    check(not leaked, f"{label}: no credential in any of {len(responses)} responses (bodies and headers)")
    logs = subprocess.run(["docker", "logs", NAME], capture_output=True, text=True, check=False)  # noqa: S603, S607
    text = logs.stdout + logs.stderr
    check(not [s for s in secrets if s in text], f"{label}: no credential in the container's log")
    modes = docker("exec", NAME, "sh", "-c", "stat -c '%a %n' /data/tryit/secrets /data/tryit/secrets/*").split("\n")
    modes = [m for m in modes if m]
    check(
        modes[0].startswith("700 ") and all(m.startswith("600 ") for m in modes[1:]),
        f"{label}: credential directory 0700, files 0600",
    )


def listeners() -> list[tuple[str, int]]:
    out = docker("exec", NAME, "sh", "-c", "cat /proc/net/tcp /proc/net/tcp6 2>/dev/null")
    found = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 3 and parts[3] == "0A":
            addr, port = parts[1].rsplit(":", 1)
            found.append((addr, int(port, 16)))
    return found


def events_lines() -> int:
    return int(docker("exec", NAME, "sh", "-c", "wc -l < /data/demo/.approval/log/events.jsonl").strip())


def phase_one() -> None:
    say("phase 1: start (live reviewer configured, unreachable; Maritime-like PATH)")
    run_container(
        {
            "PORT": str(PORT),
            "PATH": MARITIME_LIKE_PATH,
            "TRYIT_LIVE": "1",
            "WANDB_API_KEY": FAKE_WANDB_KEY,
            "REVIEWER_MODEL": "smoke/unreachable-model",
            "WANDB_BASE_URL": DEAD,
            "INFERENCE_BASE_URL": f"{DEAD}/v1",
            "JUDGE_TIMEOUT_S": "5",
            "TRYIT_LIVE_DAILY_CAP": "4",
            "TRYIT_RUN_MIN_INTERVAL_S": "0",
            "TRYIT_RUNS_PER_HOUR": "2",
        }
    )
    boot = wait_health(180)
    check(boot is not None, f"/health 200 with all four parts up (after {boot and round(boot, 1)}s)")
    if boot is None:
        return

    say("surface")
    public = [(a, p) for a, p in listeners() if a not in ("0100007F", "00000000000000000000000001000000")]
    check(public == [("00000000", PORT)], f"only the front listens off loopback ({public})")
    status, hdrs, raw = request("GET", "/")
    check(status == 200 and b"Run the agent" in raw, "the page renders with no cookie sent")
    check(hdrs.get("Content-Security-Policy", "").endswith(EXPECTED_ANCESTORS), f"page CSP ends with {EXPECTED_ANCESTORS!r}")
    check("X-Frame-Options" not in hdrs, "page carries no X-Frame-Options")
    urls = re.findall(rb'(?:src|href|action)="([^"]*)"', raw)
    check(all(not u.startswith((b"/", b"http")) for u in urls), "the page's own URLs are all relative")
    status, hdrs, _raw = request("GET", "/approver/")
    check(status == 200 and hdrs.get("Content-Security-Policy", "").endswith(EXPECTED_ANCESTORS), "approver chat page embeddable by the same origins only")
    _s, hdrs, _raw = request("GET", "/api/state")
    check(hdrs.get("X-Frame-Options") == "DENY" and "frame-ancestors 'none'" in hdrs.get("Content-Security-Policy", ""), "JSON routes are not frameable")
    bad = [p for p in FORBIDDEN for m in ("GET", "POST") if request(m, p, b"{}" if m == "POST" else None)[0] != 404]
    check(not bad, f"{len(FORBIDDEN)} forbidden paths answer 404 on GET and POST ({bad})")
    check(request("GET", "/chat")[0] == 405 and post("/chat", {"message": "hi"})[0] == 200, "platform /chat: POST answers a fixed refusal, GET is 405")
    check(post("/api/run", {}, ctype="text/plain")[0] == 415, "a non-JSON POST is refused (415)")
    status, _h, _b = request("POST", "/approver/api/tap", b"{" + b" " * 4096 + b"}")
    check(status == 413, "an oversized tap body is refused before it is read (413)")
    s = state()
    check(s["reviewer"]["mode"] == "live", f"reviewer before run 1: {s['reviewer']['label']}")
    check("advisory" in s["reviewer"]["advisory_note"], "the page says the judge is advisory")
    check(s["shared_note"] == "One shared demo tenant; other visitors may be tapping too.", "the page states the shared-tenant model")

    say("run 1: live reviewer (unreachable): the judge is absent and the taps still decide")
    status, body, _h = post("/api/run")
    check(status == 202, f"Run the agent: {status} {body.get('message')}")
    status, body, _h = post("/api/run")
    check(status == 409 and body.get("code") == "run-in-progress", "a second concurrent run is refused (409 run-in-progress)")
    check(post("/api/reset")[0] == 409, "Reset is refused while a run is in progress")
    run1 = state()["run"]
    check(run1["reviewer"] == "live", "run 1 is judged by the live reviewer")
    s = drive_run("absent")
    assert_outcomes(s, "run 1")
    floor_msg = max((m["message_id"] for m in (get_json("/approver/api/chat") or {"messages": []})["messages"]), default=0)

    say("run 2: the live cap is spent, so the offline reviewer takes over and says so")
    s = state()
    check(s["reviewer"]["mode"] == "offline" and s["reviewer"]["fallback"] == "daily-cap", f"page label: {s['reviewer']['label']}")
    check("cap" in s["reviewer"]["label"], "the label says the live cap is reached")
    status, body, _h = post("/api/run")
    check(status == 202, f"run 2 starts: {status}")
    time.sleep(2)
    run2 = state()["run"]
    check(run2["reviewer"] == "offline" and run2["fallback"] == "daily-cap", "run 2 fell back to the offline reviewer (daily-cap)")
    status, body, _h = post("/approver/api/tap", {"message_id": floor_msg, "data": "g:x"})
    check(status == 403, f"a tap on a message from before this run is refused ({status} {body.get('code')})")
    s = drive_run("verdict")
    assert_outcomes(s, "run 2")
    verdicts = {x["name"]: (x.get("judge") or {}).get("verdict") for x in s["run"]["scenarios"]}
    check(
        verdicts == {"read": None, "push-main": "NEEDS_HUMAN", "push-branch": "READY", "force-push": "NEEDS_HUMAN"},
        f"offline advisories beside each prompt ({verdicts})",
    )

    say("rate limit, record, reset")
    status, body, hdrs = post("/api/run")
    check(status == 429 and body.get("code") == "rate-limited" and "Retry-After" in hdrs, f"a third run in the hour is rate-limited ({status}, retry in {body.get('retry_after_s')}s)")
    rec = wait_for(lambda: (r := state()["record"])["log_verify"].get("status") == "clean" and r["log_verify"].get("records", 0) >= 20 and r, 60) or state()["record"]
    check(rec["judge_chain"] == "verified", "the judge's follow of the log: chain verified")
    check(rec["log_verify"].get("status") == "clean", f"approval log verify: clean ({rec['log_verify'].get('records')} records)")
    counted = wait_for(lambda: (c := state()["record"]["counters"])["escalated"] >= 2 and c["agree"] >= 1 and c, 30)
    c = counted or state()["record"]["counters"]
    check(c["agree"] >= 1 and c["escalated"] >= 2 and c["false_ready"] == 0, f"judge vs human: agree {c['agree']}, escalated {c['escalated']}, false READY {c['false_ready']}")
    before = events_lines()
    status, body, _h = post("/api/reset")
    check(status == 200, f"Reset after the run: {body.get('message')}")
    chat = get_json("/approver/api/chat") or {}
    check(chat.get("messages") == [], "Reset cleared the chat view")
    check(events_lines() == before, f"Reset left the log alone ({before} lines before and after)")
    check(state()["run"] is None, "Reset cleared the run panel")
    check(not [r for r in responses if "Set-Cookie" in r[2] or "set-cookie" in r[2]], f"no Set-Cookie on any of {len(responses)} responses")

    say("credentials and keys")
    assert_no_secret_leaks("phase 1", [FAKE_WANDB_KEY])
    holders = docker(
        "exec", NAME, "sh", "-c",
        f"for p in /proc/[0-9]*; do if grep -qa '{FAKE_WANDB_KEY}' $p/environ 2>/dev/null; then tr '\\0' ' ' < $p/cmdline; echo; fi; done",
    ).splitlines()
    others = [
        h for h in holders
        if h.strip() and "for p in /proc" not in h and "approved.tryit" not in h and "approved serve" not in h
    ]
    check(not others, f"the W&B key is in no process environment but the supervisor's and the judge's ({others})")


def phase_two() -> None:
    say("phase 2: unclean kill, cold start on the same /data (offline, 30 s approval window)")
    records_before = state().get("record", {}).get("log_verify", {}).get("records", 0)
    docker("kill", "-s", "KILL", NAME, check_rc=False)
    remove_ours()
    responses.clear()
    run_container(
        {
            "PORT": str(PORT),
            "PATH": MARITIME_LIKE_PATH,
            "TRYIT_HOOK_HARNESS_CAP_S": "90",
        }
    )
    boot = wait_health(180)
    check(boot is not None, f"healthy again after an unclean kill (after {boot and round(boot, 1)}s)")
    if boot is None:
        return
    rec = wait_for(lambda: (r := state()["record"])["log_verify"].get("status") == "clean" and r, 60) or {}
    check(rec.get("log_verify", {}).get("records", 0) >= records_before, f"the log continued across the restart ({records_before} -> {rec.get('log_verify', {}).get('records')})")
    check(state()["window_s"] == 30, "approval window is 30 s in this phase")

    say("run 3: nobody taps; the gate's own expiry ends the run")
    status, _b, _h = post("/api/run")
    check(status == 202, f"run 3 starts: {status}")
    s = wait_run_done(240)
    got = {x["name"]: x["status"] for x in s.get("run", {}).get("scenarios", [])}
    check(got == {"read": "allowed", "push-main": "expired", "push-branch": "not-run", "force-push": "not-run"}, f"expired and ended: {got}")
    check("expired" in str(s.get("run", {}).get("end_reason")), f"the page says why: {s.get('run', {}).get('end_reason')}")
    def expired_records() -> int:
        out = docker("exec", NAME, "sh", "-c", "grep -c 'approval.expired' /data/demo/.approval/log/events.jsonl || true")
        return int(out.strip() or 0)

    # serve refuses with hook-expired once the window lapses; the daemon's sweep (every 30 s)
    # appends the approval.expired record.
    expired = wait_for(expired_records, 60, every=2) or 0
    check(expired >= 1, f"the daemon's sweep recorded approval.expired ({expired})")
    rec = wait_for(lambda: (r := state()["record"])["log_verify"].get("status") == "clean" and r, 60) or {}
    check(rec.get("log_verify", {}).get("status") == "clean", f"approval log verify after the expiry: clean ({rec.get('log_verify', {}).get('records')} records)")
    assert_no_secret_leaks("phase 2", [])


def main() -> int:
    remove_ours()
    docker("volume", "rm", VOLUME, check_rc=False)
    try:
        phase_one()
        phase_two()
    except Exception as exc:  # noqa: BLE001 - report, clean up, fail
        check(False, f"smoke aborted: {type(exc).__name__}: {exc}")
    finally:
        if failed:
            print("-- last container log lines (for diagnosis) --")
            logs = subprocess.run(["docker", "logs", "--tail", "40", NAME], capture_output=True, text=True, check=False)  # noqa: S603, S607
            print((logs.stdout + logs.stderr)[-6000:])
        if os.environ.get("KEEP") == "1":
            print(f"KEEP=1: {NAME} left running on {BASE}")
        else:
            remove_ours()
            docker("volume", "rm", VOLUME, check_rc=False)
    print(f"\n== summary: {passed} passed, {failed} failed, {int(time.monotonic() - started)}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
