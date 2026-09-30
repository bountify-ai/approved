"""Private session authority, capacity, lifecycle, and durable spending invariants."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
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
