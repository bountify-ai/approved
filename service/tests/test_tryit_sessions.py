"""Private session authority, capacity, lifecycle, and durable spending invariants."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from approved.tryit.config import Settings
from approved.tryit.runs import LiveBudget
from approved.tryit.sessions import PrivateSessions


def _call(
    app: PrivateSessions,
    method: str,
    path: str,
    *,
    token: str = "",
    gate: str = "gateway-secret-0123456789-0123456789",
) -> tuple[int, dict[str, Any]]:
    headers = {"x-approved-gateway": gate, "x-approved-session": token}
    if method == "POST":
        headers.update({"content-type": "application/json", "content-length": "2"})
    response = app.handle(method, path, headers, lambda n: b"{}"[:n])
    return response.status, json.loads(response.body)


def test_policy_view_is_current_bounded_file_for_only_own_session(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        "approved.tryit.sessions.load_scenarios",
        lambda _path: [("push", "git push origin main", "manual")],
    )

    def provision(_self: PrivateSessions, visitor: Any) -> None:
        visitor.app = object()
        visitor.state = "ready"
        visitor.provision_done.set()

    monkeypatch.setattr(PrivateSessions, "_provision", provision)
    app = PrivateSessions(Settings(port=18789), tmp_path, "gateway-secret-0123456789-0123456789")
    _, first = _call(app, "POST", "/api/session")
    _, second = _call(app, "POST", "/api/session")
    one, two = first["session_token"], second["session_token"]
    paths = [
        tmp_path / "tryit" / "sessions" / app._visitors[token].id / "demo" / "APPROVAL.md"
        for token in (one, two)
    ]
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True)
        path.write_text(f"policy {index}", encoding="utf-8")
    assert _call(app, "GET", "/api/policy")[0] == 401
    assert _call(app, "GET", "/api/policy", token=one, gate="wrong")[0] == 403
    assert _call(app, "POST", "/api/policy", token=one)[0] == 405
    assert _call(app, "GET", "/api/policy?path=../other", token=one)[0] == 400
    code, own = _call(app, "GET", "/api/policy", token=one)
    assert code == 200
    assert own == {
        "path": str(paths[0]),
        "sha256": hashlib.sha256(b"policy 0").hexdigest(),
        "text": "policy 0",
    }
    assert _call(app, "GET", "/api/policy", token=two)[1]["text"] == "policy 1"
    headers = {
        "x-approved-gateway": "gateway-secret-0123456789-0123456789",
        "x-approved-session": one,
    }
    head = app.handle("HEAD", "/api/policy", headers, lambda _n: b"")
    assert head.status == 200
    assert head.body == b""
    paths[0].write_bytes(b"x" * (16 * 1024 + 1))
    assert _call(app, "GET", "/api/policy", token=one)[0] == 503
    paths[0].write_bytes(b"\xff")
    assert _call(app, "GET", "/api/policy", token=one)[0] == 503
    paths[0].unlink()
    paths[0].symlink_to(paths[1])
    assert _call(app, "GET", "/api/policy", token=one)[0] == 503
    paths[0].unlink()
    os.mkfifo(paths[0])
    assert _call(app, "GET", "/api/policy", token=one)[0] == 503
    paths[0].unlink()
    paths[0].write_text("policy 0", encoding="utf-8")
    paths[0].parent.rename(paths[0].parent.with_name("elsewhere"))
    paths[0].parent.symlink_to(paths[0].parent.with_name("elsewhere"), target_is_directory=True)
    assert _call(app, "GET", "/api/policy", token=one)[0] == 503
    app._visitors[two].expires_at = time.time() - 1
    assert _call(app, "GET", "/api/policy", token=two)[0] == 410
    app.shutdown()


def test_private_session_gateway_capability_capacity_and_expiry(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        "approved.tryit.sessions.load_scenarios",
        lambda _path: [("push", "git push origin main", "manual")],
    )

    def provision(_self: PrivateSessions, visitor: Any) -> None:
        visitor.state = "ready"
        visitor.provision_done.set()

    monkeypatch.setattr(PrivateSessions, "_provision", provision)
    app = PrivateSessions(Settings(port=18789), tmp_path, "gateway-secret-0123456789-0123456789")
    assert _call(app, "POST", "/api/session", gate="wrong")[0] == 403
    first_status, first = _call(app, "POST", "/api/session")
    second_status, second = _call(app, "POST", "/api/session")
    assert (first_status, second_status) == (202, 202)
    token1, token2 = first["session_token"], second["session_token"]
    assert token1 != token2
    assert len(token1) >= 32
    assert _call(app, "POST", "/api/session")[0] == 429
    assert _call(app, "GET", "/api/state", token="other")[0] == 401
    assert _call(app, "GET", "/approver/api/chat", token="other")[0] == 401
    assert _call(app, "POST", "/approver/api/tap", token="other")[0] == 401
    assert _call(app, "GET", "/api/state", token=token1)[1]["session"]["state"] == "ready"
    app._visitors[token1].expires_at = time.time() - 1
    assert _call(app, "GET", "/api/state", token=token1)[0] == 410
    assert app._visitors[token1].released.wait(2)
    third_status, third = _call(app, "POST", "/api/session")
    assert third_status == 202
    assert third["session_token"] not in {token1, token2}
    assert _call(app, "POST", "/api/run", token=token1)[0] == 410
    app.shutdown()


def test_state_telemetry_is_private_bounded_and_present_during_startup(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        "approved.tryit.sessions.load_scenarios",
        lambda _path: [("push", "git push origin main", "manual")],
    )

    def provision(_self: PrivateSessions, visitor: Any) -> None:
        visitor.provision_done.set()

    monkeypatch.setattr(PrivateSessions, "_provision", provision)
    app = PrivateSessions(Settings(port=18789), tmp_path, "gateway-secret-0123456789-0123456789")
    first = _call(app, "POST", "/api/session")[1]["session_token"]
    second = _call(app, "POST", "/api/session")[1]["session_token"]
    first_state = _call(app, "GET", "/api/state", token=first)[1]
    second_state = _call(app, "GET", "/api/state", token=second)[1]
    assert first_state["session"]["state"] == "starting"
    assert first_state["telemetry"]["version"] == 1
    assert first_state["telemetry"]["session"]["ref"] != second_state["telemetry"]["session"]["ref"]
    assert first_state["telemetry"]["runtime"]["topology"] == "one-host-isolated-processes"
    assert "provider" not in first_state["telemetry"]["runtime"]
    assert app._visitors[first].telemetry(maritime_deployment=True)["runtime"]["provider"] == (
        "Maritime deployment"
    )
    assert all(
        not item["running"] and not item["healthy"]
        for item in first_state["telemetry"]["runtime"]["processes"].values()
    )
    assert [event["kind"] for event in first_state["telemetry"]["events"]] == ["session_allocated"]
    visitor = app._visitors[first]
    for _ in range(30):
        visitor.emit("gate_unavailable")
    visitor.sup = SimpleNamespace(  # type: ignore[assignment]
        health=SimpleNamespace(
            parts=lambda: {"daemon": True, "fake_telegram": False, "judge": True}
        ),
        daemon=SimpleNamespace(alive=True, starts=2),
        fake_tg=SimpleNamespace(alive=False, starts=1),
        judge=SimpleNamespace(alive=True, starts=1),
        shutdown=lambda: None,
    )
    view = _call(app, "GET", "/api/state", token=first)[1]["telemetry"]
    assert len(view["events"]) == 20
    assert view["runtime"]["processes"]["approval_gate"] == {
        "running": True,
        "healthy": True,
        "starts": 2,
    }
    assert view["runtime"]["processes"]["approver_chat"]["healthy"] is False
    encoded = json.dumps(view)
    for secret in (
        first,
        second,
        visitor.id,
        "gateway-secret-0123456789-0123456789",
        str(tmp_path),
    ):
        assert secret not in encoded
    assert _call(app, "GET", "/api/state", token="other")[0] == 401
    assert second_state["telemetry"]["session"]["ref"] not in encoded
    app.shutdown()


def test_health_events_report_only_measured_transitions(monkeypatch: Any) -> None:
    from approved.tryit.supervisor import HealthProbe

    healthy = {"daemon": False, "fake_telegram": False, "judge": False}
    events: list[tuple[str, bool]] = []

    def probe(url: str) -> tuple[int, dict[str, str]]:
        if ":18000/" in url:
            return (
                200 if healthy["daemon"] else 503,
                {"facade": "listening", "webhook": "running"},
            )
        part = "fake_telegram" if ":18001/" in url else "judge"
        return (200 if healthy[part] else 503, {})

    monkeypatch.setattr("approved.tryit.supervisor.get_json", probe)
    probe_state = HealthProbe(
        settings=Settings(port=18789, daemon_port=18000, fake_tg_port=18001, judge_port=18002),
        on_change=lambda part, up: events.append((part, up)),
    )
    probe_state.check_once()
    assert events == []
    healthy.update({"daemon": True, "fake_telegram": True, "judge": True})
    probe_state.check_once()
    assert events == [("daemon", True), ("fake_telegram", True), ("judge", True)]
    healthy["judge"] = False
    probe_state.check_once()
    probe_state.check_once()
    assert events[-1] == ("judge", False)
    assert len(events) == 4


def test_live_budget_lifetime_survives_utc_rollover_and_restart(tmp_path: Path) -> None:
    now = [1780271999.0]
    path = tmp_path / "tryit" / "live-budget.json"
    budget = LiveBudget(path, daily_cap=80, per_hour=80, lifetime_cap=4, clock=lambda: now[0])
    assert budget.reserve(4) is None
    now[0] += 86402
    restarted = LiveBudget(path, daily_cap=80, per_hour=80, lifetime_cap=4, clock=lambda: now[0])
    assert restarted.reserve(1) == "lifetime-cap"
    assert restarted.snapshot()["used_lifetime"] == 4
    path.unlink()
    assert restarted.reserve(1) == "budget-unreadable"


def test_two_creation_races_allocate_distinct_slots(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "approved.tryit.sessions.load_scenarios",
        lambda _path: [("push", "git push origin main", "manual")],
    )
    hold = threading.Event()

    def provision(_self: PrivateSessions, visitor: Any) -> None:
        hold.wait(2)
        visitor.provision_done.set()

    monkeypatch.setattr(PrivateSessions, "_provision", provision)
    app = PrivateSessions(Settings(port=18789), tmp_path, "gateway-secret-0123456789-0123456789")
    results: list[tuple[int, dict[str, Any]]] = []
    lock = threading.Lock()

    def create() -> None:
        answer = _call(app, "POST", "/api/session")
        with lock:
            results.append(answer)

    threads = [threading.Thread(target=create) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    hold.set()
    assert sorted(status for status, _ in results) == [202, 202] + [429] * 6
    assert {v.slot for v in app._visitors.values()} == {0, 1}
    app.shutdown()


def test_shutdown_serializes_with_watch_restart() -> None:
    """An expiring slot cannot resurrect the daemon after its cleanup finishes."""
    from approved.tryit.supervisor import Supervisor

    entered = threading.Event()
    release = threading.Event()

    class Reaper:
        def reap(self) -> None:
            return

    class Child:
        alive = True

        def stop(self) -> None:
            self.alive = False

        def tick(self) -> bool:
            return False

        def restart(self) -> None:
            self.alive = True

    class FakeChild(Child):
        def tick(self) -> bool:
            entered.set()
            assert release.wait(2)
            return True

    sup = Supervisor.__new__(Supervisor)
    sup.reaper = Reaper()  # type: ignore[assignment]
    sup.booted = True
    sup._stopped = False
    sup._lifecycle_lock = threading.RLock()
    sup.fake_tg_generation = 0
    sup.fake_tg = FakeChild()  # type: ignore[assignment]
    sup.daemon = Child()  # type: ignore[assignment]
    sup.judge = Child()  # type: ignore[assignment]
    watcher = threading.Thread(target=sup.watch)
    watcher.start()
    assert entered.wait(2)
    stopper = threading.Thread(target=sup.shutdown)
    stopper.start()
    assert stopper.is_alive()  # shutdown waits for the in-flight restart pass
    release.set()
    watcher.join(2)
    stopper.join(2)
    assert not watcher.is_alive()
    assert not stopper.is_alive()
    assert not sup.daemon.alive
    sup.watch()
    assert not sup.daemon.alive


def test_daemon_environment_keeps_each_session_store_and_ports(tmp_path: Path) -> None:
    from approved.tryit.config import Layout

    first = Settings(
        port=18789, daemon_port=18000, fake_tg_port=18001, serve_port=18003, webhook_port=18004
    )
    second = Settings(
        port=18789, daemon_port=18100, fake_tg_port=18101, serve_port=18103, webhook_port=18104
    )
    first_layout = Layout(tmp_path / "sessions" / "one")
    second_layout = Layout(tmp_path / "sessions" / "two")
    env_one = first.daemon_env({}, first_layout)
    env_two = second.daemon_env({}, second_layout)
    assert env_one["APPROVAL_DATA_DIR"] == str(first_layout.data)
    assert env_two["APPROVAL_DATA_DIR"] == str(second_layout.data)
    assert env_one["APPROVAL_DATA_DIR"] != env_two["APPROVAL_DATA_DIR"]
    assert env_one["APPROVAL_STATE_DIR"] == str(first_layout.tryit / "runtime-state")
    assert env_two["APPROVAL_STATE_DIR"] == str(second_layout.tryit / "runtime-state")
    assert env_one["APPROVAL_STATE_DIR"] != env_two["APPROVAL_STATE_DIR"]
    assert {
        env_one["PORT"],
        env_one["APPROVAL_SERVE_INTERNAL_PORT"],
        env_one["APPROVAL_WEBHOOK_INTERNAL_PORT"],
    }.isdisjoint(
        {
            env_two["PORT"],
            env_two["APPROVAL_SERVE_INTERNAL_PORT"],
            env_two["APPROVAL_WEBHOOK_INTERNAL_PORT"],
        }
    )


def test_reusing_a_slot_keeps_bot_ownership_in_the_new_session(tmp_path: Path) -> None:
    """A new visitor gets its own core registry even on the same fake Bot API port."""
    from approved.tryit.config import Layout

    slot = Settings(port=18789, daemon_port=18000, fake_tg_port=18001)
    old = slot.daemon_env({}, Layout(tmp_path / "sessions" / "old"))
    new = slot.daemon_env({}, Layout(tmp_path / "sessions" / "new"))
    assert old["APPROVAL_IMAGE_TG_API_BASE"] == new["APPROVAL_IMAGE_TG_API_BASE"]
    assert old["APPROVAL_STATE_DIR"] != new["APPROVAL_STATE_DIR"]
    assert old["APPROVAL_STATE_DIR"].startswith(old["APPROVAL_DATA_DIR"] + "/")
    assert new["APPROVAL_STATE_DIR"].startswith(new["APPROVAL_DATA_DIR"] + "/")


def test_required_child_timeout_refuses_ready_state(tmp_path: Path, monkeypatch: Any) -> None:
    """An absent approver transport must not appear as a ready private demo."""
    from approved.tryit.config import Layout
    from approved.tryit.supervisor import Supervisor

    class Child:
        def __init__(self) -> None:
            self.starts = 0

        def start(self) -> None:
            self.starts += 1

    monkeypatch.setattr("approved.tryit.supervisor.generate", lambda _path: object())
    monkeypatch.setattr(Supervisor, "_init_store", lambda _self, _stop: True)
    for missing in ("fake_telegram", "daemon"):
        sup = Supervisor.__new__(Supervisor)
        sup.layout = Layout(tmp_path / missing)
        sup.layout.data.mkdir()
        sup.booted = False
        sup._on_event = None
        sup.boot_error = None
        sup.fake_tg_generation = 0
        sup.fake_tg = Child()  # type: ignore[assignment]
        sup.daemon = Child()  # type: ignore[assignment]
        sup.judge = Child()  # type: ignore[assignment]
        monkeypatch.setattr(
            sup, "_await", lambda part, _stop, _timeout, unavailable=missing: part != unavailable
        )
        sup.boot(threading.Event(), judge_live=True)
        assert not sup.booted
        assert sup.boot_error == f"{missing.replace('_', '-')}-unavailable"
        assert sup.judge.starts == 0
