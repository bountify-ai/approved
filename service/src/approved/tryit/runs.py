"""Agent runs on the shared demo tenant: one at a time, rate-limited, fixed scenarios only.

A run is the demo's scripted agent (``demo/agent/agent.py``) against the gate, once per
scenario of its own ``SCENARIOS`` list, in order, with the demo's settings (``AGENT_SCENARIOS``
selects one scenario per invocation, ``AGENT_RUN_ID`` names the run). Running it once per
scenario lets a run END when a request expires unanswered: the agent only exits once the gate
has answered (its wait outlasts the approval window plus the daemon's expiry sweep), so no
request is ever left open behind a finished run. Nothing a visitor sends reaches the agent:
the command lines are the agent's own constants.

Pure logic lives here (the limiter, the live-reviewer budget, outcome parsing, the tap
ownership check) so it is tested without processes or sockets; ``RunManager`` takes the
process spawning as a callable.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import secrets
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..logs import log

__all__ = [
    "REFUSALS",
    "LiveBudget",
    "Run",
    "RunLimiter",
    "RunManager",
    "classify_outcome",
    "load_scenarios",
    "tap_refusal",
]

OUTCOME = re.compile(r"^\s*OUTCOME ([A-Za-z0-9_-]+): (.*)$")
MAX_LINES = 200
MAX_LINE_CHARS = 300

#: Why a tap is refused, in words for the page. Keys are the machine-readable codes.
REFUSALS: dict[str, str] = {
    "no-active-run": "No run is in progress; press Run the agent first.",
    "unknown-message": "That message is not in the chat.",
    "not-this-run": "That prompt does not belong to the run in progress.",
    "no-buttons": "That message has no buttons: it was answered already, or it is not a prompt.",
    "already-tapped": "That prompt has already been tapped.",
    "not-a-button": "That is not one of the prompt's buttons.",
}


def load_scenarios(agent_script: Path) -> list[tuple[str, str, str]]:
    """The agent's own ``SCENARIOS`` (name, command, why), read from the script itself so the
    page and the run never carry a second copy of the commands."""
    spec = importlib.util.spec_from_file_location("tryit_demo_agent", agent_script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the demo agent at {agent_script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scenarios = [(str(n), str(c), str(w)) for n, c, w in module.SCENARIOS]
    if not scenarios or not all(re.fullmatch(r"[a-z0-9-]{1,40}", n) for n, _, _ in scenarios):
        raise RuntimeError("the demo agent's SCENARIOS are not usable scenario names")
    return scenarios


def classify_outcome(outcome: str | None) -> tuple[str, str]:
    """(status, detail) from the agent's ``OUTCOME <name>: ...`` text."""
    if outcome is None:
        return "blocked", "the agent ended without an outcome"
    text = outcome.strip()
    detail = text[:200]
    if text == "ALLOWED":
        return "allowed", "the gate let it run"
    if text.startswith("BLOCKED: hook-rejected"):
        return "rejected", detail
    if "hook-expired" in text:
        return "expired", detail
    if text.startswith("BLOCKED (no decision within"):
        return "timed-out", detail
    return "blocked", detail


def _texts_before(messages: list[Mapping[str, Any]], message_id: int, bot: Any, floor: int):
    for m in reversed(messages):
        mid = m.get("message_id")
        if isinstance(mid, int) and floor < mid < message_id and m.get("bot") == bot:
            yield str(m.get("text") or "")


