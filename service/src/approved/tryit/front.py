"""The public front: the only listener on ``0.0.0.0:$PORT``.

Routes (exact paths; everything else is 404, a known path with another method is 405):

=========================  ======  ===========================================================
path                       method  what
=========================  ======  ===========================================================
``/``                      GET     the try-it page (embeddable by the configured origins)
``/health``                GET     200 only when the daemon, fake Telegram, judge and front are up
``/api/state``             GET     run, reviewer mode, judge counters and chain status (JSON)
``/api/run``               POST    start one run of the scripted agent (one at a time, limited)
``/api/reset``             POST    clear the page's view of the chat and the last run
``/approver/``             GET     the fake Telegram's own chat page (embeddable)
``/approver/api/chat``     GET     the chat since the view was last cleared (JSON)
``/approver/api/tap``      POST    one tap on a prompt the CURRENT run opened
``/chat``                  POST    the platform contract's chat endpoint: a fixed refusal
=========================  ======  ===========================================================

Nothing else of the children is reachable: no facade route, no log export, no judge console,
no Bot API path, no generic proxy. The fake Telegram is asked for exactly three things (its
page, its chat JSON, a tap), and each answer is filtered before it leaves. Bodies are capped
per route before they are read; POSTs must be ``application/json`` (a cross-origin page then
needs a CORS preflight, which is never granted). No cookie is set or read.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from ..console.live import counters
from ..feedback import agreement, human_decision
from ..logs import log
from ..state import STATE_FILENAME, JudgedEntry, JudgeState
from .config import FAKE_TG_PORT, Settings
from .page import PAGE_SCRIPT, PAGE_STYLE, csp_hash, render_page
from .runs import REFUSALS, LiveBudget, RunManager, tap_refusal

__all__ = ["App", "FakeTelegramClient", "Response", "make_server", "record_view"]

#: Body caps per route, in bytes. A GET carries no body at all.
BODY_CAPS: dict[str, int] = {
    "/api/run": 256,
    "/api/reset": 256,
    "/approver/api/tap": 1024,
    "/chat": 64 * 1024,
}
JSON_POSTS = frozenset({"/api/run", "/api/reset", "/approver/api/tap"})
MAX_CHAT_MESSAGES = 60
MAX_TEXT_CHARS = 4096

TRACE = re.compile(r"^https://wandb\.ai/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/r/call/[A-Za-z0-9-]+$")
_INLINE = re.compile(r"<(script|style)>(.*?)</\1>", re.DOTALL)
_LENGTH = re.compile(r"[0-9]{1,12}")

SHARED_NOTE = "One shared demo tenant; other visitors may be tapping too."
ABSENT_WORDS = {
    "timeout": "the reviewer timed out",
    "parse": "the reviewer's answer could not be read",
    "inference": "the reviewer model could not be reached",
    "circuit-open": "the reviewer is paused after repeated failures",
    "error": "the reviewer failed",
    "already-decided": "decided before the judge read it",
    "stale": "too old to review",
    "not-seen": "decided before the judge read it",
    "record-unverifiable": "the record did not verify, so it was not reviewed",
}
FALLBACK_WORDS = {
    "inference-budget-exhausted": "the live inference budget is exhausted",
    "inference-budget-unavailable": "the live inference budget is unavailable",
    "lifetime-cap": "the live reviewer lifetime cap is exhausted",
    "daily-cap": "today's cap of {cap} live reviewer calls is used up; it resets at 00:00 UTC",
    "hourly-limit": (
        "the limit of {hourly} live reviewer calls an hour is reached; it frees up within the hour"
    ),
    "budget-unreadable": "the live reviewer's budget could not be read, so it is paused",
    "judge-unhealthy": "the live judge did not start",
}
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- responses


@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


def _json(status: int, payload: Any, **headers: str) -> Response:
    return Response(status, json.dumps(payload).encode("utf-8"), headers=dict(headers))


def _error(status: int, code: str, message: str) -> Response:
    return _json(status, {"ok": False, "code": code, "message": message})


BASE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
NOT_EMBEDDABLE = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
}


def embed_csp(
    scripts: list[str], styles: list[str], ancestors: tuple[str, ...], *, frames: bool
) -> str:
    """The CSP for an embeddable HTML page: inline code pinned by hash, same-origin fetches
    only, framed by this origin and the configured ancestors and nothing else."""
    parts = [
        "default-src 'none'",
        "script-src " + " ".join(csp_hash(s) for s in scripts) if scripts else "script-src 'none'",
        "style-src " + " ".join(csp_hash(s) for s in styles) if styles else "style-src 'none'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "frame-src 'self'" if frames else "frame-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'self' " + " ".join(ancestors),
    ]
    return "; ".join(parts)


def inline_blocks(page: str) -> tuple[list[str], list[str]]:
    scripts, styles = [], []
    for tag, body in _INLINE.findall(page):
        (scripts if tag == "script" else styles).append(body)
    return scripts, styles


# ---------------------------------------------------------------- the fake Telegram


class FakeTelegramClient:
    """The three things the front asks the fake Telegram, on loopback, bounded."""

    def __init__(
        self, base: str = f"http://127.0.0.1:{FAKE_TG_PORT}", timeout_s: float = 5.0
    ) -> None:
        self._base = base
        self._timeout = timeout_s

    def _get(self, path: str) -> tuple[int, bytes]:
        try:
            with _NO_PROXY.open(self._base + path, timeout=self._timeout) as r:
                return r.status, r.read(2 * 1024 * 1024)
        except Exception:  # noqa: BLE001 - an unreachable chat is an answer: 502 upstream
            return 0, b""

    def page(self) -> str | None:
        status, body = self._get("/")
        return body.decode("utf-8") if status == 200 else None

    def messages(self) -> list[dict[str, Any]] | None:
        status, body = self._get("/api/chat")
        if status != 200:
            return None
        try:
            messages = json.loads(body).get("messages")
        except (ValueError, AttributeError):
            return None
        return [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else None

    def tap(self, message_id: int, data: str) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(  # noqa: S310 - fixed loopback URL
            self._base + "/api/tap",
            data=json.dumps({"message_id": message_id, "data": data}).encode(),
            method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            with _NO_PROXY.open(request, timeout=15) as r:
                status, raw = r.status, r.read(64 * 1024)
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read(64 * 1024)
        except Exception:  # noqa: BLE001
            return 502, {"ok": False, "error": "the approver chat did not answer"}
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        return status, body if isinstance(body, dict) else {}


# ---------------------------------------------------------------- the judge's record


def _judge_info(entry: JudgedEntry | None, advisory: str | None, mode: str) -> dict[str, Any]:
    if entry is None:
        reason = (advisory or "").removeprefix("absent:")
        if advisory and advisory.startswith("absent:"):
            return {"state": "absent", "reason": ABSENT_WORDS.get(reason, reason)}
        return {"state": "unseen"}
    if entry.status == "claimed":
        return {"state": "reviewing"}
    if entry.status in ("notified", "silent") and entry.decision:
        trace = entry.trace_url if entry.trace_url and TRACE.match(entry.trace_url) else None
        note = "" if entry.status == "notified" else "(not delivered to the chat)"
        return {
            "state": "verdict",
            "verdict": entry.decision,
            "trace_url": trace,
            "note": note,
            "mode": mode,
        }
    reason = entry.reason or entry.status
    return {"state": "absent", "reason": ABSENT_WORDS.get(reason, reason)}


def record_view(
    state_dir: Path, run_id: str | None, mode: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(record, judge info per scenario of ``run_id``) from the judge's state file, read the
    way the console's live view reads it: a cache the judge writes atomically, never the log."""
    try:
        state = JudgeState.model_validate_json((state_dir / STATE_FILENAME).read_bytes())
    except (OSError, ValidationError, ValueError):
        return {"readable": False, "counters": counters(JudgeState()), "recent": []}, {}
    recent = []
    for key, decision in state.decisions.items():
        judged = state.judged.get(key)
        human = human_decision(decision.event)
        verdict = judged.decision if judged else None
        trace = (
            judged.trace_url
            if judged and judged.trace_url and TRACE.match(judged.trace_url)
            else None
        )
        recent.append(
            {
                "seq": decision.seq,
                "action_class": judged.action_class if judged else None,
                "event": decision.event.removeprefix("approval."),
                "human": human,
                "verdict": verdict,
                "label": agreement(verdict, human),
                "advisory": decision.advisory,
                "feedback": decision.feedback,
                "trace_url": trace,
            }
        )
    recent.sort(key=lambda r: r["seq"], reverse=True)
    per_scenario: dict[str, Any] = {}
    if run_id:
        prefix = f"hook:demo-{run_id}-"
        for key in {*state.judged, *state.decisions}:
            if key.startswith(prefix):
                scenario = key[len(prefix) :].split(":", 1)[0]
                decision = state.decisions.get(key)
                per_scenario[scenario] = _judge_info(
                    state.judged.get(key), decision.advisory if decision else None, mode
                )
    record = {
        "readable": True,
        "counters": counters(state),
        "recent": recent[:8],
        "chain_break": state.chain_break is not None,
    }
    return record, per_scenario


