"""Actual-call inference budget: durability and request-boundary behavior."""

from __future__ import annotations

import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from approved.config import ConfigError, load_inference_settings
from approved.inference_budget import BudgetError, InferenceCallBudget, initialize_budget
from approved.judge import Judge
from approved.reviewer import (
    InferenceBudgetExhausted,
    InferenceError,
    JudgeRequest,
    LiveReviewer,
)

from .fakes import ScriptedReviewer


def _reserve_in_process(path: str, results: Any) -> None:
    try:
        InferenceCallBudget(Path(path), 5, hourly_limit=5).reserve()
        results.put(True)
    except BudgetError:
        results.put(False)


def _reserve_then_crash(path: str) -> None:
    InferenceCallBudget(Path(path), 2, hourly_limit=2).reserve()
    os._exit(7)


class Completions:
    def __init__(self, replies: list[tuple[str, str] | Exception]) -> None:
        self.replies = replies
        self.calls = 0

    def create(self, **_kwargs: object) -> object:
        self.calls += 1
        reply = self.replies[min(self.calls - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        content, finish = reply
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)
            ],
            model="test-model",
        )


def _reviewer(
    path: Path, replies: list[tuple[str, str] | Exception], *, limit: int = 2
) -> tuple[LiveReviewer, Completions]:
    initialize_budget(path, limit, hourly_limit=limit)
    completions = Completions(replies)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    reviewer = LiveReviewer(
        model="test-model",
        base_url="https://api.inference.wandb.ai/v1",
        api_key="test",
        timeout_s=1,
        client=client,
        call_budget=InferenceCallBudget(path, limit, hourly_limit=limit),
    )
    return reviewer, completions


def _request() -> JudgeRequest:
    return JudgeRequest(action_key="a", action_class="vcs.push.branch", seq=1)


def _used(path: Path) -> int:
    return json.loads(path.read_text())["used"]


def test_missing_corrupt_and_reinitialized_ledger_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "calls.json"
    budget = InferenceCallBudget(path, 2, hourly_limit=2)
    with pytest.raises(BudgetError, match="unavailable"):
        budget.reserve()
    initialize_budget(path, 2, hourly_limit=2)
    budget.reserve()
    initialize_budget(path, 2, hourly_limit=2)
    assert _used(path) == 1
    path.unlink()
    with pytest.raises(BudgetError, match="unavailable"):
        initialize_budget(path, 2, hourly_limit=2)
    with pytest.raises(BudgetError, match="unavailable"):
        budget.reserve()
    path.write_text("not json")
    with pytest.raises(BudgetError, match="unavailable"):
        budget.reserve()


def test_hourly_cap_does_not_reset_lifetime(tmp_path: Path) -> None:
    path = tmp_path / "calls.json"
    initialize_budget(path, 3, hourly_limit=1)
    budget = InferenceCallBudget(path, 3, hourly_limit=1)
    budget.reserve()
    with pytest.raises(BudgetError, match="exhausted"):
        budget.reserve()
    assert _used(path) == 1


def test_crash_after_reservation_does_not_refund(tmp_path: Path) -> None:
    path = tmp_path / "calls.json"
    initialize_budget(path, 2, hourly_limit=2)
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_reserve_then_crash, args=(str(path),))
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 7
    assert _used(path) == 1


def test_thread_and_process_writers_share_one_limit(tmp_path: Path) -> None:
    path = tmp_path / "calls.json"
    initialize_budget(path, 5, hourly_limit=5)

    def attempt() -> bool:
        try:
            InferenceCallBudget(path, 5, hourly_limit=5).reserve()
            return True
        except BudgetError:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        thread_results = list(pool.map(lambda _i: attempt(), range(3)))
    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    processes = [
        ctx.Process(target=_reserve_in_process, args=(str(path), results)) for _ in range(10)
    ]
    for process in processes:
        process.start()
    process_results = [results.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sum(thread_results) + sum(process_results) == 5
    assert _used(path) == 5


def test_failed_transport_is_charged_before_request(tmp_path: Path) -> None:
    path = tmp_path / "calls.json"
    reviewer, completions = _reviewer(path, [RuntimeError("network failed")], limit=1)
    with pytest.raises(InferenceError, match="inference call failed"):
        reviewer.review(_request())
    assert completions.calls == 1
    assert _used(path) == 1
    with pytest.raises(InferenceBudgetExhausted):
        reviewer.review(_request())
    assert completions.calls == 1


def test_budget_exhaustion_is_safe_absence_without_breaker_masking() -> None:
    judge = Judge(
        ScriptedReviewer([InferenceBudgetExhausted("inference budget exhausted")]), timeout_s=1
    )
    reasons = [judge.evaluate(_request()).absent_reason for _ in range(4)]
    assert reasons == ["budget-exhausted"] * 4


def test_truncation_retry_reserves_second_actual_call(tmp_path: Path) -> None:
    path = tmp_path / "calls.json"
    reviewer, completions = _reviewer(path, [("", "length"), ("", "stop")], limit=2)
    with pytest.raises(InferenceError):
        reviewer.review(_request())
    assert completions.calls == 2
    assert _used(path) == 2


def test_input_and_output_guards_precede_reservation(tmp_path: Path) -> None:
    path = tmp_path / "calls.json"
    reviewer, completions = _reviewer(path, [("", "stop")])
    reviewer._system = "x" * 16_001
    with pytest.raises(InferenceError, match="input too large"):
        reviewer.review(_request())
    assert _used(path) == 0
    assert completions.calls == 0
    reviewer._system = "short"
    reviewer.max_tokens = 16_001
    with pytest.raises(InferenceError, match="output limit"):
        reviewer.review(_request())
    assert _used(path) == 0
    assert completions.calls == 0


def test_budget_config_is_opt_in_and_requires_all_fields() -> None:
    base = {"OFFLINE": "1"}
    assert load_inference_settings(base).inference_call_budget_file is None
    with pytest.raises(ConfigError, match="must be set together"):
        load_inference_settings({**base, "INFERENCE_CALL_BUDGET_FILE": "/data/calls.json"})
    configured = load_inference_settings(
        {
            **base,
            "INFERENCE_CALL_BUDGET_FILE": "/data/calls.json",
            "INFERENCE_CALL_BUDGET_LIMIT": "80",
            "INFERENCE_CALL_BUDGET_HOURLY_LIMIT": "20",
        }
    )
    assert configured.inference_call_budget_limit == 80
    assert configured.inference_call_budget_hourly_limit == 20


def test_read_only_availability_refuses_spent_or_corrupt_ledger(tmp_path: Path) -> None:
    path = tmp_path / "calls.json"
    initialize_budget(path, 4, hourly_limit=4)
    budget = InferenceCallBudget(path, 4, hourly_limit=4)
    assert budget.check(4) is None
    budget.reserve()
    assert budget.check(4) == "inference-budget-exhausted"
    assert budget.check(3) is None
    assert _used(path) == 1  # checking never charges
    path.write_text("corrupt", encoding="utf-8")
    assert budget.check() == "inference-budget-unavailable"
