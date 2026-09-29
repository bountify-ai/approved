"""A thin, guarded wrapper around the ``maritime`` CLI (1.7.0).

Every call runs ``maritime --json <command> ...`` (``--json`` is a global option, so it goes
before the subcommand) without a shell and parses the JSON contract from
``maritime guide``: stdout carries one JSON value on success; stderr one error object
``{ok: false, error: {code, message}}`` on failure; exit codes 0 ok, 1 error, 2 auth,
3 not found, 4 usage.

**No secret on argv, ever.** A :class:`SecretGuard` holding every credential value this
process knows is checked against the full argv before each spawn; a hit raises before
anything runs. Credentials reach a machine only through ``maritime env import <agent>
<file>``, whose argv carries a path.

``exec`` output is normalised the way approval-md-hosted
``scripts/gate-placement/maritime-exec-adapter.mjs`` does it: the result may sit under
``result`` and the exit code may be spelled ``exit_code``, ``exitCode``, ``exitStatus`` or
``code``; a truncated or incomplete answer is an error, never a partial success.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = ["ExecResult", "Maritime", "MaritimeError", "SecretGuard"]

EXEC_CEILING_S = 120
DEFAULT_TIMEOUT_S = 180
MAX_OUTPUT = 256 * 1024


class MaritimeError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.code = code


class SecretGuard:
    """Refuses any argv (or printed text) that contains a known credential value."""

    MIN_LEN = 8

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values: set[str] = set()
        self.add(*values)

    def add(self, *values: str) -> None:
        for value in values:
            if value and len(value) >= self.MIN_LEN:
                self._values.add(value)

    def leaks(self, text: str) -> bool:
        return any(value in text for value in self._values)

    def redact(self, text: str) -> str:
        for value in self._values:
            text = text.replace(value, "[REDACTED]")
        return text

    def check_argv(self, argv: list[str]) -> None:
        if self.leaks("\x00".join(argv)):
            raise MaritimeError("refusing to run: a credential value would appear on argv")


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class Maritime:
    def __init__(
        self,
        guard: SecretGuard,
        *,
        binary: str = "maritime",
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.guard = guard
        self.binary = binary
        self.timeout_s = timeout_s

    # ------------------------------------------------------------------ core

    def run(self, args: list[str], *, stdin: str = "", timeout_s: float | None = None) -> Any:
        argv = [self.binary, "--json", *args]
        self.guard.check_argv(argv)
        try:
            proc = subprocess.run(  # noqa: S603 - fixed binary, argv list, no shell
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout_s or self.timeout_s,
                check=False,
            )
        except FileNotFoundError:
            raise MaritimeError(f"{self.binary} is not installed or not on PATH") from None
        except subprocess.TimeoutExpired:
            raise MaritimeError(f"maritime {args[0]} timed out") from None
        if len(proc.stdout) + len(proc.stderr) > MAX_OUTPUT:
            raise MaritimeError(f"maritime {args[0]} output exceeds 256 KiB")
        if proc.returncode != 0:
            code, message = None, proc.stderr.strip()[:300]
            try:
                err = json.loads(proc.stderr).get("error", {})
                code, message = err.get("code"), str(err.get("message", ""))[:300]
            except (ValueError, AttributeError):
                pass
            raise MaritimeError(
                f"maritime {args[0]} exited {proc.returncode}: {self.guard.redact(message)}",
                exit_code=proc.returncode,
                code=code,
            )
        text = proc.stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            raise MaritimeError(f"maritime {args[0]} returned no JSON") from None

    # ------------------------------------------------------------------ verbs

    def list_agents(self) -> list[dict[str, Any]]:
        answer = self.run(["list"])
        if isinstance(answer, dict):
            for key in ("agents", "items", "data", "result"):
                if isinstance(answer.get(key), list):
                    answer = answer[key]
                    break
        if not isinstance(answer, list):
            raise MaritimeError("maritime list returned an unexpected shape")
        return [a for a in answer if isinstance(a, dict)]

    def find(self, name: str) -> dict[str, Any] | None:
        return next((a for a in self.list_agents() if a.get("name") == name), None)

    def create(self, argv: list[str]) -> dict[str, Any]:
        """``argv`` from :func:`approved.console.bundle.create_argv` plus ``-e`` pairs.

        stdin is one empty line: ``create --repo`` prints a template list and asks "Template
        number (or press Enter to skip)"; Enter skips it, which gives a custom framework.
        """
        if argv[:2] != ["maritime", "create"]:
            raise MaritimeError("create_argv must start with 'maritime create'")
        answer = self.run(argv[1:], stdin="\n", timeout_s=300)
        return answer if isinstance(answer, dict) else {}

    def env_import(self, agent: str, path: str) -> None:
        self.run(["env", "import", agent, path])

    def stop(self, agent: str) -> None:
        self.run(["stop", agent], timeout_s=300)

    def start(self, agent: str) -> None:
        self.run(["start", agent], timeout_s=300)

    def status(self, agent: str) -> dict[str, Any]:
        answer = self.run(["status", agent])
        return answer if isinstance(answer, dict) else {}

    def exec(self, agent: str, script: str) -> ExecResult:
        answer = self.run(["exec", agent, "--", "sh", "-c", script], timeout_s=EXEC_CEILING_S + 15)
        value = answer.get("result") if isinstance(answer, dict) else None
        if not isinstance(value, dict):
            value = answer if isinstance(answer, dict) else {}
        exit_code = next(
            (
                value[k]
                for k in ("exit_code", "exitCode", "exitStatus", "code")
                if isinstance(value.get(k), int) and not isinstance(value.get(k), bool)
            ),
            None,
        )
        stdout, stderr = value.get("stdout"), value.get("stderr")
        if (
            exit_code is None
            or not isinstance(stdout, str)
            or not isinstance(stderr, str)
            or value.get("truncated") is True
        ):
            raise MaritimeError("maritime exec JSON is incomplete or truncated")
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


def agent_id(agent: dict[str, Any]) -> str | None:
    for key in ("id", "agentId", "agent_id"):
        value = agent.get(key)
        if isinstance(value, str) and value:
            return value
    nested = agent.get("agent")
    return agent_id(nested) if isinstance(nested, dict) else None
