"""Private, bounded visitor runtimes behind the dedicated gateway."""

from __future__ import annotations

import hmac
import json
import secrets
import subprocess
import threading
import time
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
SESSION_ROUTES = frozenset(
    {
        "/api/state",
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


@dataclass
class Visitor:
    id: str
    token: str = field(repr=False)
    slot: int = 0
    expires_at: float = 0.0
    state: str = "starting"
    message: str = "Preparing your private demo."
    app: App | None = None
    sup: Supervisor | None = None
    runs: RunManager | None = None
    stop: threading.Event = field(default_factory=threading.Event, repr=False)
    released: threading.Event = field(default_factory=threading.Event, repr=False)
    provision_done: threading.Event = field(default_factory=threading.Event, repr=False)
    cleanup_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def lifecycle(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "expires_at": self.expires_at,
            "retryable": self.state in {"failed", "expired"},
            "message": self.message,
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
            self._visitors[visitor.token] = visitor
            threading.Thread(target=self._provision, args=(visitor,), daemon=True).start()
        return _json(
            202, {"ok": True, "session_token": visitor.token, "lifecycle": visitor.lifecycle()}
        )

    def _provision(self, visitor: Visitor) -> None:
        settings = self._ports(visitor.slot)
        layout = Layout(self.data / "tryit" / "sessions" / visitor.id)
        try:
            sup = Supervisor(settings, layout, self._reaper)
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
        if path == "/api/state":
            if method not in {"GET", "HEAD"}:
                return _error(405, "method-not-allowed", "GET only")
            if app is None or visitor.state != "ready":
                return _json(200, {"session": lifecycle})
            state = json.loads(app.state(b"").body)
            state["session"] = lifecycle
            return _json(200, state)
        if app is None or visitor.state != "ready":
            return _json(503, {"ok": False, "code": "session-starting", "session": lifecycle})
        return app.handle(method, target, headers, read_body)
