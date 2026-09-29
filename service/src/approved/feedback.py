"""Weave feedback: attach the human's decision to the judge call that commented on it.

When ``approval.granted``, ``approval.rejected``, ``approval.expired`` or
``approval.withdrawn`` appears for an action_key the judge reviewed with a traced call, two
feedback items go onto that Weave call:

* ``approved.human_decision``: ``{decision, event, actor, seq, ts, latency_s, action_key}``,
  where ``latency_s`` runs from the request's log timestamp to the decision's.
* ``approved.agreement``: ``{label, judge_decision, human_decision, seq}``, from
  :func:`agreement`. No agreement item is written when the label is ``None``.

Agreement mapping (the judge's verdict against what the human did):

=============  ==========  ==========  ==========  ==========
judge          granted     rejected    expired     withdrawn
=============  ==========  ==========  ==========  ==========
READY          agree       disagree    (none)      (none)
REVISE         disagree    agree       (none)      (none)
ABORT          disagree    agree       (none)      (none)
NEEDS_HUMAN    escalated   escalated   (none)      (none)
=============  ==========  ==========  ==========  ==========

``escalated`` is scored separately: sending a decision to the human is the judge declining to
call it, which is neither right nor wrong about the outcome. An expiry or a withdrawal says
nothing about whether the action was acceptable, so it carries no label.

Weave API (weave 0.53.11, the version pinned in ``uv.lock``): ``weave.init`` returns a
``WeaveClient``; ``WeaveClient.get_call(call_id)`` returns a ``Call`` whose ``.feedback`` is a
``RefFeedbackQuery`` that iterates existing ``Feedback`` rows (``feedback_type``,
``payload``) and has ``.add(feedback_type, payload)`` (``weave/trace/weave_client.py``
``get_call``; ``weave/trace/feedback.py`` ``RefFeedbackQuery.add``).

Delivery properties:

* **Persistent.** The action_key -> call id mapping lives in the judge's state
  (``JudgedEntry.call_id``); a decision starts ``pending`` and becomes ``sent``, so feedback
  that could not be delivered before a restart is delivered after it.
* **Idempotent.** A decision is sent only while ``pending`` or ``failed``. Before adding, the
  call's existing feedback is read and an item of the same type for the same decision seq is
  not added again, which also covers a crash between the add and the state save.
* **Fails toward absence.** Each delivery runs with a deadline; a timeout or an error is
  logged, counted (``feedback.failed``) and retried on a later idle tick, at most
  ``MAX_FEEDBACK_ATTEMPTS`` times in all. Nothing here can stop the follow loop.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from .bounded import run_bounded
from .logs import METRICS, log
from .state import MAX_FEEDBACK_ATTEMPTS, DecisionRecord, JudgedEntry, StateStore
from .text import redact

__all__ = [
    "AGREEMENT_TYPE",
    "HUMAN_DECISION_TYPE",
    "AgreementLabel",
    "FeedbackClient",
    "FeedbackSender",
    "WeaveFeedbackClient",
    "agreement",
    "build_feedback",
    "human_decision",
]

QUEUE_SIZE = 256
HUMAN_DECISION_TYPE = "approved.human_decision"
AGREEMENT_TYPE = "approved.agreement"

AgreementLabel = Literal["agree", "disagree", "escalated"]
HumanDecision = Literal["granted", "rejected", "expired", "withdrawn"]

_EVENT_TO_DECISION: dict[str, HumanDecision] = {
    "approval.granted": "granted",
    "approval.rejected": "rejected",
    "approval.expired": "expired",
    "approval.withdrawn": "withdrawn",
}


def human_decision(event: str) -> HumanDecision | None:
    return _EVENT_TO_DECISION.get(event)


def agreement(judge_decision: str | None, human: str | None) -> AgreementLabel | None:
    """The agreement label for a judge verdict and a human outcome (table in module doc)."""
    if human not in ("granted", "rejected") or judge_decision is None:
        return None
    if judge_decision == "NEEDS_HUMAN":
        return "escalated"
    if judge_decision == "READY":
        return "agree" if human == "granted" else "disagree"
    if judge_decision in ("REVISE", "ABORT"):
        return "agree" if human == "rejected" else "disagree"
    return None


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def build_feedback(
    action_key: str, decision: DecisionRecord, judged: JudgedEntry
) -> list[tuple[str, dict[str, Any]]]:
    """The (feedback_type, payload) items for one decision, in the order they are added."""
    human = human_decision(decision.event)
    requested, decided = _ts(judged.requested_ts), _ts(decision.ts)
    latency = round((decided - requested).total_seconds(), 3) if requested and decided else None
    items: list[tuple[str, dict[str, Any]]] = [
        (
            HUMAN_DECISION_TYPE,
            {
                "decision": human,
                "event": decision.event,
                "actor": decision.actor,
                "seq": decision.seq,
                "ts": decision.ts,
                "latency_s": latency,
                "action_key": action_key,
            },
        )
    ]
    items[0] = (HUMAN_DECISION_TYPE, redact(items[0][1]))
    label = agreement(judged.decision, human)
    if label is not None:
        items.append(
            (
                AGREEMENT_TYPE,
                {
                    "label": label,
                    "judge_decision": judged.decision,
                    "human_decision": human,
                    "seq": decision.seq,
                },
            )
        )
    return items


class FeedbackClient(Protocol):
    """Adds feedback items to one call, skipping items already present. Returns items added."""

    def add(self, call_id: str, items: list[tuple[str, dict[str, Any]]]) -> int: ...


class WeaveFeedbackClient:
    """:class:`FeedbackClient` over the ``WeaveClient`` that ``weave.init`` returned."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def add(self, call_id: str, items: list[tuple[str, dict[str, Any]]]) -> int:
        # Calls are uploaded asynchronously; flush so a call made moments ago is queryable.
        flush = getattr(self._client, "flush", None)
        if callable(flush):
            flush()
        call = self._client.get_call(call_id)
        present = {(row.feedback_type, (row.payload or {}).get("seq")) for row in call.feedback}
        added = 0
        for feedback_type, payload in items:
            if (feedback_type, payload.get("seq")) in present:
                continue
            call.feedback.add(feedback_type, payload)
            added += 1
        return added


