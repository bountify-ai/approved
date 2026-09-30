"""Private, bounded visitor runtimes behind the dedicated gateway."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import subprocess
import threading
import time
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..inference_budget import InferenceCallBudget, initialize_budget
from ..logs import log
from .config import AGENT_PYTHON, AGENT_SCRIPT, Layout, Settings
from .front import App, FakeTelegramClient, Response, _error, _json, embed_csp
from .page import PAGE_SCRIPT, PAGE_STYLE, render_page
from .runs import LiveBudget, Run, RunLimiter, RunManager, load_scenarios
from .supervisor import Reaper, Supervisor

SESSION_TTL_S = 15 * 60
MAX_SESSIONS = 2
MAX_TELEMETRY_EVENTS = 20
MAX_POLICY_BYTES = 16 * 1024
TELEMETRY_KINDS = frozenset(
    {
        "session_allocated",
        "store_ready",
        "approver_started",
        "approver_healthy",
        "approver_unavailable",
        "approver_failed",
        "gate_started",
        "gate_healthy",
        "gate_unavailable",
        "gate_failed",
        "judge_started",
        "judge_healthy",
        "judge_unavailable",
        "runtime_ready",
        "startup_failed",
    }
)
SESSION_ROUTES = frozenset(
    {
        "/api/state",
        "/api/policy",
        "/api/run",
        "/api/reset",
        "/approver",
        "/approver/",
        "/approver/api/chat",
        "/approver/api/tap",
    }
)


def gateway_secret(env: dict[str, str]) -> str:
    """Read one gateway credential without logging or copying its value into child envs."""
    path = env.get("TRYIT_GATEWAY_SECRET_FILE")
    direct = env.get("TRYIT_GATEWAY_SECRET")
    if bool(path) == bool(direct):
        raise ValueError("set exactly one TRYIT_GATEWAY_SECRET or TRYIT_GATEWAY_SECRET_FILE")
    value = Path(path).read_text().strip() if path else str(direct).strip()
    if len(value) < 32:
        raise ValueError("gateway credential must contain at least 32 characters")
    return value


def _policy_file(data: Path, visitor_id: str) -> tuple[str, bytes]:
    """Read only this session's regular policy through no-follow directory descriptors."""
    path = Layout(data / "tryit" / "sessions" / visitor_id).store / "APPROVAL.md"
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    with ExitStack() as stack:
        directory = os.open(data, directory_flags)
        stack.callback(os.close, directory)
        for part in ("tryit", "sessions", visitor_id, "demo"):
            directory = os.open(part, directory_flags, dir_fd=directory)
            stack.callback(os.close, directory)
        fd = os.open("APPROVAL.md", file_flags, dir_fd=directory)
        stack.callback(os.close, fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_POLICY_BYTES:
            raise ValueError("policy file is not a bounded regular file")
        content = bytearray()
        while len(content) <= MAX_POLICY_BYTES:
            chunk = os.read(fd, MAX_POLICY_BYTES + 1 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > MAX_POLICY_BYTES:
            raise ValueError("policy file exceeds limit")
    return str(path), bytes(content)


@dataclass
class Visitor:
    id: str
    token: str = field(repr=False)
    slot: int = 0
    expires_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    created_monotonic: float = field(default_factory=time.monotonic, repr=False)
    state: str = "starting"
    message: str = "Preparing your private demo."
    app: App | None = None
    sup: Supervisor | None = None
    runs: RunManager | None = None
    stop: threading.Event = field(default_factory=threading.Event, repr=False)
    released: threading.Event = field(default_factory=threading.Event, repr=False)
    provision_done: threading.Event = field(default_factory=threading.Event, repr=False)
    cleanup_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    event_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    events: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=MAX_TELEMETRY_EVENTS), repr=False
    )

    def lifecycle(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "expires_at": self.expires_at,
            "retryable": self.state in {"failed", "expired"},
            "message": self.message,
        }

    def emit(self, kind: str) -> None:
        if kind not in TELEMETRY_KINDS:
            raise ValueError("unrecognized demo telemetry event")
        with self.event_lock:
            self.events.append({"at": time.time(), "kind": kind})

    def telemetry(self, *, maritime_deployment: bool = False) -> dict[str, Any]:
        sup = self.sup
        parts = sup.health.parts() if sup is not None else {}
        processes: dict[str, dict[str, Any]] = {}
        for name, child, part in (
            ("approval_gate", sup.daemon if sup else None, "daemon"),
            ("approver_chat", sup.fake_tg if sup else None, "fake_telegram"),
            ("ai_judge", sup.judge if sup else None, "judge"),
        ):
            running = bool(child and child.alive)
            processes[name] = {
                "running": running,
                "healthy": running and bool(parts.get(part)),
                "starts": child.starts if child else 0,
            }
        with self.event_lock:
            events = list(self.events)
        runtime: dict[str, Any] = {
            "topology": "one-host-isolated-processes",
            "processes": processes,
        }
        if maritime_deployment:
            runtime["provider"] = "Maritime deployment"
        return {
            "version": 1,
            "session": {
                "ref": hashlib.sha256(self.id.encode("ascii")).hexdigest()[:12],
                "created_at": self.created_at,
                "elapsed_s": max(0, int(time.monotonic() - self.created_monotonic)),
                "expires_at": self.expires_at,
            },
            "runtime": runtime,
            "events": events,
        }