def tap_refusal(
    messages: Iterable[Mapping[str, Any]],
    message_id: int,
    data: str,
    *,
    run_id: str,
    floor: int,
    tapped: Iterable[int] = (),
) -> str | None:
    """``None`` when a tap on ``message_id`` with ``data`` may be delivered, else a code.

    A tap is delivered only for a prompt the CURRENT run opened: the message arrived after the
    run started (``floor`` is the chat's last message id at that moment), carries that button,
    and it or the gate's ``APPROVAL REQUIRED`` header before it names this run's action keys
    (``hook:demo-<run id>-<scenario>:...``, from the agent's session id). One tap per prompt.
    """
    if message_id in set(tapped):
        return "already-tapped"
    if message_id <= floor:
        return "not-this-run"
    chat = [m for m in messages if isinstance(m, Mapping) and isinstance(m.get("message_id"), int)]
    message = next((m for m in chat if m["message_id"] == message_id), None)
    if message is None:
        return "unknown-message"
    buttons = [b.get("data") for b in (message.get("buttons") or []) if isinstance(b, Mapping)]
    if not buttons:
        return "no-buttons"
    if data not in buttons:
        return "not-a-button"
    marker = f"hook:demo-{run_id}-"
    if marker in str(message.get("text") or ""):
        return None
    for text in _texts_before(chat, message_id, message.get("bot"), floor):
        if "APPROVAL REQUIRED" in text:
            return None if marker in text else "not-this-run"
    return "not-this-run"


