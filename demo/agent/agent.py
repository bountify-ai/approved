"""Scripted demo agent: Hermes-shaped hook calls against the approval.md facade.

Behaves the way approval-md-hosted ``hermes-image/hermes-hook-shim.sh`` does: POST the
``pre_tool_call`` envelope to ``/hook/hermes`` with the AGENT credential in
``X-Approval-Authorization``, replay the answer, and while the facade answers a block naming
``hook-timeout`` (no decision yet; the question stays open and a re-post of the same bytes
adopts it) wait and post again, up to ``AGENT_WAIT_S``. Anything that is not a clean answer
is treated as a block (fail closed).

Standard library only. The agent credential comes from ``AGENT_TOKEN_FILE`` or
``AGENT_TOKEN``; it is never printed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCENARIOS: list[tuple[str, str, str]] = [
    ("read", "cat README.md", "a read: autonomous under the demo policy"),
    (
        "push-main",
        "git push origin main",
        "push to main: manual; the judge escalates it",
    ),
    (
        "push-branch",
        "git push origin feat/checkout-retry",
        "branch push: manual; judge READY",
    ),
    (
        "force-push",
        "git push --force origin main",
        "force push: rewrites shared history",
    ),
]

WITHDRAWN = re.compile(r"WAS WITHDRAWN")


def _token() -> str:
    path = os.environ.get("AGENT_TOKEN_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return os.environ["AGENT_TOKEN"].strip()


def _post(url: str, token: str, envelope: dict, timeout_s: float) -> tuple[int, dict | None]:
    request = urllib.request.Request(  # noqa: S310 - scheme checked in main(): http(s) only
        url,
        data=json.dumps(envelope).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Approval-Authorization": f"Bearer {token}",
        },
    )
    try:
        # Scheme checked in main(): http(s) only.
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return 0, None


def _verdict(body: dict | None) -> tuple[str, str]:
    """(allow|block|wait, message) from a facade answer, failing closed like the shim."""
    if not isinstance(body, dict) or not isinstance(body.get("exit_code"), int):
        return "block", "unreadable facade answer"
    out = (body.get("stdout") or "").strip()
    err = body.get("stderr") or ""
    directive: dict = {}
    if out:
        try:
            parsed = json.loads(out)
        except ValueError:
            return "block", "stdout is not JSON"
        if not isinstance(parsed, dict):
            return "block", "stdout is not a JSON object"
        directive = parsed
    is_block = directive.get("action") == "block" or directive.get("decision") == "block"
    if body["exit_code"] != 0 and not is_block:
        return "block", f"exit {body['exit_code']} without a block directive"
    if not is_block:
        return "allow", ""
    message = directive.get("message") if isinstance(directive.get("message"), str) else ""
    match = re.search(r'"code"\s*:\s*"([A-Za-z0-9_:.-]{1,80})"', err) or re.match(
        r"^([A-Za-z0-9_.:-]{1,80}): ", message
    )
    code = match.group(1) if match else ""
    if code == "hook-timeout" and not WITHDRAWN.search(
        message.replace("NOTHING WAS WITHDRAWN", "")
    ):
        return "wait", message
    return "block", message


def run_scenario(base: str, token: str, run_id: str, name: str, command: str) -> str:
    store = os.environ.get("AGENT_STORE_DIR", "/data/demo")
    envelope = {
        "hook_event_name": "pre_tool_call",
        "tool_name": "terminal",
        "tool_input": {"command": command, "workdir": store},
        "session_id": f"demo-{run_id}-{name}",
        "cwd": store,
    }
    wait_s = float(os.environ.get("AGENT_WAIT_S", "240"))
    deadline = time.monotonic() + wait_s
    attempt = 0
    while True:
        attempt += 1
        status, body = _post(f"{base}/hook/hermes", token, envelope, timeout_s=25)
        if status != 200:
            return f"BLOCKED (facade HTTP {status})"
        kind, message = _verdict(body)
        if os.environ.get("AGENT_DEBUG"):
            print(
                f"   [debug] attempt={attempt} kind={kind} message={message[:200]!r}",
                flush=True,
            )
        if kind == "allow":
            return "ALLOWED"
        if kind == "block":
            return f"BLOCKED: {message[:160]}"
        if time.monotonic() + 3 >= deadline:
            return f"BLOCKED (no decision within {int(wait_s)}s): {message[:120]}"
        if attempt == 1:
            print(
                "   ... waiting for the approver (open the fake Telegram page)",
                flush=True,
            )
        # Re-ask at once: the facade itself holds each post for its hook timeout, and a gap
        # between posts is a window in which a rejection lands with nobody waiting on it
        # (a later re-post of the same bytes is then a new question, by core design).
        time.sleep(0.2)


def main() -> int:
    base = os.environ.get("FACADE_URL", "http://daemon:8080").rstrip("/")
    if not base.startswith(("http://", "https://")):
        print("FACADE_URL must be an http(s) URL", file=sys.stderr)
        return 2
    token = _token()
    run_id = os.environ.get("AGENT_RUN_ID") or str(int(time.time()))
    only = set(filter(None, os.environ.get("AGENT_SCENARIOS", "").split(",")))
    for name, command, why in SCENARIOS:
        if only and name not in only:
            continue
        print(f"-> {name}: `{command}`  ({why})", flush=True)
        outcome = run_scenario(base, token, run_id, name, command)
        print(f"   OUTCOME {name}: {outcome}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
