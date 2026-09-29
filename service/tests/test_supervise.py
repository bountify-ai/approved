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
