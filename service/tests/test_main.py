"""Entry point exit codes, wiring, and the Weave op path without a Weave client."""

from __future__ import annotations

from pathlib import Path

import pytest

from approved.__main__ import EXIT_CONFIG, EXIT_STATE, build_worker, main
from approved.config import AGENT_CREDENTIAL_ENV_NAMES, load_settings
from approved.judge import WeaveTracer
from approved.notify import NullNotifier, TelegramNotifier
from approved.reviewer import InferenceError, JudgeRequest, OfflineReviewer

BASE = {"FACADE_URL": "https://facade.test/a/t", "TENANT_TOKEN": "t" * 24, "OFFLINE": "1"}


def _set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name in (*BASE, "STATE_DIR", "TG_BOT_TOKEN", *AGENT_CREDENTIAL_ENV_NAMES):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_config_error_exits_2(monkeypatch: pytest.MonkeyPatch, logs_captured) -> None:
    _set_env(monkeypatch, {"OFFLINE": "1"})
    assert main(["worker", "--once"]) == EXIT_CONFIG
    assert "FACADE_URL" in logs_captured.records("config.error")[0]["detail"]


def test_untrustworthy_state_exits_4(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "judge-state.json").write_text("garbage")
    _set_env(monkeypatch, {**BASE, "STATE_DIR": str(tmp_path)})
    assert main(["worker", "--once"]) == EXIT_STATE


def test_offline_wiring(tmp_path: Path) -> None:
    worker = build_worker(load_settings({**BASE, "STATE_DIR": str(tmp_path)}))
    assert isinstance(worker.judge.reviewer, OfflineReviewer)
    assert isinstance(worker.notifier, NullNotifier)
    with_tg = build_worker(
        load_settings(
            {**BASE, "STATE_DIR": str(tmp_path), "TG_BOT_TOKEN": "1:x", "TG_CHAT_ID": "9"}
        )
    )
    assert isinstance(with_tg.notifier, TelegramNotifier)


def test_weave_tracer_without_a_client_runs_and_reraises() -> None:
    """Uninitialised Weave: the op still runs the reviewer, returns no call id, and a
    reviewer exception still reaches the judge (so it becomes absence, never a verdict)."""
    tracer = WeaveTracer()
    request = JudgeRequest(action_key="k", action_class="fs.read", seq=1)
    verdict, call_id = tracer.run(OfflineReviewer(), request)
    assert verdict.decision.value == "READY"
    assert call_id is None

    class Failing:
        name = "failing"

        def review(self, request: JudgeRequest):
            raise InferenceError("down")

    with pytest.raises(InferenceError):
        tracer.run(Failing(), request)
