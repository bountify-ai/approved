"""The try-it image: its supervisor's settings, the run slot and its limits, the live reviewer's
budget, the tap ownership check, the public front's routes and headers, and the image files
kept in step with the demo and the daemon image. No socket is opened: the front is driven
through ``App.handle`` and the fake Telegram is an in-memory fake."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from approved.state import DecisionRecord, JudgedEntry, StateStore
from approved.tryit import config as tryit_config
from approved.tryit.config import (
    DAEMON_COMPOSE_ENV,
    FAKE_TG_COMPOSE_ENV,
    Layout,
    TryitConfigError,
    load_settings,
)
from approved.tryit.credentials import generate
from approved.tryit.front import App, Response, embed_csp, inline_blocks, record_view
from approved.tryit.page import PAGE_SCRIPT, PAGE_STYLE, csp_hash, render_page
from approved.tryit.runs import (
    LiveBudget,
    Run,
    RunLimiter,
    RunManager,
    classify_outcome,
    load_scenarios,
    tap_refusal,
)
from approved.tryit.supervisor import Reaper, Supervisor

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "demo" / "agent" / "agent.py"


# ---------------------------------------------------------------- the image files


def _compose_env(service: str) -> dict[str, str]:
    """``environment:`` of one service in demo/compose.yaml (plain ``KEY: value`` lines)."""
    lines = (ROOT / "demo" / "compose.yaml").read_text().splitlines()
    start = lines.index(f"  {service}:")
    env: dict[str, str] = {}
    inside = False
    for line in lines[start + 1 :]:
        if re.match(r"^  \S", line):
            break
        if line.strip() == "environment:":
            inside = True
            continue
        if inside:
            match = re.match(r"^      ([A-Z_]+): (.*)$", line)
            if not match:
                break
            env[match.group(1)] = match.group(2).strip().strip('"')
    return env


def test_tryit_children_get_the_compose_files_environment() -> None:
    assert _compose_env("daemon") == DAEMON_COMPOSE_ENV
    assert _compose_env("fake-telegram") == FAKE_TG_COMPOSE_ENV
    settings = load_settings({"PORT": "18789"})
    daemon = settings.daemon_env({"APPROVAL_SERVE_AGENT_TOKEN": "a" * 48})
    for key, value in DAEMON_COMPOSE_ENV.items():
        expected = value.replace("http://fake-telegram:8081", "http://127.0.0.1:8081")
        assert daemon[key] == expected, key
    assert settings.fake_tg_env()["FORWARD_WEBHOOK_URL"] == "http://127.0.0.1:8080/telegram/webhook"


def _stage(text: str, header: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(header)
    out = []
    for line in lines[start + 1 :]:
        if line.startswith("FROM "):
            break
        out.append(line)
    while out and (not out[-1].strip() or out[-1].startswith("#")):
        out.pop()
    return out


def test_tryit_image_builds_the_daemon_images_runtime_verbatim() -> None:
    daemon = (ROOT / "images" / "daemon" / "Dockerfile").read_text()
    tryit = (ROOT / "images" / "tryit" / "Dockerfile").read_text()
    header = "FROM ${NODE_IMAGE} AS build"
    assert _stage(tryit, header) == _stage(daemon, header)
    for arg in ("ARG NODE_IMAGE=", "ARG APPROVAL_MD_REPO=", "ARG APPROVAL_MD_COMMIT="):
        [d] = [x for x in daemon.splitlines() if x.startswith(arg)]
        [t] = [x for x in tryit.splitlines() if x.startswith(arg)]
        assert d == t
    # The daemon image's own supervisor, copied from its file, never a fork of it.
    assert "COPY images/daemon/entrypoint.mjs /opt/image/entrypoint.mjs" in tryit


def test_tryit_image_builds_the_judge_as_the_root_dockerfile_does() -> None:
    def steps(path: Path) -> list[str]:
        return [
            line
            for line in path.read_text().splitlines()
            if line.startswith(("COPY service/", "RUN uv", "FROM python")) or "UV_" in line
        ]

    assert steps(ROOT / "images" / "tryit" / "Dockerfile") == steps(ROOT / "Dockerfile")


def test_tryit_image_command_and_children_use_absolute_interpreters(tmp_path: Path) -> None:
    dockerfile = (ROOT / "images" / "tryit" / "Dockerfile").read_text()
    [entry] = [x for x in dockerfile.splitlines() if x.startswith("ENTRYPOINT")]
    argv = json.loads(entry.removeprefix("ENTRYPOINT").strip())
    assert argv[0].startswith("/")
    assert argv[1:] == ["-m", "approved.tryit"]
    sup = Supervisor(load_settings({"PORT": "18789"}), Layout(tmp_path), Reaper())
    for child in sup.children:
        assert child.argv[0].startswith("/"), child.name
        assert all(not a.startswith("./") for a in child.argv)
    for name in ("NODE", "JUDGE_PYTHON", "AGENT_PYTHON", "SH", "AGENT_SCRIPT", "INIT_SCRIPT"):
        assert getattr(tryit_config, name).startswith("/"), name


def test_tryit_dockerignore_admits_every_copied_source() -> None:
    dockerfile = (ROOT / "images" / "tryit" / "Dockerfile").read_text()
    ignore = (ROOT / "images" / "tryit" / "Dockerfile.dockerignore").read_text().splitlines()
    admitted = {line[1:].rstrip("/") for line in ignore if line.startswith("!")}
    sources = re.findall(r"^COPY (?!--from)(\S+) ", dockerfile, re.MULTILINE)
    assert sources
    for source in sources:
        assert source.rstrip("/") in admitted, source
        assert (ROOT / source).exists(), source


# ---------------------------------------------------------------- settings and credentials


def test_tryit_settings_fail_closed() -> None:
    with pytest.raises(TryitConfigError, match="PORT"):
        load_settings({})
    with pytest.raises(TryitConfigError, match="PORT"):
        load_settings({"PORT": "8080"})  # a child's loopback port
    with pytest.raises(TryitConfigError, match="TRYIT_FRAME_ANCESTORS"):
        load_settings({"PORT": "18789", "TRYIT_FRAME_ANCESTORS": "*"})
    with pytest.raises(TryitConfigError, match="TRYIT_HOOK_HARNESS_CAP_S"):
        load_settings({"PORT": "18789", "TRYIT_HOOK_HARNESS_CAP_S": "30"})
    settings = load_settings({"PORT": "18789"})
    assert settings.frame_ancestors == ("https://approval.md",)
    assert settings.window_s == 240
    assert settings.agent_wait_s > settings.window_s + 30  # the gate's expiry always lands first


def test_tryit_live_mode_needs_the_flag_a_key_and_a_model() -> None:
    assert not load_settings({"PORT": "1", "WANDB_API_KEY": "k", "REVIEWER_MODEL": "m"}).live_ready
    no_model = load_settings({"PORT": "1", "TRYIT_LIVE": "1", "WANDB_API_KEY": "k"})
    assert not no_model.live_ready
    assert "REVIEWER_MODEL" in (no_model.live_not_ready_reason or "")
    live = load_settings(
        {"PORT": "1", "TRYIT_LIVE": "1", "WANDB_API_KEY": "wandb-value", "REVIEWER_MODEL": "m"}
    )
    assert live.live_ready
    assert "wandb-value" not in repr(live)


def test_tryit_judge_and_agent_environments_hold_only_their_own_credentials(tmp_path: Path) -> None:
    env = {
        "PORT": "18789",
        "TRYIT_LIVE": "1",
        "WANDB_API_KEY": "wandb-value",
        "REVIEWER_MODEL": "m",
        "APPROVAL_SERVE_AGENT_TOKEN": "leak",
        "AWS_SECRET_ACCESS_KEY": "leak",
    }
    settings = load_settings(env)
    layout = Layout(tmp_path)
    offline = settings.judge_env(layout, live=False)
    live = settings.judge_env(layout, live=True)
    agent = settings.agent_env(layout, run_id="t1", scenario="read")
    assert "WANDB_API_KEY" not in offline
    assert live["WANDB_API_KEY"] == "wandb-value"
    assert offline["OFFLINE"] == "1"
    assert live["OFFLINE"] == "0"
    assert offline["CONSOLE_ENABLED"] == "0"
    assert offline["CONSOLE_HOST"] == "127.0.0.1"
    assert offline["POLICY_FILE"] == str(layout.store / "APPROVAL.md")
    assert live["POLICY_FILE"] == str(layout.store / "APPROVAL.md")
    other = settings.judge_env(Layout(tmp_path / "other-session"), live=True)
    assert other["POLICY_FILE"] != live["POLICY_FILE"]
    assert "POLICY_FILE" not in agent
    assert "POLICY_FILE" not in settings.daemon_env({}, layout)
    assert "PORT" not in offline  # PORT would win over CONSOLE_PORT in approved.config
    for child in (offline, live, agent):
        assert "leak" not in child.values()
    assert "WANDB_API_KEY" not in agent
    assert "TENANT_TOKEN_FILE" not in agent
    assert agent["AGENT_TOKEN_FILE"].endswith("/agent_token")
    assert offline["TENANT_TOKEN_FILE"].endswith("/tenant_token")


def test_tryit_credentials_are_fresh_0600_in_a_0700_dir(tmp_path: Path) -> None:
    directory = tmp_path / "tryit" / "secrets"
    first = generate(directory)
    second = generate(directory)
    assert set(first.values()).isdisjoint(second.values())
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in (
        "agent_token",
        "tenant_token",
        "webhook_secret",
        "gate_bot_token",
        "judge_bot_token",
    ):
        path = directory / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_text() == getattr(second, name)
    assert "redacted" in repr(second)
    assert second.agent_token not in repr(second)
    assert len(second.agent_token) >= 24
    assert second.agent_token != second.tenant_token


# ---------------------------------------------------------------- limits and budget


def test_tryit_run_limiter_min_interval_and_hourly() -> None:
    now = [1000.0]
    limiter = RunLimiter(20, 3, clock=lambda: now[0])
    assert limiter.retry_after() is None
    limiter.record()
    assert limiter.retry_after() == pytest.approx(20)
    now[0] += 21
    assert limiter.retry_after() is None
    limiter.record()
    now[0] += 21
    limiter.record()
    now[0] += 21
    wait = limiter.retry_after()  # three in the hour
    assert wait is not None
    assert wait == pytest.approx(3600 - 63)
    now[0] += wait
    assert limiter.retry_after() is None


def test_tryit_live_budget_caps_per_day_and_hour_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    now = [1_790_700_000.0]  # 2026-09-29
    budget = LiveBudget(path, daily_cap=8, per_hour=6, clock=lambda: now[0])
    assert budget.reserve(4) is None
    assert budget.reserve(4) == "hourly-limit"  # 8 > 6 an hour
    now[0] += 3601
    assert budget.reserve(4) is None
    # A new process reads the same file: the daily cap survives a restart.
    again = LiveBudget(path, daily_cap=8, per_hour=6, clock=lambda: now[0])
    now[0] += 3601
    assert again.check(4) == "daily-cap"
    assert again.reserve(4) == "daily-cap"
    assert again.snapshot()["used_today"] == 8
    now[0] += 86_400  # the next UTC day
    assert again.reserve(4) is None


def test_tryit_unreadable_budget_counts_as_spent(tmp_path: Path) -> None:
    path = tmp_path / "budget.json"
    path.write_text("not json")
    assert LiveBudget(path, daily_cap=300, per_hour=60).reserve(4) == "budget-unreadable"
    assert LiveBudget(None, daily_cap=0, per_hour=60).check(1) == "daily-cap"


# ---------------------------------------------------------------- outcomes and taps


def test_tryit_outcomes_are_classified() -> None:
    assert classify_outcome("ALLOWED")[0] == "allowed"
    assert classify_outcome("BLOCKED: hook-rejected: a human rejected hook:x")[0] == "rejected"
    assert (
        classify_outcome("BLOCKED: hook-expired: the request for hook:x lapsed before a decision")[
            0
        ]
        == "expired"
    )
    assert classify_outcome("BLOCKED (no decision within 330s): ...")[0] == "timed-out"
    assert classify_outcome("BLOCKED (facade HTTP 503)")[0] == "blocked"
    assert classify_outcome(None)[0] == "blocked"


def _chat(run_id: str, first: int = 10) -> list[dict[str, Any]]:
    key = f"hook:demo-{run_id}-push-main:3f8a0d15336014e7:vcs.push.main"
    return [
        {
            "bot": "7002",
            "message_id": first,
            "text": "Judge (advisory AI, not an approval)",
            "buttons": [],
        },
        {"bot": "7001", "message_id": first + 1, "text": "<b>1 pending</b>", "buttons": []},
        {
            "bot": "7001",
            "message_id": first + 2,
            "text": f"<b>APPROVAL REQUIRED</b>\n<code>{key}</code>",
            "buttons": [],
        },
        {"bot": "7001", "message_id": first + 3, "text": "<b>PAYLOAD</b>", "buttons": []},
        {
            "bot": "7001",
            "message_id": first + 4,
            "text": "<b>WHAT THIS DOES</b>",
            "buttons": [
                {"text": "Approve", "data": "g:1:ab"},
                {"text": "Reject", "data": "r:1:ab"},
            ],
        },
    ]


def test_tryit_taps_only_reach_prompts_the_current_run_opened() -> None:
    chat = _chat("tcurrent")
    ok = {"run_id": "tcurrent", "floor": 9}
    assert tap_refusal(chat, 14, "r:1:ab", **ok) is None
    assert tap_refusal(chat, 14, "r:1:ab", run_id="tother", floor=9) == "not-this-run"
    assert tap_refusal(chat, 14, "r:1:ab", run_id="tcurrent", floor=14) == "not-this-run"
    assert tap_refusal(chat, 14, "x:forged", **ok) == "not-a-button"
    assert tap_refusal(chat, 10, "r:1:ab", **ok) == "no-buttons"  # the judge's message
    assert tap_refusal(chat, 99, "r:1:ab", **ok) == "unknown-message"
    assert tap_refusal(chat, 14, "r:1:ab", tapped={14}, **ok) == "already-tapped"
    # A header outside the run's window does not vouch for a later button message.
    assert tap_refusal(chat, 14, "r:1:ab", run_id="tcurrent", floor=12) == "not-this-run"


def test_tryit_scenarios_come_from_the_demo_agent_itself() -> None:
    names = [n for n, _, _ in load_scenarios(AGENT)]
    assert names == ["read", "push-main", "push-branch", "force-push"]


# ---------------------------------------------------------------- the run slot


def _script(
    outcomes: dict[str, str], delay: float = 0.0
) -> Callable[[str, str], subprocess.Popen[bytes]]:
    def spawn(run_id: str, scenario: str) -> subprocess.Popen[bytes]:
        code = (
            f"import time; print('-> {scenario}: `cmd`', flush=True); time.sleep({delay}); "
            f"print('   OUTCOME {scenario}: ' + {outcomes.get(scenario, 'ALLOWED')!r})"
        )
        return subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", code], stdout=subprocess.PIPE, start_new_session=True
        )

    return spawn


def _manager(spawn: Callable[[str, str], subprocess.Popen[bytes]], **kwargs: Any) -> RunManager:
    return RunManager(
        scenarios=load_scenarios(AGENT),
        spawn=spawn,
        wait_exit=lambda proc: proc.wait(),
        limiter=kwargs.pop("limiter", RunLimiter(0, 100)),
        **kwargs,
    )


def _wait_done(manager: RunManager, timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = manager.snapshot()
        if snap and snap["state"] == "done":
            return snap
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_tryit_one_run_at_a_time_then_rate_limited() -> None:
    now = [0.0]
    manager = _manager(
        _script({"push-main": "BLOCKED: hook-rejected: no"}, delay=0.3),
        limiter=RunLimiter(20, 60, clock=lambda: now[0]),
    )
    first = manager.start(floor=0)
    assert first.status == 202
    assert manager.start(floor=0).status == 409
    assert not manager.clear()  # Reset is refused during a run
    snap = _wait_done(manager)
    assert [s["status"] for s in snap["scenarios"]] == ["allowed", "rejected", "allowed", "allowed"]
    limited = manager.start(floor=0)
    assert limited.status == 429
    assert limited.retry_after_s is not None
    now[0] += 21
    assert manager.start(floor=0).status == 202
    _wait_done(manager)
    assert manager.clear()
    assert manager.snapshot() is None


def test_tryit_an_expired_request_ends_the_run() -> None:
    ended: list[Run] = []
    manager = _manager(_script({"push-main": "BLOCKED: hook-expired: lapsed"}), on_end=ended.append)
    assert manager.start(floor=0).status == 202
    snap = _wait_done(manager)
    assert [s["status"] for s in snap["scenarios"]] == ["allowed", "expired", "not-run", "not-run"]
    assert "expired" in (snap["end_reason"] or "")
    assert ended
    assert ended[0].state == "done"


def test_tryit_prepare_picks_the_reviewer_before_the_agent_runs() -> None:
    order: list[str] = []

    def prepare(run: Run) -> None:
        order.append("prepare")
        run.reviewer, run.fallback = "offline", "daily-cap"

    def spawn(run_id: str, scenario: str) -> subprocess.Popen[bytes]:
        order.append(scenario)
        return _script({})(run_id, scenario)

    manager = _manager(spawn, prepare=prepare)
    manager.start(floor=0)
    snap = _wait_done(manager)
    assert order[0] == "prepare"
    assert snap["fallback"] == "daily-cap"


# ---------------------------------------------------------------- the front


class FakeChat:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages_list = messages or []
        self.taps: list[tuple[int, str]] = []

    def page(self) -> str:
        return "<!doctype html><style>b{}</style><script>fetch('api/chat')</script>"

    def messages(self) -> list[dict[str, Any]]:
        return list(self.messages_list)

    def tap(self, message_id: int, data: str) -> tuple[int, dict[str, Any]]:
        self.taps.append((message_id, data))
        return 200, {"ok": True, "webhook_status": 200}


SECRET = "c0ffee" * 8


def _front(
    tmp_path: Path,
    *,
    booted: bool = True,
    parts_ok: bool = True,
    reviewer: tuple[str, str | None] = ("offline", None),
    env: dict[str, str] | None = None,
):
    settings = load_settings({"PORT": "18789", **(env or {})})
    chat = FakeChat()
    manager = _manager(_script({}, delay=5.0), limiter=RunLimiter(0, 100))
    app = App(
        settings=settings,
        judge_state_dir=tmp_path / "judge",
        runs=manager,
        chat=chat,  # type: ignore[arg-type]
        budget=LiveBudget(None, daily_cap=settings.live_daily_cap, per_hour=60),
        health=lambda: dict.fromkeys(("daemon", "fake_telegram", "judge"), parts_ok),
        judge_worker=lambda: {"chain": "verified"},
        log_verify=lambda: {"status": "clean", "records": 12},
        request_verify=lambda: None,
        next_reviewer=lambda: reviewer,
        chat_generation=lambda: 1,
        booted=lambda: booted,
    )
    return app, chat, manager


def _call(app: App, method: str, path: str, body: bytes = b"", **headers: str) -> Response:
    hdrs = {k.replace("_", "-"): v for k, v in headers.items()}
    if body:
        hdrs.setdefault("content-length", str(len(body)))
    return app.handle(method, path, hdrs, lambda n: body[:n])


FORBIDDEN = [
    "/log/follow",
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
    "/static/console.js",
    "/downloads/hermes-hook-shim.sh",
    "/api/chat",
    "/api/tap",
    "/approver/api/../../log/follow",
    "/approver/bot7001:x/sendMessage",
    "/approver/healthz",
    "//log/follow",
    "/api/state/",
]


def test_tryit_front_exposes_only_its_own_routes(tmp_path: Path) -> None:
    app, _chat, _ = _front(tmp_path)
    for path in FORBIDDEN:
        for method in ("GET", "POST"):
            response = _call(app, method, path)
            assert response.status == 404, (method, path)
    assert _call(app, "DELETE", "/api/run").status == 405
    assert _call(app, "GET", "/api/run").status == 405
    assert _call(app, "POST", "/").status == 405
    assert _call(app, "GET", "/?x=1").status == 200


def test_tryit_front_caps_bodies_and_requires_json(tmp_path: Path) -> None:
    app, _chat, _ = _front(tmp_path)
    big = b"{" + b" " * 2000 + b"}"
    assert (
        _call(app, "POST", "/approver/api/tap", big, content_type="application/json").status == 413
    )
    assert (
        _call(app, "POST", "/api/run", b"{}" + b" " * 300, content_type="application/json").status
        == 413
    )
    assert _call(app, "GET", "/api/state", b"x").status == 413
    assert _call(app, "POST", "/api/run", b"{}", content_type="text/plain").status == 415
    for bad_length in ("\u00b2", "-1", "1e3", " "):
        odd = app.handle("POST", "/api/run", {"content-length": bad_length}, lambda n: b"")
        assert odd.status == 400, bad_length
    chunked = app.handle("POST", "/api/run", {"transfer-encoding": "chunked"}, lambda n: b"")
    assert chunked.status == 411
    assert _call(app, "POST", "/chat", b"x" * (64 * 1024 + 1)).status == 413
    assert _call(app, "POST", "/chat", b'{"message":"hi"}').status == 200


def test_tryit_front_headers_embed_only_on_the_configured_origin(tmp_path: Path) -> None:
    app, _chat, _ = _front(tmp_path)
    page = _call(app, "GET", "/")
    csp = page.headers["Content-Security-Policy"]
    assert csp.endswith("frame-ancestors 'self' https://approval.md")
    assert "X-Frame-Options" not in page.headers
    assert csp_hash(PAGE_SCRIPT) in csp
    assert csp_hash(PAGE_STYLE) in csp
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    approver = _call(app, "GET", "/approver/")
    assert approver.headers["Content-Security-Policy"].endswith(
        "frame-ancestors 'self' https://approval.md"
    )
    assert "X-Frame-Options" not in approver.headers
    for path in ("/api/state", "/health", "/nope", "/approver/api/chat"):
        response = _call(app, "GET", path)
        assert response.headers["X-Frame-Options"] == "DENY", path
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"], path
    for response in (page, approver, _call(app, "GET", "/api/state")):
        assert "Set-Cookie" not in response.headers
        assert response.headers["Cache-Control"] == "no-store"


def test_tryit_embed_csp_pins_the_fake_telegram_pages_inline_code() -> None:
    page = "<style>a{}</style><script>x()</script>"
    scripts, styles = inline_blocks(page)
    csp = embed_csp(scripts, styles, ("https://approval.md",), frames=False)
    assert csp_hash("x()") in csp
    assert csp_hash("a{}") in csp
    assert "frame-src 'none'" in csp


def test_tryit_health_is_ok_only_when_everything_is_up(tmp_path: Path) -> None:
    assert _call(_front(tmp_path)[0], "GET", "/health").status == 200
    assert _call(_front(tmp_path, booted=False)[0], "GET", "/health").status == 503
    down = _front(tmp_path, parts_ok=False)[0]
    assert _call(down, "GET", "/health").status == 503
    assert _call(down, "POST", "/api/run", b"{}", content_type="application/json").status == 503


def test_tryit_front_run_tap_and_reset(tmp_path: Path) -> None:
    app, chat, manager = _front(tmp_path)
    chat.messages_list = [{"bot": "7001", "message_id": 3, "text": "old", "buttons": []}]
    started = _call(app, "POST", "/api/run", b"{}", content_type="application/json")
    assert started.status == 202
    again = _call(app, "POST", "/api/run", b"{}", content_type="application/json")
    assert again.status == 409
    assert json.loads(again.body)["code"] == "run-in-progress"
    run = manager.active()
    assert run is not None
    assert run.floor == 3
    chat.messages_list += _chat(run.id, first=4)
    shown = json.loads(_call(app, "GET", "/approver/api/chat").body)["messages"]
    assert [m["message_id"] for m in shown] == [4, 5, 6, 7, 8]  # the view starts at the run
    assert all(set(m) == {"message_id", "text", "buttons", "edited", "bot"} for m in shown)
    tap = json.dumps({"message_id": 8, "data": "g:1:ab"}).encode()
    assert (
        _call(app, "POST", "/approver/api/tap", tap, content_type="application/json").status == 200
    )
    assert chat.taps == [(8, "g:1:ab")]
    second = _call(app, "POST", "/approver/api/tap", tap, content_type="application/json")
    assert second.status == 409
    assert chat.taps == [(8, "g:1:ab")]
    bogus = json.dumps({"message_id": 3, "data": "g:1:ab"}).encode()
    assert (
        _call(app, "POST", "/approver/api/tap", bogus, content_type="application/json").status
        == 403
    )
    for bad in (b"[]", b'{"message_id": true, "data": "g"}', b'{"message_id": 8}', b"nope"):
        assert (
            _call(app, "POST", "/approver/api/tap", bad, content_type="application/json").status
            == 400
        )
    assert _call(app, "POST", "/api/reset", b"{}", content_type="application/json").status == 409
    manager.stop()


def test_tryit_state_never_carries_a_credential(tmp_path: Path) -> None:
    app, _chat, _ = _front(
        tmp_path, env={"TRYIT_LIVE": "1", "WANDB_API_KEY": SECRET, "REVIEWER_MODEL": "model-x"}
    )
    body = _call(app, "GET", "/api/state").body.decode()
    state = json.loads(body)
    assert SECRET not in body
    assert state["shared_note"].startswith("One shared demo tenant")
    assert "advisory" in state["reviewer"]["advisory_note"]
    assert state["record"]["log_verify"]["status"] == "clean"


def test_tryit_page_is_relative_and_escapes_the_base_path() -> None:
    page = render_page('/a/x"><script>')
    assert '/a/x"><script>' not in page
    assert "&quot;" in page
    for url in re.findall(r'(?:src|href|action)="([^"]*)"', render_page("")):
        assert not url.startswith(("/", "http")), url
    assert "fetch(new URL(path,base)" in PAGE_SCRIPT
    assert "document.cookie" not in PAGE_SCRIPT
    assert "window.open" not in PAGE_SCRIPT
    assert "top.location" not in PAGE_SCRIPT


def test_tryit_record_view_maps_the_run_and_drops_bad_trace_links(tmp_path: Path) -> None:
    store = StateStore(tmp_path, "http://127.0.0.1:8080")
    good = "https://wandb.ai/bountify/judgy/r/call/019a-call"
    store.settle(
        "hook:demo-trun1-push-main:aa:vcs.push.main",
        JudgedEntry(
            status="notified",
            seq=3,
            decision="NEEDS_HUMAN",
            trace_url=good,
            action_class="vcs.push.main",
        ),
    )
    store.settle(
        "hook:demo-trun1-push-branch:bb:vcs.push.branch",
        JudgedEntry(status="absent", seq=7, reason="timeout", action_class="vcs.push.branch"),
    )
    store.settle(
        "hook:demo-trun1-force-push:cc:vcs.history.rewrite",
        JudgedEntry(status="notified", seq=9, decision="READY", trace_url="javascript:alert(1)"),
    )
    store.record_decision(
        "hook:demo-trun1-push-main:aa:vcs.push.main",
        DecisionRecord(event="approval.rejected", actor="human:demo", seq=5, ts="t"),
    )
    store.save()
    record, per = record_view(tmp_path, "trun1", "live")
    assert per["push-main"]["verdict"] == "NEEDS_HUMAN"
    assert per["push-main"]["trace_url"] == good
    assert per["push-branch"] == {"state": "absent", "reason": "the reviewer timed out"}
    assert per["force-push"]["trace_url"] is None
    assert record["counters"]["escalated"] == 1
    assert record["recent"][0]["label"] == "escalated"
    assert record_view(tmp_path, "tother", "live")[1] == {}
    assert record_view(tmp_path / "missing", None, "offline")[0]["readable"] is False


def test_tryit_loopback_preload_exists_and_is_used() -> None:
    preload = (ROOT / "images" / "tryit" / "loopback.mjs").read_text()
    assert '"127.0.0.1"' in preload
    assert "net.Server.prototype.listen" in preload
    assert tryit_config.LOOPBACK_PRELOAD == "file:///opt/tryit/loopback.mjs"
    assert os.path.basename(tryit_config.LOOPBACK_PRELOAD) == "loopback.mjs"


def test_tryit_page_says_plainly_when_the_live_reviewer_is_paused(tmp_path: Path) -> None:
    live = {"TRYIT_LIVE": "1", "WANDB_API_KEY": SECRET, "REVIEWER_MODEL": "model-x"}
    app, _chat, _ = _front(tmp_path, reviewer=("live", None), env=live)
    label = json.loads(_call(app, "GET", "/api/state").body)["reviewer"]["label"]
    assert label.startswith("Live reviewer: W&B Inference (model-x)")
    app, _chat, _ = _front(tmp_path, reviewer=("offline", "daily-cap"), env=live)
    reviewer = json.loads(_call(app, "GET", "/api/state").body)["reviewer"]
    assert reviewer["mode"] == "offline"
    assert reviewer["fallback"] == "daily-cap"
    assert "cap of 80 live reviewer calls is used up" in reviewer["label"]
    app, _chat, _ = _front(tmp_path)
    offline = json.loads(_call(app, "GET", "/api/state").body)["reviewer"]
    assert offline["label"].startswith("Offline reviewer, hermetic demo")
    assert "budget" not in offline
