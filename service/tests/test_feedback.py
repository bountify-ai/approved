"""Weave feedback: the agreement table, payloads, idempotency, persistence, isolation."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from approved import logs
from approved.feedback import (
    AGREEMENT_TYPE,
    HUMAN_DECISION_TYPE,
    FeedbackSender,
    WeaveFeedbackClient,
    agreement,
    build_feedback,
)
from approved.state import DecisionRecord, JudgedEntry, StateStore

from .fakes import CallIdTracer, ScriptedReviewer, decision_event, request_event, verdict

URL = "https://facade.test/a/tenant-1"


@pytest.mark.parametrize(
    ("judge", "human", "label"),
    [
        ("READY", "granted", "agree"),
        ("READY", "rejected", "disagree"),
        ("REVISE", "rejected", "agree"),
        ("REVISE", "granted", "disagree"),
        ("ABORT", "rejected", "agree"),
        ("ABORT", "granted", "disagree"),
        ("NEEDS_HUMAN", "granted", "escalated"),
        ("NEEDS_HUMAN", "rejected", "escalated"),
        *[
            (j, h, None)
            for j in ("READY", "REVISE", "ABORT", "NEEDS_HUMAN")
            for h in ("expired", "withdrawn")
        ],
        (None, "granted", None),
        ("READY", None, None),
    ],
)
def test_agreement_table(judge: str | None, human: str | None, label: str | None) -> None:
    assert agreement(judge, human) == label


def _judged(decision: str = "READY", call_id: str | None = "call-1") -> JudgedEntry:
    return JudgedEntry(
        status="notified",
        seq=3,
        decision=decision,
        call_id=call_id,
        requested_ts="2026-09-29T12:00:00.000Z",
        action_class="vcs.push.main",
    )


def _decision(event: str = "approval.granted", seq: int = 5) -> DecisionRecord:
    return DecisionRecord(event=event, actor="human:carter", seq=seq, ts="2026-09-29T12:00:42.500Z")


def test_feedback_payloads() -> None:
    items = build_feedback("k1", _decision(), _judged())
    assert items == [
        (
            HUMAN_DECISION_TYPE,
            {
                "decision": "granted",
                "event": "approval.granted",
                "actor": "human:carter",
                "seq": 5,
                "ts": "2026-09-29T12:00:42.500Z",
                "latency_s": 42.5,
                "action_key": "k1",
            },
        ),
        (
            AGREEMENT_TYPE,
            {"label": "agree", "judge_decision": "READY", "human_decision": "granted", "seq": 5},
        ),
    ]


def test_expiry_gets_a_decision_item_but_no_label() -> None:
    items = build_feedback("k1", _decision("approval.expired"), _judged("NEEDS_HUMAN"))
    assert [t for t, _ in items] == [HUMAN_DECISION_TYPE]
    assert items[0][1]["decision"] == "expired"


# ------------------------------------------------------------------ fakes


class FakeClient:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, list[tuple[str, dict[str, Any]]]]] = []

    def add(self, call_id: str, items: list[tuple[str, dict[str, Any]]]) -> int:
        self.calls.append((call_id, items))
        if self.error is not None:
            raise self.error
        return len(items)


class _Row:
    def __init__(self, feedback_type: str, payload: dict[str, Any]) -> None:
        self.feedback_type = feedback_type
        self.payload = payload


class _Feedback(list):
    def add(self, feedback_type: str, payload: dict[str, Any]) -> str:
        self.append(_Row(feedback_type, payload))
        return f"fb-{len(self)}"


class _Call:
    def __init__(self) -> None:
        self.feedback = _Feedback()


class FakeWeaveClient:
    """The two ``WeaveClient`` surfaces used: ``get_call(id).feedback`` (iterate, ``add``)."""

    def __init__(self) -> None:
        self.calls: dict[str, _Call] = {}
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1

    def get_call(self, call_id: str) -> _Call:
        return self.calls.setdefault(call_id, _Call())


def _store(tmp_path: Path, *, event: str = "approval.granted") -> StateStore:
    store = StateStore(tmp_path, URL)
    store.settle("k1", _judged())
    store.record_decision("k1", _decision(event))
    store.save()
    return store


# ------------------------------------------------------------------ sender


def test_sends_once_and_is_idempotent(tmp_path: Path) -> None:
    store, client = _store(tmp_path), FakeClient()
    sender = FeedbackSender(store, client)
    assert sender.send("k1") is True
    assert sender.send("k1") is False
    assert sender.drain() == 0
    assert len(client.calls) == 1
    assert store.state.decisions["k1"].feedback == "sent"
    assert logs.METRICS.get("feedback.sent") == 1


def test_weave_client_skips_items_already_on_the_call() -> None:
    """Covers a crash between the Weave add and the state save: the retry adds nothing."""
    weave_client = FakeWeaveClient()
    client = WeaveFeedbackClient(weave_client)
    items = build_feedback("k1", _decision(), _judged())
    assert client.add("call-1", items) == 2
    assert client.add("call-1", items) == 0
    assert weave_client.flushes == 2  # a just-made call is uploaded before it is looked up
    assert [r.feedback_type for r in weave_client.calls["call-1"].feedback] == [
        HUMAN_DECISION_TYPE,
        AGREEMENT_TYPE,
    ]


def test_pending_feedback_survives_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    FeedbackSender(store, None).drain()  # Weave off: stays pending
    assert store.state.decisions["k1"].feedback == "pending"
    assert logs.METRICS.get("feedback.unavailable") == 1

    reloaded = StateStore(tmp_path, URL)  # a new process
    client = FakeClient()
    assert FeedbackSender(reloaded, client).drain() == 1
    assert client.calls[0][0] == "call-1"
    assert StateStore(tmp_path, URL).state.decisions["k1"].feedback == "sent"


def test_failure_is_isolated_counted_and_bounded(tmp_path: Path, logs_captured) -> None:
    store = _store(tmp_path)
    client = FakeClient(error=RuntimeError("weave down"))
    sender = FeedbackSender(store, client, max_attempts=3)
    assert sender.send("k1") is False
    record = store.state.decisions["k1"]
    assert (record.feedback, record.feedback_attempts, record.feedback_error) == (
        "failed",
        1,
        "RuntimeError",
    )
    sender.drain()
    sender.drain()
    sender.drain()  # attempts exhausted: no fourth call
    assert len(client.calls) == 3
    assert logs.METRICS.get("feedback.failed") == 3
    assert logs_captured.records("feedback.failed")[0]["level"] == "warning"


def test_feedback_timeout_is_a_failure(tmp_path: Path) -> None:
    release = threading.Event()

    class Hanging:
        def add(self, call_id: str, items: list) -> int:
            release.wait(5)
            return 0

    store = _store(tmp_path)
    try:
        assert FeedbackSender(store, Hanging(), timeout_s=0.05).send("k1") is False
    finally:
        release.set()
    assert store.state.decisions["k1"].feedback_error == "timeout"


def test_untraced_decisions_are_not_applicable(tmp_path: Path) -> None:
    store = StateStore(tmp_path, URL)
    store.settle("k1", _judged(call_id=None))
    store.record_decision("k1", _decision())
    client = FakeClient()
    assert FeedbackSender(store, client).drain() == 0
    assert client.calls == []


def test_worker_attaches_feedback_and_survives_a_failing_backend(
    make_worker, facade, telegram
) -> None:
    worker = make_worker(ScriptedReviewer([verdict()]), tracer=CallIdTracer())
    client = FakeClient(error=RuntimeError("down"))
    sender = FeedbackSender(worker.store, client)
    worker.on_decision, worker.on_idle = sender.on_decision, sender.drain
    facade.append(request_event("k1"), request_event("k2"))
    worker.run(once=True)
    facade.append(decision_event("k1", "approval.rejected"), request_event("k3"))
    worker.run(once=True)
    # The failing backend did not stop the loop: k3 was still judged.
    assert worker.store.state.judged["k3"].status == "notified"
    assert worker.store.state.decisions["k1"].feedback == "failed"

    client.error = None
    worker.run(once=True)  # idle tick retries
    assert worker.store.state.decisions["k1"].feedback == "sent"
    call_id, items = client.calls[-1]
    assert call_id == "call-k1"
    assert items[1][1]["label"] == "escalated"