# ---------------------------------------------------------------- the app


class App:
    """Routing and policy, independent of sockets (tests drive ``handle`` directly)."""

    def __init__(
        self,
        *,
        settings: Settings,
        judge_state_dir: Path,
        runs: RunManager,
        chat: FakeTelegramClient,
        budget: LiveBudget,
        health: Callable[[], dict[str, bool]],
        judge_worker: Callable[[], dict[str, Any]],
        log_verify: Callable[[], dict[str, Any]],
        request_verify: Callable[[], None],
        next_reviewer: Callable[[], tuple[str, str | None]],
        chat_generation: Callable[[], int],
        booted: Callable[[], bool],
        private_mode: bool = False,
    ) -> None:
        self.settings = settings
        self.judge_state_dir = judge_state_dir
        self.runs = runs
        self.chat = chat
        self.budget = budget
        self.health = health
        self.judge_worker = judge_worker
        self.log_verify = log_verify
        self.request_verify = request_verify
        self.next_reviewer = next_reviewer
        self.chat_generation = chat_generation
        self.booted = booted
        self.private_mode = private_mode
        self._view_lock = threading.Lock()
        self._view: tuple[int, int] = (0, 0)  # (fake Telegram generation, floor message id)
        self._last_verify_request = 0.0
        #: Polled views are shared by every visitor for a moment, so a crowd polling the page
        #: costs the loopback children the same as one visitor. Actions always read fresh.
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        page = render_page(settings.public_base_path)
        self._page = page.encode("utf-8")
        self._page_csp = embed_csp(
            [PAGE_SCRIPT], [PAGE_STYLE], settings.frame_ancestors, frames=True
        )
        self.routes: dict[str, dict[str, Callable[[bytes], Response]]] = {
            "/": {"GET": self.page},
            "/health": {"GET": self.health_route},
            "/api/state": {"GET": self.state},
            "/api/run": {"POST": self.run},
            "/api/reset": {"POST": self.reset},
            "/approver": {"GET": self.approver_redirect},
            "/approver/": {"GET": self.approver_page},
            "/approver/api/chat": {"GET": self.approver_chat},
            "/approver/api/tap": {"POST": self.tap},
            "/chat": {"POST": self.platform_chat},
        }

    # ------------------------------------------------------------ dispatch

    def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str],
        read_body: Callable[[int], bytes],
    ) -> Response:
        path = target.split("?", 1)[0].split("#", 1)[0]
        route = self.routes.get(path)
        if route is None:
            return self._finish(_error(404, "not-found", "Not found."), embeddable=False)
        verb = "GET" if method == "HEAD" else method
        handler = route.get(verb)
        if handler is None:
            response = _error(405, "method-not-allowed", f"{path} answers {', '.join(route)}.")
            response.headers["Allow"] = ", ".join(
                sorted({*route, *(["HEAD"] if "GET" in route else [])})
            )
            return self._finish(response, embeddable=False)
        body = self._read(verb, path, headers, read_body)
        if isinstance(body, Response):
            return self._finish(body, embeddable=False)
        try:
            response = handler(body)
        except Exception as exc:  # noqa: BLE001 - one bad request never takes the front down
            log("tryit.front.error", level="error", path=path, error=type(exc).__name__)
            response = _error(500, "internal-error", "Something went wrong; try again.")
        return self._finish(response, embeddable=response.content_type.startswith("text/html"))

    def _read(
        self, verb: str, path: str, headers: Mapping[str, str], read_body: Callable[[int], bytes]
    ) -> bytes | Response:
        if "transfer-encoding" in headers:
            return _error(
                411, "length-required", "Send a Content-Length; chunked bodies are refused."
            )
        raw = headers.get("content-length")
        cap = BODY_CAPS.get(path, 0) if verb == "POST" else 0
        length = 0
        if raw is not None:
            if not _LENGTH.fullmatch(raw.strip()):
                return _error(400, "bad-length", "Content-Length is not a number.")
            length = int(raw.strip())
        if length > cap:
            return _error(413, "body-too-large", f"{path} reads at most {cap} bytes.")
        if verb == "POST" and path in JSON_POSTS:
            ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return _error(415, "json-required", "POST application/json.")
        return read_body(length) if length else b""

    def _finish(self, response: Response, *, embeddable: bool) -> Response:
        headers = dict(BASE_HEADERS)
        if not embeddable:
            headers.update(NOT_EMBEDDABLE)
        headers.update(response.headers)
        headers["Content-Type"] = (
            "application/json"
            if response.content_type == "application/json"
            else response.content_type
        )
        response.headers = headers
        return response

    def _embeddable_html(self, body: bytes, csp: str) -> Response:
        return Response(
            200,
            body,
            content_type="text/html; charset=utf-8",
            headers={
                "Content-Security-Policy": csp,
                "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
            },
        )

    # ------------------------------------------------------------ pages

    def page(self, _body: bytes) -> Response:
        return self._embeddable_html(self._page, self._page_csp)

    def approver_redirect(self, _body: bytes) -> Response:
        # Relative, so it resolves under whatever prefix the platform's proxy stripped.
        return Response(308, b"", content_type="text/plain", headers={"Location": "approver/"})

    def approver_page(self, _body: bytes) -> Response:
        page = self.chat.page()
        if page is None:
            return _error(502, "chat-unavailable", "The approver chat is not answering yet.")
        scripts, styles = inline_blocks(page)
        csp = embed_csp(scripts, styles, self.settings.frame_ancestors, frames=False)
        return self._embeddable_html(page.encode("utf-8"), csp)

    # ------------------------------------------------------------ health and state

    def _parts(self) -> dict[str, bool]:
        parts = dict(self.health())
        parts["front"] = True
        return parts

    def health_route(self, _body: bytes) -> Response:
        parts = self._parts()
        ok = self.booted() and all(
            v for k, v in parts.items() if not self.private_mode or k != "judge"
        )
        return _json(200 if ok else 503, {"status": "ok" if ok else "unhealthy", "parts": parts})

    def _mode(self) -> tuple[str, str | None]:
        """The reviewer the run in progress uses, or else the one the NEXT run will use (the
        judge process switches at a run's start, so between runs it may still be the last
        run's). ``(mode, why live is paused)``."""
        run = self.runs.active()
        if run is not None and run.state != "preparing":
            return run.reviewer, run.fallback
        return self.next_reviewer()

    def reviewer(self) -> dict[str, Any]:
        s = self.settings
        mode, fallback = self._mode()
        if self.private_mode and fallback:
            label = "Live reviewer unavailable: " + FALLBACK_WORDS.get(fallback, fallback).format(
                cap=s.live_daily_cap, hourly=s.live_calls_per_hour
            )
        elif mode == "live":
            label = (
                f"Live reviewer: W&B Inference ({s.reviewer_model}), each verdict traced to "
                f"Weave ({s.weave_project})"
            )
        elif self.private_mode:
            label = "Live reviewer unavailable: " + str(fallback or "the model did not answer")
        elif fallback:
            words = FALLBACK_WORDS.get(fallback, fallback).format(
                cap=s.live_daily_cap, hourly=s.live_calls_per_hour
            )
            label = f"Offline reviewer for now (hermetic, no model call): {words}"
        else:
            label = "Offline reviewer, hermetic demo: deterministic rules, no model call, no cost"
        out: dict[str, Any] = {
            "mode": mode,
            "label": label,
            "fallback": fallback,
            "advisory_note": "The AI judge is advisory: it never approves or blocks anything.",
        }
        if s.live_ready:
            out["budget"] = self.budget.snapshot()
        return out

    def _cached(self, key: str, ttl_s: float, compute: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < ttl_s:
                return hit[1]
        value = compute()
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), value)
        return value

    def _forget(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def state(self, _body: bytes) -> Response:
        return _json(200, self._cached("state", 1.0, self._state))

    def _state(self) -> dict[str, Any]:
        run = self.runs.snapshot()
        mode = run["reviewer"] if run else self._mode()[0]
        record, per_scenario = record_view(self.judge_state_dir, run["id"] if run else None, mode)
        if run:
            for scenario in run["scenarios"]:
                scenario["judge"] = per_scenario.get(scenario["name"])
        if time.monotonic() - self._last_verify_request > 20:
            self._last_verify_request = time.monotonic()
            self.request_verify()
        worker = self.judge_worker()
        record["judge_chain"] = (
            "chain-break" if record.get("chain_break") else worker.get("chain", "unknown")
        )
        record["log_verify"] = self.log_verify()
        parts = self._parts()
        return {
            "health": {
                "ok": self.booted()
                and all(v for k, v in parts.items() if not self.private_mode or k != "judge"),
                "parts": parts,
            },
            "reviewer": self.reviewer(),
            "shared_note": "Private demo session. Browser messaging transport is simulated."
            if self.private_mode
            else SHARED_NOTE,
            "window_s": self.settings.window_s,
            "limits": self.runs.limits(),
            "run": run,
            "record": record,
        }

    # ------------------------------------------------------------ the chat view

    def _floor(self) -> int:
        with self._view_lock:
            generation, floor = self._view
        return floor if generation == self.chat_generation() else 0

    def _set_floor(self, floor: int) -> None:
        with self._view_lock:
            self._view = (self.chat_generation(), floor)

    def approver_chat(self, _body: bytes) -> Response:
        messages = self._cached("chat", 0.5, self.chat.messages)
        if messages is None:
            return _error(502, "chat-unavailable", "The approver chat is not answering yet.")
        floor = self._floor()
        shown = []
        for m in messages:
            mid = m.get("message_id")
            if not isinstance(mid, int) or mid <= floor:
                continue
            buttons = [
                {"text": str(b.get("text", ""))[:64], "data": str(b.get("data", ""))[:64]}
                for b in (m.get("buttons") or [])
                if isinstance(b, dict)
            ]
            shown.append(
                {
                    "message_id": mid,
                    "text": str(m.get("text") or "")[:MAX_TEXT_CHARS],
                    "buttons": buttons,
                    "edited": bool(m.get("edited")),
                    "bot": {"7001": "gate", "7002": "judge"}.get(str(m.get("bot"))),
                }
            )
        return _json(200, {"messages": shown[-MAX_CHAT_MESSAGES:]})

    # ------------------------------------------------------------ actions

    def run(self, _body: bytes) -> Response:
        if not self.booted() or not all(
            v for k, v in self._parts().items() if not self.private_mode or k != "judge"
        ):
            return _error(
                503, "starting", "The demo is still starting; try again in a few seconds."
            )
        messages = self.chat.messages()
        if messages is None:
            return _error(503, "chat-unavailable", "The approver chat is not answering yet.")
        floor = max(
            (m["message_id"] for m in messages if isinstance(m.get("message_id"), int)), default=0
        )
        result = self.runs.start(floor)
        if result.status == 202:
            self._set_floor(floor)
            self._forget()
        headers = {}
        if result.retry_after_s is not None:
            headers["Retry-After"] = str(int(result.retry_after_s) + 1)
        payload = {"ok": result.status == 202, "code": result.code, "message": result.message}
        if result.retry_after_s is not None:
            payload["retry_after_s"] = result.retry_after_s
        return _json(result.status, payload, **headers)

    def reset(self, _body: bytes) -> Response:
        if self.runs.active() is not None:
            return _error(
                409, "run-in-progress", "A run is in progress; Reset is available when it ends."
            )
        messages = self.chat.messages() or []
        floor = max(
            (m["message_id"] for m in messages if isinstance(m.get("message_id"), int)), default=0
        )
        self._set_floor(max(floor, self._floor()))
        self.runs.clear()
        self._forget()
        return _json(
            200, {"ok": True, "code": "reset", "message": "View cleared. The log is untouched."}
        )

    def tap(self, body: bytes) -> Response:
        try:
            payload = json.loads(body or b"{}")
            message_id = payload["message_id"]
            data = payload["data"]
        except (ValueError, KeyError, TypeError):
            return _error(400, "bad-tap", "A tap is {message_id, data}.")
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or not isinstance(data, str)
            or len(data) > 64
        ):
            return _error(400, "bad-tap", "A tap is {message_id, data}.")
        run = self.runs.active()
        if run is None:
            return _error(409, "no-active-run", REFUSALS["no-active-run"])
        messages = self.chat.messages()
        if messages is None:
            return _error(502, "chat-unavailable", "The approver chat is not answering.")
        refusal = tap_refusal(
            messages, message_id, data, run_id=run.id, floor=run.floor, tapped=run.tapped
        )
        if refusal is not None:
            return _error(403 if refusal != "already-tapped" else 409, refusal, REFUSALS[refusal])
        if not self.runs.claim_tap(run, message_id):
            return _error(409, "already-tapped", REFUSALS["already-tapped"])
        status, answer = self.chat.tap(message_id, data)
        delivered = status == 200 and answer.get("ok") is True
        if not delivered:
            self.runs.release_tap(run, message_id)
            return _error(502, "tap-not-delivered", "The tap did not reach the gate; try again.")
        self._forget()
        return _json(200, {"ok": True, "code": "delivered", "message": "Delivered to the gate."})

    def platform_chat(self, _body: bytes) -> Response:
        return _json(
            200,
            {
                "ok": False,
                "code": "chat-unsupported",
                "reply": (
                    "No conversation here. This machine serves the Approved try-it page; "
                    "open it in a browser."
                ),
            },
        )


# ---------------------------------------------------------------- the HTTP adapter


class _Handler(BaseHTTPRequestHandler):
    server_version = "approved-tryit"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    timeout = 15  # seconds a slow client may hold a connection

    def _dispatch(self) -> None:
        headers = {k.lower(): v for k, v in self.headers.items()}
        app = cast(_Server, self.server).app
        response = app.handle(self.command, self.path, headers, self.rfile.read)
        self.send_response(response.status)
        for name, value in BASE_HEADERS.items():
            if name not in response.headers:
                self.send_header(name, value)
        if "Content-Type" not in response.headers:
            self.send_header("Content-Type", response.content_type)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _dispatch

    def log_message(self, format: str, *args: Any) -> None:
        return  # no access log: request lines are visitor text


BUSY = b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: Any, max_active: int = 64) -> None:
        self.app = app
        self._slots = threading.BoundedSemaphore(max_active)
        super().__init__(address, _Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            with contextlib.suppress(OSError):
                request.sendall(BUSY)
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(app: Any, port: int) -> ThreadingHTTPServer:
    return _Server(("0.0.0.0", port), app)  # noqa: S104 - the one public listener, by contract
