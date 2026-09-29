"""Judge bounds: timeout, circuit breaker, failure-to-absence, idempotency, trace URL."""

from __future__ import annotations

import pytest

from approved import logs
from approved.judge import CircuitBreaker, Judge
from approved.reviewer import (
    Decision,
    InferenceError,
    JudgeRequest,
    Reviewer,
    ReviewerParseError,
    Verdict,
)

from .fakes import BlockingReviewer, ScriptedReviewer, verdict


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _req(key: str = "k", seq: int = 1) -> JudgeRequest:
    return JudgeRequest(action_key=key, action_class="vcs.push.main", seq=seq)


class FakeTracer:
    def run(self, reviewer: Reviewer, request: JudgeRequest) -> tuple[Verdict, str | None]:
        return reviewer.review(request), "0199-call-id"


def test_timeout_is_absence_and_does_not_raise() -> None:
    reviewer = BlockingReviewer()
    judge = Judge(reviewer, timeout_s=0.05)
    try:
        outcome = judge.evaluate(_req())
    finally:
        reviewer.release.set()
    assert outcome.verdict is None
    assert outcome.absent_reason == "timeout"
    assert logs.METRICS.get("judge.absent.timeout") == 1


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ReviewerParseError("bad"), "parse"),
        (InferenceError("down"), "inference"),
        (ValueError("bug"), "error"),
    ],
)
def test_reviewer_failures_become_absence(error: Exception, reason: str) -> None:
    outcome = Judge(ScriptedReviewer([error]), timeout_s=1).evaluate(_req())
    assert outcome.verdict is None
    assert outcome.absent_reason == reason
    assert logs.METRICS.get(f"judge.absent.{reason}") == 1


def test_breaker_opens_after_threshold_and_skips_the_reviewer() -> None:
    clock = Clock()
    reviewer = ScriptedReviewer([InferenceError("down")])
    judge = Judge(reviewer, timeout_s=1, breaker=CircuitBreaker(3, 30, clock=clock))
    reasons = [judge.evaluate(_req(f"k{i}")).absent_reason for i in range(5)]
    assert reasons == ["inference"] * 3 + ["circuit-open"] * 2
    assert len(reviewer.seen) == 3
    assert judge.breaker.state == "open"


def test_breaker_half_opens_after_cooldown_and_recovers() -> None:
    clock = Clock()
    reviewer = ScriptedReviewer([InferenceError("a"), InferenceError("b"), verdict()])
    judge = Judge(reviewer, timeout_s=1, breaker=CircuitBreaker(2, 30, clock=clock))
    judge.evaluate(_req("a"))
    judge.evaluate(_req("b"))
    assert judge.evaluate(_req("c")).absent_reason == "circuit-open"
    clock.now += 30
    assert judge.breaker.state == "half-open"
    outcome = judge.evaluate(_req("d"))
    assert outcome.verdict is not None
    assert judge.breaker.state == "closed"


def test_half_open_failure_reopens_immediately() -> None:
    clock = Clock()
    reviewer = ScriptedReviewer([InferenceError("down")])
    breaker = CircuitBreaker(3, 30, clock=clock)
    judge = Judge(reviewer, timeout_s=1, breaker=breaker)
    for i in range(3):
        judge.evaluate(_req(f"k{i}"))
    clock.now += 31
    assert judge.evaluate(_req("trial")).absent_reason == "inference"
    assert breaker.state == "open"
    assert judge.evaluate(_req("next")).absent_reason == "circuit-open"


def test_half_open_admits_one_trial_only() -> None:
    clock = Clock()
    breaker = CircuitBreaker(1, 10, clock=clock)
    breaker.record_failure()
    clock.now += 10
    assert breaker.allow() is True
    assert breaker.allow() is False


def test_timeouts_count_toward_the_breaker() -> None:
    reviewer = BlockingReviewer()
    judge = Judge(reviewer, timeout_s=0.02, breaker=CircuitBreaker(2, 60))
    try:
        judge.evaluate(_req("a"))
        judge.evaluate(_req("b"))
        assert judge.evaluate(_req("c")).absent_reason == "circuit-open"
    finally:
        reviewer.release.set()
    assert reviewer.calls == 2


def test_trace_url_comes_from_the_call_id() -> None:
    judge = Judge(
        ScriptedReviewer([verdict(Decision.READY)]),
        timeout_s=1,
        tracer=FakeTracer(),
        trace_url=lambda cid: f"https://wandb.ai/bountify/judgy/r/call/{cid}" if cid else None,
    )
    outcome = judge.evaluate(_req())
    assert outcome.call_id == "0199-call-id"
    assert outcome.trace_url == "https://wandb.ai/bountify/judgy/r/call/0199-call-id"


def test_no_trace_url_without_a_call() -> None:
    outcome = Judge(ScriptedReviewer([verdict()]), timeout_s=1).evaluate(_req())
    assert outcome.call_id is None
    assert outcome.trace_url is None


class MemoryLedger:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def claim(self, action_key: str, seq: int) -> bool:
        if action_key in self.keys:
            return False
        self.keys.add(action_key)
        return True


def test_judge_once_per_action_key() -> None:
    reviewer = ScriptedReviewer([verdict()])
    judge, ledger = Judge(reviewer, timeout_s=1), MemoryLedger()
    assert judge.judge_once(_req("k"), ledger) is not None
    assert judge.judge_once(_req("k"), ledger) is None
    assert len(reviewer.seen) == 1


def test_weave_init_is_a_noop_offline_or_without_a_key() -> None:
    from approved.config import load_settings
    from approved.judge import init_weave

    base = {"FACADE_URL": "https://f.test", "TENANT_TOKEN": "t" * 24}
    assert init_weave(load_settings({**base, "OFFLINE": "1", "WANDB_API_KEY": "x"})) is None
    live_no_key = {**base, "REVIEWER_MODEL": "m", "INFERENCE_API_KEY": "k"}
    assert init_weave(load_settings(live_no_key)) is None
