"""The worker loop end to end against the fake facade and fake Telegram.

Every test here runs behind the ``facade`` fixture's guard: a write route or the agent
credential fails the test at teardown (invariants 1 and 2).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from approved import logs
from approved.judge import CircuitBreaker
from approved.reviewer import Decision, InferenceError
from approved.state import JudgedEntry
from approved.worker import EXIT_CHAIN_BREAK, EXIT_FOLLOW_ERROR, EXIT_OK, policy_rule_for

from .conftest import NOW
from .fakes import (
    BlockingReviewer,
    CallIdTracer,
    FakeFacade,
    ReplayFacade,
    ScriptedReviewer,
    decision_event,
    load_fixture,
    request_event,
    task_event,
    verdict,
)


def test_judges_a_request_and_sends_one_advisory(make_worker, facade, telegram) -> None:
    facade.append(task_event("k1", "ship the fix"), request_event("k1"))
    reviewer = ScriptedReviewer([verdict(why="Push to main publishes the change.")])
    worker = make_worker(reviewer)
    assert worker.run(once=True) == EXIT_OK
    assert telegram.texts == [
        "Judge (advisory, AI): NEEDS_HUMAN, Push to main publishes the change. "
        "Trace: not traced (offline)\nRequest class: vcs.push.main"
    ]
    [seen] = reviewer.seen
    assert seen.action_class == "vcs.push.main"
    assert seen.summary == "git push origin main"
    assert seen.task_summary == "ship the fix"
    assert worker.store.state.judged["k1"].status == "notified"
    assert worker.store.state.cursor.seq == 2


def test_each_action_key_is_judged_once(make_worker, facade, telegram) -> None:
    facade.append(request_event("k1"))
    reviewer = ScriptedReviewer([verdict()])
    worker = make_worker(reviewer)
    worker.run(once=True)
    # The same key again later in the log (a re-ask) is not judged a second time.
    facade.append(request_event("k1"), request_event("k2"))
    worker.run(once=True)
    assert [r.action_key for r in reviewer.seen] == ["k1", "k2"]
    assert len(telegram.texts) == 2


def test_restart_resumes_from_persisted_cursor(make_worker, facade, telegram, tmp_path) -> None:
    facade.append(request_event("k1"), request_event("k2"))
    first = make_worker(ScriptedReviewer([verdict()]))
    first.run(once=True)
    head = first.store.state.cursor
    assert head.seq == 2

    facade.append(request_event("k3"))
    reviewer = ScriptedReviewer([verdict()])
    second = make_worker(reviewer)  # new process: state reloaded from disk
    assert second.store.state.cursor == head
    requests_before = len(facade.requests)
    second.run(once=True)
    resumed = facade.requests[requests_before]
    assert resumed.url.params["from"] == "2"
    assert resumed.url.params["cursor_hash"] == head.hash
    assert [r.action_key for r in reviewer.seen] == ["k3"]
    assert len(telegram.texts) == 3


def test_crash_after_claim_never_rejudges(make_worker, facade, telegram) -> None:
    """A process killed mid-review leaves the key claimed; the restart stays silent."""
    facade.append(request_event("k1"))

    class Crash(BaseException):
        pass

    crashing = make_worker(ScriptedReviewer([verdict()]))

    def claim_then_die(request, ledger):
        ledger.claim(request.action_key, request.seq)
        raise Crash

    crashing.judge.judge_once = claim_then_die  # type: ignore[method-assign]
    with pytest.raises(Crash):
        crashing.run(once=True)
    assert crashing.store.state.cursor.seq == 0  # never advanced past the unfinished record

    reviewer = ScriptedReviewer([verdict()])
    make_worker(reviewer).run(once=True)
    assert reviewer.seen == []
    assert telegram.texts == []


def test_chain_break_stops_judging_and_is_surfaced(
    make_worker, facade, telegram, logs_captured
) -> None:
    facade.append(request_event("k1"))
    reviewer = ScriptedReviewer([verdict()])
    worker = make_worker(reviewer)
    worker.run(once=True)
    # Attack input: the log's retained prefix is replaced under the judge.
    facade.records[0]["hash"] = "e" * 64
    facade.append(request_event("k2"))
    assert worker.run() == EXIT_CHAIN_BREAK
    assert [r.action_key for r in reviewer.seen] == ["k1"]
    brk = worker.store.state.chain_break
    assert brk is not None
    assert brk.reason == "facade-integrity:cursor-mismatch"
    assert worker.store.state.cursor.seq == 1  # kept, never skipped
    assert len(telegram.texts) == 2
    assert "chain-break at seq 1" in telegram.texts[1]
    assert logs_captured.records("chain-break")[0]["level"] == "error"
    assert logs.METRICS.get("follow.chain_break") == 1

    # A restart refuses to follow at all while the break is recorded.
    seen = len(facade.requests)
    assert make_worker(reviewer).run() == EXIT_CHAIN_BREAK
    assert len(facade.requests) == seen
    assert len(telegram.texts) == 2


def test_client_side_link_mismatch_is_a_chain_break(make_worker, telegram) -> None:
    body = load_fixture("follow-genesis.json")["body"]
    body["records"][2]["prev"] = "0" * 64  # attack input, in memory only
    worker = make_worker(facade_override=ReplayFacade(body=body))
    assert worker.run() == EXIT_CHAIN_BREAK
    assert worker.store.state.chain_break is not None
    assert worker.store.state.chain_break.reason == "prev-mismatch"
    assert worker.store.state.cursor.seq == 0
    assert all("Judge (advisory, AI): halted" in t for t in telegram.texts)


def test_reviewer_timeout_sends_nothing_and_the_loop_continues(
    make_worker, facade, telegram
) -> None:
    facade.append(request_event("slow"), request_event("fast"))
    blocking = BlockingReviewer()
    scripted = ScriptedReviewer([verdict(Decision.READY)])

    class Router:
        name = "router"

        def review(self, request):
            return (blocking if request.action_key == "slow" else scripted).review(request)

    worker = make_worker(Router(), timeout_s=0.05)
    try:
        assert worker.run(once=True) == EXIT_OK
    finally:
        blocking.release.set()
    assert worker.store.state.judged["slow"].status == "absent"
    assert worker.store.state.judged["slow"].reason == "timeout"
    assert len(telegram.texts) == 1
    assert "READY" in telegram.texts[0]
    assert logs.METRICS.get("judge.absent.timeout") == 1


def test_open_circuit_sends_nothing_and_recovers(make_worker, facade, telegram) -> None:
    now = [1000.0]
    breaker = CircuitBreaker(2, 30, clock=lambda: now[0])
    reviewer = ScriptedReviewer([InferenceError("a"), InferenceError("b"), verdict()])
    facade.append(*(request_event(f"k{i}") for i in range(3)))
    worker = make_worker(reviewer, breaker=breaker)
    worker.run(once=True)
    assert [worker.store.state.judged[f"k{i}"].reason for i in range(3)] == [
        "inference",
        "inference",
        "circuit-open",
    ]
    assert telegram.texts == []
    now[0] += 30
    facade.append(request_event("k3"))
    worker.run(once=True)
    assert worker.store.state.judged["k3"].status == "notified"
    assert len(telegram.texts) == 1


def test_telegram_failure_is_recorded_silent_and_loop_continues(
    make_worker, facade, telegram
) -> None:
    telegram.fail_with = 500
    facade.append(request_event("k1"), request_event("k2"))
    worker = make_worker(ScriptedReviewer([verdict()]))
    assert worker.run(once=True) == EXIT_OK
    assert {k: e.status for k, e in worker.store.state.judged.items()} == {
        "k1": "silent",
        "k2": "silent",
    }


def test_already_decided_and_stale_requests_are_skipped(make_worker, facade, telegram) -> None:
    facade.append(
        request_event("decided"),
        decision_event("decided", "approval.rejected"),
        request_event("old", ts="2026-09-29T10:00:00.000Z"),
    )
    reviewer = ScriptedReviewer([verdict()])
    worker = make_worker(reviewer)
    worker.run(once=True)
    assert reviewer.seen == []
    assert worker.store.state.judged["decided"].reason == "already-decided"
    assert worker.store.state.judged["old"].reason == "stale"


def test_decisions_are_recorded_for_feedback_and_hook_errors_are_contained(
    make_worker, facade, telegram
) -> None:
    calls: list[tuple[str, str, str | None]] = []

    def hook(key, decision, judged: JudgedEntry | None) -> None:
        calls.append((key, decision.event, judged.status if judged else None))
        raise RuntimeError("feedback backend down")

    facade.append(request_event("k1"))
    worker = make_worker(ScriptedReviewer([verdict()]), on_decision=hook, tracer=CallIdTracer())
    worker.run(once=True)
    for event in ("approval.granted", "approval.expired"):
        facade.append(decision_event("k1", event))
    facade.append(decision_event("other", "approval.withdrawn", actor="agent:x"))
    assert worker.run(once=True) == EXIT_OK
    assert calls == [
        ("k1", "approval.granted", "notified"),
        ("other", "approval.withdrawn", None),
    ]
    assert worker.store.state.decisions["k1"].feedback == "pending"
    assert worker.store.state.decisions["other"].feedback == "not-applicable"
    assert logs.METRICS.get("decision.hook_failed") == 2


def test_real_capture_replays_through_the_worker(make_worker, telegram) -> None:
    """Records the runtime itself wrote: one open request, judged by the offline rules."""
    body = load_fixture("follow-genesis.json")["body"]
    captured_at = 1_790_324_787.8  # 2026-09-25T08:26:27.8Z, just after the request
    worker = make_worker(facade_override=ReplayFacade(body=body), clock=lambda: captured_at)
    assert worker.run(once=True) == EXIT_OK
    [text] = telegram.texts
    assert text.startswith("Judge (advisory, AI): NEEDS_HUMAN, Push to the default branch")
    key = body["records"][2]["action_key"]
    assert worker.store.state.judged[key].decision == "NEEDS_HUMAN"


def test_real_capture_with_expiries_judges_nothing(make_worker, telegram) -> None:
    body = load_fixture("follow-genesis-latest.json")["body"]
    worker = make_worker(facade_override=ReplayFacade(body=body))
    worker.run(once=True)
    assert telegram.texts == []
    assert len(worker.store.state.decisions) == 6
    assert {e.reason for e in worker.store.state.judged.values()} == {"already-decided"}
    assert worker.store.state.cursor.seq == 19


def test_transport_errors_back_off_without_moving_the_cursor(make_worker, facade) -> None:
    facade.append(request_event("k1"))
    facade.script = [httpx.ConnectError("down"), httpx.Response(503)]
    worker = make_worker(ScriptedReviewer([verdict()]))
    assert worker.run(once=True) == EXIT_FOLLOW_ERROR
    assert worker.store.state.cursor.seq == 0
    worker.run(once=True)  # 503
    assert worker.store.state.cursor.seq == 0
    assert worker.run(once=True) == EXIT_OK
    assert worker.store.state.cursor.seq == 1


def test_graceful_stop_finishes_the_inflight_judgement(make_worker, facade, telegram) -> None:
    facade.append(request_event("k1"), request_event("k2"))
    holder: dict = {}

    def stop_then_verdict(request):
        holder["worker"].stop()  # SIGTERM arrives while the reviewer is running
        return verdict()

    worker = make_worker(ScriptedReviewer([stop_then_verdict]))
    holder["worker"] = worker
    assert worker.run() == EXIT_OK
    assert list(worker.store.state.judged) == ["k1"]
    assert worker.store.state.cursor.seq == 1
    assert len(telegram.texts) == 1


def test_policy_rule_excerpt(tmp_path: Path) -> None:
    policy = tmp_path / "APPROVAL.md"
    policy.write_text("# Policy\n\n## Classes\n- `vcs.push.main`: manual, 1 approver\n- other\n")
    assert "vcs.push.main" in (policy_rule_for(policy, "vcs.push.main") or "")
    assert policy_rule_for(policy, "payments.send") is None
    assert policy_rule_for(tmp_path / "missing.md", "vcs.push.main") is None
    assert policy_rule_for(None, "vcs.push.main") is None


def test_unauthenticated_facade_is_a_credential_error_not_a_crash(
    make_worker, telegram, logs_captured
) -> None:
    wrong = FakeFacade(tenant_token="a-different-tenant-credential")
    wrong.append(request_event("k1"))
    worker = make_worker(facade_override=wrong)
    assert worker.run(once=True) == EXIT_FOLLOW_ERROR
    assert logs_captured.records("follow.credential-refused")[0]["status"] == 401
    assert wrong.violations == []


def test_now_constant_is_thirty_seconds_after_synthetic_requests() -> None:
    from datetime import UTC, datetime

    assert datetime.fromtimestamp(NOW, UTC).isoformat() == "2026-09-29T12:00:30+00:00"


def test_task_context_survives_restart_and_is_pruned_on_close(
    make_worker, facade, telegram
) -> None:
    facade.append(task_event("k1", "ship the checkout fix"))
    make_worker(ScriptedReviewer([verdict()])).run(once=True)  # registration only, then exit

    reviewer = ScriptedReviewer([verdict()])
    restarted = make_worker(reviewer)  # new process
    assert restarted.store.task_summary("k1") == "ship the checkout fix"
    facade.append(request_event("k1"))
    restarted.run(once=True)
    assert reviewer.seen[0].task_summary == "ship the checkout fix"

    facade.append(decision_event("k1"))
    restarted.run(once=True)
    assert restarted.store.state.task_context == {}


def test_follow_failures_retry_quickly_before_backing_off(make_worker, facade) -> None:
    """The facade is briefly unavailable while a hook call holds it; the judge keeps asking at
    the poll interval for a few failures before its backoff grows."""
    facade.script = [httpx.Response(503) for _ in range(8)]
    worker = make_worker(ScriptedReviewer([verdict()]))
    delays: list[float] = []

    def record(delay: float | None = None) -> bool:
        delays.append(delay or 0.0)
        if len(delays) >= 8:
            worker.stop()
        return worker.stopping

    worker._stop.wait = record  # type: ignore[method-assign]
    worker.run()
    poll = worker.poll_interval_s
    assert delays[:5] == [poll] * 5
    assert delays[5:8] == [poll * 2, poll * 4, poll * 8]