class FeedbackSender:
    """Delivers pending decisions to Weave. Wire :meth:`on_decision` and :meth:`drain` into
    the worker's ``on_decision`` and ``on_idle`` hooks."""

    def __init__(
        self,
        store: StateStore,
        client: FeedbackClient | None,
        *,
        timeout_s: float = 15.0,
        max_attempts: int = MAX_FEEDBACK_ATTEMPTS,
        queue_size: int = QUEUE_SIZE,
    ) -> None:
        self.store = store
        self.client = client
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        self.queue_size = queue_size
        self._queue: queue.Queue[str | None] | None = None
        self._queued: set[str] = set()
        self._queued_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    # ------------------------------------------------------------------ threading

    def start(self) -> None:
        """Deliver on a background thread from a bounded queue. The follow loop then never
        waits on Weave: a full queue drops the submission (counted); the decision stays
        ``pending`` in state and the next idle drain offers it again."""
        if self._thread is not None:
            return
        self._queue = queue.Queue(maxsize=self.queue_size)
        self._thread = threading.Thread(target=self._run, name="feedback", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        if self._queue is None or self._thread is None:
            return
        self._stopping.set()
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout_s)

    def _run(self) -> None:
        assert self._queue is not None
        while True:
            try:
                key = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stopping.is_set():
                    return
                continue
            if key is None or self._stopping.is_set():
                return
            with self._queued_lock:
                self._queued.discard(key)
            try:
                self.send(key)
            except Exception as exc:  # noqa: BLE001 - one bad send never stops delivery
                METRICS.incr("feedback.failed")
                log("feedback.failed", level="warning", action_key=key, error=type(exc).__name__)

    def submit(self, action_key: str) -> bool:
        """Queue (threaded) or send now (not started). Never blocks the caller when started."""
        if self._queue is None:
            return self.send(action_key)
        with self._queued_lock:
            if action_key in self._queued:
                return False
            try:
                self._queue.put_nowait(action_key)
            except queue.Full:
                METRICS.incr("feedback.dropped")
                log("feedback.dropped", level="warning", action_key=action_key)
                return False
            self._queued.add(action_key)
        return False

    # ------------------------------------------------------------------ hooks

    def on_decision(
        self, action_key: str, decision: DecisionRecord, judged: JudgedEntry | None
    ) -> None:
        self.submit(action_key)

    def drain(self) -> int:
        """Offer every pending or failed decision once. Returns how many were delivered now
        (always 0 when started: delivery then happens on the feedback thread)."""
        return sum(self.submit(key) for key in self.store.pending_feedback(self.max_attempts))

    def send(self, action_key: str) -> bool:
        decision = self.store.state.decisions.get(action_key)
        judged = self.store.state.judged.get(action_key)
        if decision is None or decision.feedback not in ("pending", "failed"):
            return False
        if decision.feedback_attempts >= self.max_attempts:
            return False
        if judged is None or not judged.call_id:
            self.store.mark_feedback(action_key, "not-applicable")
            return False
        if self.client is None:
            METRICS.incr("feedback.unavailable")
            log("feedback.unavailable", action_key=action_key, detail="weave is off")
            return False

        client, call_id = self.client, judged.call_id
        items = build_feedback(action_key, decision, judged)
        run = run_bounded(
            lambda: client.add(call_id, items), self.timeout_s, name=f"feedback-{decision.seq}"
        )
        if run.timed_out or run.error is not None:
            error = "timeout" if run.timed_out else type(run.error).__name__
            self.store.mark_feedback(action_key, "failed", error)
            METRICS.incr("feedback.failed")
            log(
                "feedback.failed",
                level="warning",
                action_key=action_key,
                call_id=call_id,
                error=error,
                attempts=self.store.state.decisions[action_key].feedback_attempts,
            )
            return False
        self.store.mark_feedback(action_key, "sent")
        METRICS.incr("feedback.sent")
        log(
            "feedback.sent",
            action_key=action_key,
            call_id=call_id,
            items=[t for t, _ in items],
            added=run.value,
        )
        return True
