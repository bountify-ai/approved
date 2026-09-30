"""S1: a worker that ends on its own degrades /health and shuts the server down."""

from __future__ import annotations

from typing import Any

import pytest

from approved.supervise import WorkerRunner


class Recorder:
    def __init__(self) -> None:
        self.status: dict[str, Any] = {}
        self.shutdowns = 0
        self.waited: list[float] = []

    def update(self, **fields: Any) -> None:
        self.status.update(fields)

    def shutdown(self) -> None:
        self.shutdowns += 1

    def wait(self, seconds: float) -> None:
        self.waited.append(seconds)


def _run(target, *, stop_first: bool = False) -> tuple[WorkerRunner, Recorder]:
    rec = Recorder()
    runner = WorkerRunner(target, update_status=rec.update, shutdown=rec.shutdown, wait=rec.wait)
    if stop_first:
        runner.request_stop()
    runner.start()
    runner.join(5)
    return runner, rec


def test_chain_break_degrades_and_shuts_down() -> None:
    runner, rec = _run(lambda: 3)
    assert rec.status == {"degraded": True, "degraded_reason": "chain-break", "running": False}
    assert rec.shutdowns == 1
    assert rec.waited == [3.0]
    assert runner.exit_code == 3


def test_a_crashed_worker_degrades_with_a_nonzero_exit() -> None:
    def boom() -> int:
        raise RuntimeError("unexpected")

    runner, rec = _run(boom)
    assert rec.status["degraded_reason"] == "worker-crashed:RuntimeError"
    assert runner.exit_code == 1
    assert rec.shutdowns == 1


@pytest.mark.parametrize("code", [0, 1])
def test_an_unrequested_exit_is_never_a_clean_exit(code: int) -> None:
    runner, rec = _run(lambda: code)
    assert rec.status["degraded"] is True
    assert runner.exit_code == 1 if code == 0 else code


def test_a_requested_stop_is_not_degraded() -> None:
    runner, rec = _run(lambda: 0, stop_first=True)
    assert rec.status == {}
    assert rec.shutdowns == 0
    assert runner.exit_code == 0


def test_health_answers_503_when_degraded(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from approved.config import load_settings
    from approved.console.app import context_from_settings, create_app

    status = {
        "running": False,
        "chain": "chain-break",
        "degraded": True,
        "degraded_reason": "chain-break",
    }
    settings = load_settings(
        {
            "FACADE_URL": "https://f.test",
            "TENANT_TOKEN": "t" * 24,
            "OFFLINE": "1",
            "STATE_DIR": str(tmp_path),
            "APPROVED_DEMO": "1",
        }
    )
    with TestClient(create_app(context_from_settings(settings, lambda: status))) as c:
        answer = c.get("/health")
    assert answer.status_code == 503
    assert answer.json()["status"] == "degraded"
    assert answer.json()["reason"] == "chain-break"


# ------------------------------------------------------------------ serve's shutdown wait


def _serve_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, **extra: str) -> None:
    for name in ("CONSOLE_TOKEN_FILE", "TENANT_TOKEN_FILE", "JUDGE_TG_BOT_TOKEN", "TG_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    env = {
        "FACADE_URL": "https://facade.test",
        "TENANT_TOKEN": "t" * 24,
        "OFFLINE": "1",
        "STATE_DIR": str(tmp_path),
        "CONSOLE_TOKEN": "c" * 40,
        **extra,
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_the_worker_join_outlasts_an_inflight_judgement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """RESILIENCE.md, SIGTERM: the in-flight judgement finishes. serve must wait at least the
    reviewer deadline plus the advisory's send, whatever the two are configured to."""
    import uvicorn

    from approved.__main__ import SHUTDOWN_MARGIN_S, main, worker_join_timeout
    from approved.config import load_settings

    defaults = load_settings(
        {"FACADE_URL": "https://facade.test", "TENANT_TOKEN": "t" * 24, "OFFLINE": "1"}
    )
    assert defaults.judge_timeout_s == 60
    assert defaults.http_timeout_s == 20
    assert worker_join_timeout(defaults) == 90

    joined: list[float | None] = []
    monkeypatch.setattr(WorkerRunner, "start", lambda self: None)
    monkeypatch.setattr(WorkerRunner, "join", lambda self, timeout=None: joined.append(timeout))
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)
    for judge, http in (("60", "20"), ("120", "30"), ("5", "2")):
        _serve_env(monkeypatch, tmp_path, JUDGE_TIMEOUT_S=judge, HTTP_TIMEOUT_S=http)
        assert main(["serve"]) == 0
        waited = joined[-1]
        assert waited is not None
        assert waited == float(judge) + float(http) + SHUTDOWN_MARGIN_S
        assert waited > float(judge) + float(http)