class RunLimiter:
    """Global run limits: a minimum gap between run starts and a rolling hourly count."""

    def __init__(
        self,
        min_interval_s: float,
        per_hour: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min_interval = min_interval_s
        self._per_hour = per_hour
        self._clock = clock
        self._starts: deque[float] = deque()

    def retry_after(self) -> float | None:
        """Seconds until a run may start, or ``None`` if one may start now."""
        now = self._clock()
        while self._starts and now - self._starts[0] >= 3600:
            self._starts.popleft()
        waits = []
        if self._starts and now - self._starts[-1] < self._min_interval:
            waits.append(self._min_interval - (now - self._starts[-1]))
        if len(self._starts) >= self._per_hour:
            waits.append(3600 - (now - self._starts[0]))
        return max(waits) if waits else None

    def record(self) -> None:
        self._starts.append(self._clock())

    def snapshot(self) -> dict[str, Any]:
        wait = self.retry_after()
        return {
            "min_interval_s": self._min_interval,
            "runs_per_hour": self._per_hour,
            "runs_this_hour": len(self._starts),
            "next_run_in_s": round(wait, 1) if wait is not None else 0,
        }


class LiveBudget:
    """A hard cap on live reviewer calls: per UTC day (persisted under /data, so a restart does
    not reset it) and per rolling hour.

    Charged conservatively and UP FRONT: a live run reserves one call per scenario before it
    starts (the judge reviews at most one request per scenario), so the cap holds even if every
    request is reviewed. A budget file that cannot be read counts as spent: the fail-closed
    direction for a spending cap is the offline reviewer.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        daily_cap: int,
        per_hour: int,
        lifetime_cap: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._marker = path.with_name("live-budget-initialized") if path else None
        self._daily_cap = daily_cap
        self._per_hour = per_hour
        self._lifetime_cap = lifetime_cap
        self._clock = clock
        self._lock = threading.Lock()

    def _day(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y-%m-%d")

    def _load(self) -> dict[str, Any] | None:
        empty = {"day": self._day(), "used": 0, "lifetime_used": 0, "recent": []}
        if self._path is None:
            return empty
        if not self._path.exists():
            return None if self._marker is not None and self._marker.exists() else empty
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            used = int(data["used"])
            lifetime_used = int(data.get("lifetime_used", used))
            recent = [(float(t), int(n)) for t, n in data.get("recent", [])]
            day = str(data["day"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if day != self._day():
            used = 0
        now = self._clock()
        recent = [(t, n) for t, n in recent if now - t < 3600]
        return {"day": self._day(), "used": used, "lifetime_used": lifetime_used, "recent": recent}

    def _save(self, data: dict[str, Any]) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f".{self._path.name}.{secrets.token_hex(4)}")
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self._path)
        if self._marker is not None and not self._marker.exists():
            self._marker.write_text("initialized\n", encoding="ascii")

    def _refusal(self, data: dict[str, Any] | None, calls: int) -> str | None:
        if data is None:
            return "budget-unreadable"
        if self._lifetime_cap is not None and data["lifetime_used"] + calls > self._lifetime_cap:
            return "lifetime-cap"
        if data["used"] + calls > self._daily_cap:
            return "daily-cap"
        if sum(n for _, n in data["recent"]) + calls > self._per_hour:
            return "hourly-limit"
        return None

    def check(self, calls: int) -> str | None:
        """Why ``calls`` live calls may NOT be spent now, or ``None`` if they may."""
        with self._lock:
            return self._refusal(self._load(), calls)

    def reserve(self, calls: int) -> str | None:
        """Charge ``calls`` live calls and return ``None``, or return why not (charging nothing)."""
        with self._lock:
            data = self._load()
            refusal = self._refusal(data, calls)
            if refusal is not None or data is None:
                return refusal
            data["used"] += calls
            data["lifetime_used"] += calls
            data["recent"].append((self._clock(), calls))
            self._save(data)
            return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = self._load()
        return {
            "daily_cap": self._daily_cap,
            "calls_per_hour": self._per_hour,
            "used_today": None if data is None else data["used"],
            "lifetime_cap": self._lifetime_cap,
            "used_lifetime": None if data is None else data["lifetime_used"],
            "used_this_hour": None if data is None else sum(n for _, n in data["recent"]),
        }


@dataclass
class Scenario:
    name: str
    command: str
    why: str
    status: str = (
        "pending"  # pending running waiting allowed rejected expired timed-out blocked not-run
    )
    detail: str = ""


@dataclass
class Run:
    id: str
    floor: int
    started_at: float
    scenarios: list[Scenario]
    state: str = "preparing"  # preparing running done
    reviewer: str = "offline"  # the reviewer this run's requests were judged by
    fallback: str | None = None  # why a live-configured run fell back to offline
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    ended_at: float | None = None
    end_reason: str | None = None
    tapped: set[int] = field(default_factory=set)
    proc: subprocess.Popen[bytes] | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "reviewer": self.reviewer,
            "fallback": self.fallback,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "end_reason": self.end_reason,
            "scenarios": [
                {
                    "name": s.name,
                    "command": s.command,
                    "why": s.why,
                    "status": s.status,
                    "detail": s.detail,
                }
                for s in self.scenarios
            ],
            "lines": list(self.lines),
        }


@dataclass(frozen=True)
class StartResult:
    status: int
    code: str
    message: str
    run_id: str | None = None
    retry_after_s: float | None = None


Spawn = Callable[[str, str], "subprocess.Popen[bytes]"]


class RunManager:
    """Owns the one run slot. ``start`` answers at once; the run itself is a thread."""

    def __init__(
        self,
        *,
        scenarios: list[tuple[str, str, str]],
        spawn: Spawn,
        wait_exit: Callable[[subprocess.Popen[bytes]], int],
        limiter: RunLimiter,
        prepare: Callable[[Run], None] = lambda run: None,
        admit: Callable[[], str | None] = lambda: None,
        on_end: Callable[[Run], None] = lambda run: None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._scenarios = scenarios
        self._spawn = spawn
        self._wait_exit = wait_exit
        self._limiter = limiter
        self._prepare = prepare
        self._admit = admit
        self._on_end = on_end
        self._clock = clock
        self._lock = threading.Lock()
        self._current: Run | None = None
        self._last: Run | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ slot

    def start(self, floor: int) -> StartResult:
        with self._lock:
            if self._stopping:
                return StartResult(
                    503, "shutting-down", "The demo is restarting; try again shortly."
                )
            if self._current is not None:
                return StartResult(
                    409,
                    "run-in-progress",
                    "A run is already in progress in this private session. "
                    "Watch it or tap its prompts.",
                )
            wait = self._limiter.retry_after()
            if wait is not None:
                return StartResult(
                    429,
                    "rate-limited",
                    f"Runs are rate-limited in this session; the next one may start in "
                    f"{int(wait) + 1} s.",
                    retry_after_s=round(wait, 1),
                )
            refusal = self._admit()
            if refusal is not None:
                if refusal in {
                    "lifetime-cap",
                    "daily-cap",
                    "hourly-limit",
                    "inference-budget-exhausted",
                }:
                    return StartResult(
                        503, "budget-exhausted", "The live inference budget is exhausted."
                    )
                if refusal in {"budget-unreadable", "inference-budget-unavailable"}:
                    return StartResult(
                        503,
                        "budget-unavailable",
                        "The live inference spending ledger is unavailable.",
                    )
                return StartResult(503, "live-unavailable", "The live reviewer is unavailable.")
            self._limiter.record()
            run = Run(
                id=f"t{secrets.token_hex(5)}",
                floor=floor,
                started_at=self._clock(),
                scenarios=[Scenario(n, c, w) for n, c, w in self._scenarios],
            )
            self._current = self._last = run
            self._thread = threading.Thread(
                target=self._execute, args=(run,), name="run", daemon=True
            )
            self._thread.start()
        log("tryit.run.start", run=run.id)
        return StartResult(202, "started", "The agent is running.", run_id=run.id)

    def active(self) -> Run | None:
        with self._lock:
            return self._current

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return None if self._last is None else self._last.snapshot()

    def clear(self) -> bool:
        """Forget the last finished run's view (the Reset control). Refused during a run."""
        with self._lock:
            if self._current is not None:
                return False
            self._last = None
            return True

    def claim_tap(self, run: Run, message_id: int) -> bool:
        with self._lock:
            if self._current is not run or message_id in run.tapped:
                return False
            run.tapped.add(message_id)
            return True

    def release_tap(self, run: Run, message_id: int) -> None:
        with self._lock:
            run.tapped.discard(message_id)

    def limits(self) -> dict[str, Any]:
        with self._lock:
            return self._limiter.snapshot()

    def stop(self, timeout_s: float = 10.0) -> None:
        with self._lock:
            self._stopping = True
            run = self._current
            proc = run.proc if run else None
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, 15)
        if self._thread is not None:
            self._thread.join(timeout_s)

    # ------------------------------------------------------------ the run

    def _line(self, run: Run, raw: bytes) -> str:
        line = raw.decode("utf-8", errors="replace").rstrip()[:MAX_LINE_CHARS]
        with self._lock:
            run.lines.append(line)
        return line

    def _set(self, run: Run, scenario: Scenario | None = None, **fields: Any) -> None:
        with self._lock:
            target: Any = scenario if scenario is not None else run
            for key, value in fields.items():
                setattr(target, key, value)

    def _execute(self, run: Run) -> None:
        ended_by: str | None = None
        try:
            self._prepare(run)
            self._set(run, state="running")
            for scenario in run.scenarios:
                if ended_by is not None or self._stopping:
                    self._set(run, scenario, status="not-run", detail=ended_by or "shutting down")
                    continue
                self._set(run, scenario, status="running")
                proc = self._spawn(run.id, scenario.name)
                self._set(run, proc=proc)
                outcome = None
                assert proc.stdout is not None
                for raw in proc.stdout:
                    line = self._line(run, raw)
                    if "waiting for the approver" in line:
                        self._set(run, scenario, status="waiting")
                    match = OUTCOME.match(line)
                    if match and match.group(1) == scenario.name:
                        outcome = match.group(2)
                self._wait_exit(proc)
                status, detail = classify_outcome(outcome)
                self._set(run, scenario, status=status, detail=detail)
                if status in ("expired", "timed-out"):
                    ended_by = "the run ended: a request expired unanswered"
                    self._set(run, end_reason=ended_by)
        except Exception as exc:  # noqa: BLE001 - a broken run ends; the demo keeps serving
            log("tryit.run.error", level="error", run=run.id, error=type(exc).__name__)
            self._set(
                run, end_reason=f"the run stopped on an internal error ({type(exc).__name__})"
            )
            for scenario in run.scenarios:
                if scenario.status in ("pending", "running", "waiting"):
                    self._set(run, scenario, status="not-run")
        finally:
            with self._lock:
                run.state = "done"
                run.ended_at = self._clock()
                run.proc = None
                if self._current is run:
                    self._current = None
            log("tryit.run.end", run=run.id, reason=run.end_reason)
            try:
                self._on_end(run)
            except Exception as exc:  # noqa: BLE001
                log("tryit.run.on_end_failed", level="warning", error=type(exc).__name__)