class PrivateSessions:
    """HTTP app and lifecycle owner. The shared budget is one object for both sessions."""

    def __init__(self, settings: Settings, data: Path, gateway_key: str) -> None:
        self.settings = settings
        self.data = data
        self._gateway_key = gateway_key
        self._lock = threading.RLock()
        self._visitors: dict[str, Visitor] = {}
        self._reaper = Reaper()
        self._scenarios = load_scenarios(Path(AGENT_SCRIPT))
        self._budget = LiveBudget(
            data / "tryit" / "live-budget.json",
            daily_cap=settings.live_daily_cap,
            per_hour=settings.live_calls_per_hour,
            lifetime_cap=settings.live_lifetime_cap,
        )
        initialize_budget(
            data / "tryit" / "inference-calls.json",
            settings.live_lifetime_cap,
            hourly_limit=settings.live_calls_per_hour,
        )
        self._inference_budget = InferenceCallBudget(
            data / "tryit" / "inference-calls.json",
            settings.live_lifetime_cap,
            hourly_limit=settings.live_calls_per_hour,
        )
        self._page = render_page(settings.public_base_path).encode()
        self._page_csp = embed_csp(
            [PAGE_SCRIPT], [PAGE_STYLE], settings.frame_ancestors, frames=True
        )

    def _ports(self, slot: int) -> Settings:
        base = 18000 + slot * 100
        return replace(
            self.settings,
            daemon_port=base,
            fake_tg_port=base + 1,
            judge_port=base + 2,
            serve_port=base + 3,
            webhook_port=base + 4,
            inference_budget_file=self.data / "tryit" / "inference-calls.json",
        )

    def _expire_locked(self, visitor: Visitor) -> None:
        if visitor.state == "expired":
            return
        visitor.state = "expired"
        visitor.message = "This session expired. Start a new private session."
        visitor.stop.set()
        threading.Thread(target=self._stop, args=(visitor,), daemon=True).start()

    def _stop(self, visitor: Visitor) -> None:
        with visitor.cleanup_lock:
            if visitor.released.is_set():
                return
            if visitor.runs is not None:
                visitor.runs.stop()
            if visitor.sup is not None:
                visitor.sup.shutdown()
            visitor.provision_done.wait()
            visitor.released.set()

    def sweep(self) -> None:
        with self._lock:
            now = time.time()
            for visitor in self._visitors.values():
                if visitor.state not in {"expired", "failed"} and now >= visitor.expires_at:
                    self._expire_locked(visitor)
        self._reaper.reap()

    def run_sweeper(self, stop: threading.Event) -> None:
        while not stop.wait(0.25):
            self.sweep()
            with self._lock:
                ready = [v.sup for v in self._visitors.values() if v.state == "ready" and v.sup]
            for sup in ready:
                sup.watch()

    def shutdown(self) -> None:
        with self._lock:
            visitors = list(self._visitors.values())
            for visitor in visitors:
                visitor.stop.set()
        for visitor in visitors:
            self._stop(visitor)

    def _create(self) -> Response:
        with self._lock:
            self.sweep()
            occupied = {v.slot for v in self._visitors.values() if not v.released.is_set()}
            free = next((slot for slot in range(MAX_SESSIONS) if slot not in occupied), None)
            if free is None:
                return _error(
                    429, "capacity", "Two private sessions are active. Try again shortly."
                )
            visitor = Visitor(
                id=secrets.token_hex(16),
                token=secrets.token_urlsafe(32),
                slot=free,
                expires_at=time.time() + SESSION_TTL_S,
            )
            visitor.emit("session_allocated")
            self._visitors[visitor.token] = visitor
            threading.Thread(target=self._provision, args=(visitor,), daemon=True).start()
        return _json(
            202, {"ok": True, "session_token": visitor.token, "lifecycle": visitor.lifecycle()}
        )

    def _provision(self, visitor: Visitor) -> None:
        settings = self._ports(visitor.slot)
        layout = Layout(self.data / "tryit" / "sessions" / visitor.id)
        try:
            sup = Supervisor(settings, layout, self._reaper, on_event=visitor.emit)
            visitor.sup = sup
            sup.boot(visitor.stop, judge_live=True)
            if visitor.stop.is_set():
                return
            if not sup.booted:
                raise RuntimeError(sup.boot_error or "the demo runtime could not start")

            def spawn(run_id: str, scenario: str) -> subprocess.Popen[bytes]:
                return self._reaper.spawn(
                    [AGENT_PYTHON, "-u", AGENT_SCRIPT],
                    env=settings.agent_env(layout, run_id=run_id, scenario=scenario),
                    cwd="/demo/agent",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

            def admit() -> str | None:
                expected = len(self._scenarios)
                return self._inference_budget.check(expected) or self._budget.reserve(expected)

            runs = RunManager(
                scenarios=self._scenarios,
                spawn=spawn,
                wait_exit=lambda proc: self._reaper.wait(proc) or 0,
                limiter=RunLimiter(settings.run_min_interval_s, settings.runs_per_hour),
                admit=admit,
                on_end=lambda _run: sup.verifier.request(),
                prepare=lambda run: self._prepare(run, sup),
            )
            visitor.runs = runs
            app = App(
                settings=settings,
                judge_state_dir=layout.judge_state,
                runs=runs,
                chat=FakeTelegramClient(f"http://127.0.0.1:{settings.fake_tg_port}"),
                budget=self._budget,
                health=sup.health.parts,
                judge_worker=sup.health.judge_worker,
                log_verify=sup.verifier.result,
                request_verify=sup.verifier.request,
                next_reviewer=lambda: (
                    "live",
                    self._inference_budget.check(len(self._scenarios))
                    or self._budget.check(len(self._scenarios)),
                ),
                chat_generation=lambda: sup.fake_tg_generation,
                booted=lambda: sup.booted,
                private_mode=True,
            )
            with self._lock:
                visitor.app = app
                if not visitor.stop.is_set():
                    visitor.state = "ready"
                    visitor.message = "Your private demo is ready."
            threading.Thread(target=sup.health.run, args=(visitor.stop,), daemon=True).start()
            threading.Thread(target=sup.verifier.run, args=(visitor.stop,), daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            log("tryit.session.failed", level="error", error=type(exc).__name__)
            with self._lock:
                visitor.state = "failed"
                visitor.message = "The private demo could not start. Try a new session."
                visitor.emit("startup_failed")
            visitor.stop.set()
        finally:
            visitor.provision_done.set()
            if visitor.stop.is_set():
                self._stop(visitor)

    @staticmethod
    def _prepare(run: Run, sup: Supervisor) -> None:
        run.reviewer = "live"
        if not sup.health.parts().get("judge"):
            run.fallback = "judge-unhealthy"

    def handle(self, method: str, target: str, headers: dict[str, str], read_body: Any) -> Response:
        path = target.split("?", 1)[0].split("#", 1)[0]
        if path == "/health":
            return (
                _json(200, {"status": "ok"})
                if method in {"GET", "HEAD"}
                else _error(405, "method-not-allowed", "GET only")
            )
        gateway = headers.get("x-approved-gateway", "")
        if not hmac.compare_digest(gateway, self._gateway_key):
            return _error(403, "gateway-required", "Forbidden.")
        if path == "/":
            if method not in {"GET", "HEAD"}:
                return _error(405, "method-not-allowed", "GET only")
            return Response(
                200,
                self._page,
                "text/html; charset=utf-8",
                {
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": self._page_csp,
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                },
            )
        if path == "/api/session":
            if method != "POST":
                return _error(405, "method-not-allowed", "POST only")
            if headers.get("content-type", "").split(";", 1)[0] != "application/json":
                return _error(415, "json-required", "POST application/json.")
            if headers.get("content-length", "0") not in {"0", "2"}:
                return _error(413, "body-too-large", "Empty JSON object only.")
            if headers.get("content-length") == "2" and read_body(2) != b"{}":
                return _error(400, "bad-session", "Empty JSON object only.")
            return self._create()
        if path not in SESSION_ROUTES:
            return _error(404, "not-found", "Not found.")
        token = headers.get("x-approved-session", "")
        with self._lock:
            visitor = self._visitors.get(token)
            if visitor is None or not hmac.compare_digest(visitor.token, token):
                return _error(401, "session-required", "A private session is required.")
            if time.time() >= visitor.expires_at:
                self._expire_locked(visitor)
            lifecycle = visitor.lifecycle()
            app = visitor.app
        if visitor.state == "expired":
            return _json(410, {"ok": False, "code": "session-expired", "session": lifecycle})
        if path == "/api/policy":
            if method not in {"GET", "HEAD"}:
                return _error(405, "method-not-allowed", "GET only")
            if target != path:
                return _error(400, "query-not-allowed", "Policy path is fixed.")
            if app is None or visitor.state != "ready":
                return _error(503, "session-starting", "The private session is still starting.")
            try:
                policy_path, raw = _policy_file(self.data, visitor.id)
                content = raw.decode("utf-8")
            except (OSError, ValueError, UnicodeError):
                return _error(503, "policy-unavailable", "The current policy file is unavailable.")
            with self._lock:
                if visitor.state != "ready" or time.time() >= visitor.expires_at:
                    return _error(410, "session-expired", "This session expired.")
            response = _json(
                200,
                {
                    "path": policy_path,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "text": content,
                },
                **{"Cache-Control": "no-store"},
            )
            if method == "HEAD":
                response.body = b""
            return response
        if path == "/api/state":
            if method not in {"GET", "HEAD"}:
                return _error(405, "method-not-allowed", "GET only")
            if app is None or visitor.state != "ready":
                return _json(
                    200,
                    {
                        "session": lifecycle,
                        "telemetry": visitor.telemetry(
                            maritime_deployment=self.settings.maritime_deployment
                        ),
                    },
                )
            state = json.loads(app.state(b"").body)
            state["session"] = lifecycle
            state["telemetry"] = visitor.telemetry(
                maritime_deployment=self.settings.maritime_deployment
            )
            return _json(200, state)
        if app is None or visitor.state != "ready":
            return _json(503, {"ok": False, "code": "session-starting", "session": lifecycle})
        return app.handle(method, target, headers, read_body)
