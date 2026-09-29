"""``approved ask``: one prompt to a gated Hermes, through its loopback API, detached-safe.

The gateway call is the one approval-md-hosted ``scripts/gate-placement/run.mjs``
``gatewayScript`` makes: as the ``hermes`` user (``/command/s6-setuidgid hermes``), read
``API_SERVER_KEY`` from ``/data/.hermes/.env`` inside the machine, and POST
``{model: "hermes", messages: [{role: "user", content: <prompt>}]}`` to
``http://127.0.0.1:8642/v1/chat/completions``. The key never leaves the machine and never
appears on argv.

A gated tool call can wait minutes for a human, and ``maritime exec`` stops at 120 s, so the
call runs detached, as run.mjs ``detachedCommand`` does: a first exec writes the script into
a fresh 0700 job directory and starts it under ``setsid``; later execs poll its ``status``
file every few seconds and read ``out`` once it lands. A poll that outlasts ``--wait-s``
leaves the job running and prints its id, so ``approved ask --job <id>`` can collect it.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .maritime import Maritime, MaritimeError
from .names import resolve_target

__all__ = ["AskOutcome", "ask", "gateway_script", "job_dir"]

GATEWAY_URL = "http://127.0.0.1:8642/v1/chat/completions"
MAX_PROMPT = 8000
_JOB = re.compile(r"^[0-9a-f]{16}$")
_BLOCK_MARKERS = re.compile(
    r"hook-timeout|hook-rejected|hook-expired|approval facade|waiting for (a )?human|"
    r"waiting for approval|approval (is )?required|\bblocked\b",
    re.IGNORECASE,
)

Kind = Literal["allowed", "blocked", "timeout", "error"]


@dataclass(frozen=True)
class AskOutcome:
    kind: Kind
    reply: str
    job: str


def job_dir(job: str) -> str:
    if not _JOB.match(job):
        raise ValueError("job id must be 16 lowercase hex characters")
    return f"/tmp/approved-ask-{job}"  # noqa: S108 - a path inside the remote machine


def gateway_script(prompt: str) -> str:
    """The in-machine call. The prompt is embedded as a JSON string literal, so it can never
    end the heredoc (JSON escapes newlines) or reach a shell."""
    return (
        "set -eu\n/command/s6-setuidgid hermes node <<'GATEWAY_JS'\n"
        "const fs = require('fs');\n"
        "const key = fs.readFileSync('/data/.hermes/.env','utf8')"
        ".match(/^API_SERVER_KEY=(.*)$/m)?.[1];\n"
        "if (!key) process.exit(12);\n"
        f"const prompt = {json.dumps(prompt)};\n"
        f"fetch('{GATEWAY_URL}',{{method:'POST',headers:{{Authorization:'Bearer '+key,"
        "'content-type':'application/json'},body:JSON.stringify({model:'hermes',"
        "messages:[{role:'user',content:prompt}]})})"
        ".then(async r => { const text=await r.text(); "
        "console.log(JSON.stringify({http:r.status,body:text.slice(0,16000)})); "
        "if(!r.ok)process.exit(13); })"
        ".catch(e=>{console.error(e.name);process.exit(14)});\n"
        "GATEWAY_JS\n"
    )


def _launch_script(path: str, call: str) -> str:
    return (
        "set -eu\n"
        f"mkdir -m 700 {path}\n"
        f"cat > {path}/call.sh <<'APPROVED_ASK_CALL'\n{call}APPROVED_ASK_CALL\n"
        f"setsid sh -c 'sh {path}/call.sh > {path}/out 2> {path}/err; code=$?; "
        f'printf "%s\\n" "$code" > {path}/status.tmp; mv {path}/status.tmp {path}/status\' '
        "</dev/null >/dev/null 2>&1 &\n"
        "printf '%s\\n' \"$!\"\n"
    )


def _reply_of(output: str) -> tuple[int | None, str]:
    try:
        wrapper = json.loads(output.strip().splitlines()[-1])
        http = wrapper.get("http")
        body = json.loads(wrapper.get("body") or "{}")
        content = body["choices"][0]["message"]["content"]
        return (http if isinstance(http, int) else None), str(content or "")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None, ""


def ask(
    maritime: Maritime,
    agent: str,
    prompt: str | None,
    *,
    job: str | None = None,
    wait_s: float = 280.0,
    poll_s: float = 5.0,
    allow_target: str | None = None,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> AskOutcome:
    if allow_target is not None:
        out(f"warning: --allow-target lets this read-only ask reach {allow_target!r} only")
    resolve_target(maritime, agent, allow=allow_target)
    agent = agent.strip()
    if job is None:
        if not prompt or len(prompt) > MAX_PROMPT:
            raise ValueError(f"the prompt must be 1 to {MAX_PROMPT} characters")
        job = secrets.token_hex(8)
        path = job_dir(job)
        launched = maritime.exec(agent, _launch_script(path, gateway_script(prompt)))
        if launched.exit_code != 0 or not launched.stdout.strip().isdigit():
            return AskOutcome("error", "the detached call did not start", job)
        out(f"asked {agent} (job {job}); polling every {poll_s:g}s for up to {wait_s:g}s")
    path = job_dir(job)

    deadline = time.monotonic() + wait_s
    marker = "pending"
    while True:
        try:
            marker = maritime.exec(
                agent, f"if test -f {path}/status; then cat {path}/status; else echo pending; fi\n"
            ).stdout.strip()
        except MaritimeError:
            marker = "pending"  # a lost poll is not a result; ask again
        if marker != "pending" or time.monotonic() >= deadline:
            break
        sleep(poll_s)
    if marker == "pending":
        out(
            f"no answer within {wait_s:g}s. If the agent is waiting for approval on Telegram, "
            f"tap it, then collect with: approved ask {agent} --job {job}"
        )
        return AskOutcome("timeout", "", job)

    result = maritime.exec(agent, f"cat {path}/out\n")
    maritime.exec(agent, f"rm -rf {path}\n")
    http, reply = _reply_of(result.stdout)
    if marker != "0" or http != 200:
        out(f"the gateway call failed (exit {marker}, HTTP {http})")
        return AskOutcome("error", reply, job)
    out(reply)
    if _BLOCK_MARKERS.search(reply):
        out("-> blocked by the gate: waiting for approval on Telegram")
        return AskOutcome("blocked", reply, job)
    return AskOutcome("allowed", reply, job)
