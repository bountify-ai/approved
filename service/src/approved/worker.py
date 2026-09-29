"""The judge worker loop: follow the log, judge new requests, notify, remember decisions.

One step:

1. Fetch one ``/log/follow`` page from the persisted cursor and verify its continuity.
2. For each record, in log order:
   * ``task.registered``: persist each action's summary until its request closes, so a
     restart between the registration and the request keeps the reviewer's context;
   * ``approval.requested``: judge it at most once, unless it is already decided later in the
     same page or older than ``JUDGE_MAX_AGE_S`` (a verdict nobody can use is noise);
   * ``approval.granted`` / ``rejected`` / ``expired`` / ``withdrawn``: record the decision,
     close the task context, and call the decision hook (Weave feedback, ``feedback.py``).
3. Persist the cursor (atomic write) after the page is processed.

Ordering gives at-least-once page processing with at-most-once judging: the action_key is
claimed and saved before the reviewer runs, so replaying a page after a crash judges nothing
twice and sends no duplicate message.

A chain break stops everything: it is persisted, logged as ``chain-break``, announced once to
the approver with fixed text, and the worker exits with :data:`EXIT_CHAIN_BREAK`. A restart
with the break still recorded refuses to follow until an operator clears the state. Transport
and credential failures are retried with capped exponential backoff; the cursor never moves on
a page that was not verified.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .follow import ChainBreakError, CredentialError, FacadeClient, FollowError, LogRecord
from .judge import Judge, JudgeOutcome
from .logs import METRICS, log
from .notify import Notifier, format_advisory, format_chain_break
from .reviewer import JudgeRequest
from .state import Cursor, DecisionRecord, JudgedEntry, StateStore

__all__ = [
    "EXIT_CHAIN_BREAK",
    "EXIT_FOLLOW_ERROR",
    "EXIT_OK",
    "TERMINAL_EVENTS",
    "DecisionHook",
    "StepResult",
    "Worker",
    "policy_rule_for",
]

EXIT_OK = 0
#: ``--once`` could not finish a verified pass (transport or credential failure).
EXIT_FOLLOW_ERROR = 1
EXIT_CHAIN_BREAK = 3

REQUEST_EVENT = "approval.requested"
TASK_EVENT = "task.registered"
TERMINAL_EVENTS = frozenset(
    {"approval.granted", "approval.rejected", "approval.expired", "approval.withdrawn"}
)

MAX_BACKOFF_S = 60.0
SUMMARY_CHARS = 500
POLICY_CONTEXT_LINES = 6

#: Called once per terminal decision: (action_key, decision, what the judge did or None).
DecisionHook = Callable[[str, DecisionRecord, JudgedEntry | None], None]


def _noop_hook(action_key: str, decision: DecisionRecord, judged: JudgedEntry | None) -> None:
    return None


def _noop() -> None:
    return None


class FollowStatus:
    """What the worker knows about its follow, for the console. Thread-safe snapshots.

    Updated by the worker thread on every step; read by the web thread. The console never
    calls the facade itself: this and the state file are all it shows.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._data: dict[str, Any] = {
            "running": False,
            "started_at": None,
            "last_follow_at": None,
            "last_follow_ok": None,
            "last_error": None,
            "chain": "unknown",
            "head_seq": 0,
            "caught_up": False,
            "pages": 0,
        }

    def update(self, **fields: Any) -> None:
        with self._lock:
            self._data.update(fields)

    def followed(self, *, ok: bool, error: str | None = None, **fields: Any) -> None:
        self.update(last_follow_at=self._clock(), last_follow_ok=ok, last_error=error, **fields)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)


@dataclass(frozen=True)
class StepResult:
    caught_up: bool
    records: int
    chain_break: bool = False
    error: str | None = None


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).timestamp()
    except ValueError:
        return None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def policy_rule_for(policy_file: Path | None, action_class: str | None) -> str | None:
    """Lines of the tenant policy that mention ``action_class``, with a little context.

    Best effort: an unset, unreadable or silent policy yields ``None``, and the prompt says
    the rule was not readable rather than the reviewer guessing one.
    """
    if policy_file is None or not action_class:
        return None
    try:
        lines = policy_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    keep: set[int] = set()
    for index, line in enumerate(lines):
        if action_class in line:
            keep.update(range(max(0, index - 1), min(len(lines), index + POLICY_CONTEXT_LINES)))
    if not keep:
        return None
    return "\n".join(lines[i] for i in sorted(keep))


class Worker:
    def __init__(
        self,
        *,
        facade: FacadeClient,
        judge: Judge,
        notifier: Notifier,
        store: StateStore,
        follow_limit: int = 200,
        poll_interval_s: float = 5.0,
        max_age_s: float = 900.0,
        policy_file: Path | None = None,
        on_decision: DecisionHook = _noop_hook,
        on_idle: Callable[[], object] = _noop,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.facade = facade
        self.judge = judge
        self.notifier = notifier
        self.store = store
        self.follow_limit = follow_limit
        self.poll_interval_s = poll_interval_s
        self.max_age_s = max_age_s
        self.policy_file = policy_file
        self.on_decision = on_decision
        self.on_idle = on_idle
        self._clock = clock
        self._stop = threading.Event()
        self.status = FollowStatus(clock)
        self.status.update(head_seq=store.state.cursor.seq)
        self._failures = 0

    # ------------------------------------------------------------------ control

    def stop(self) -> None:
        """Request a graceful stop: the in-flight step finishes, state is saved, run returns."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run(self, *, once: bool = False) -> int:
        """Loop until stopped (or, with ``once``, until caught up). Returns an exit code."""
        brk = self.store.state.chain_break
        if brk is not None:
            self.status.update(chain="chain-break", last_error=f"chain-break at seq {brk.at_seq}")
            log(
                "chain-break",
                level="error",
                at_seq=brk.at_seq,
                reason=brk.reason,
                persisted=True,
                detail="judging stays stopped until an operator investigates and clears state",
            )
            return EXIT_CHAIN_BREAK
        log("worker.start", cursor_seq=self.store.state.cursor.seq, once=once)
        self.status.update(running=True, started_at=self._clock())
        try:
            return self._loop(once=once)
        finally:
            self.status.update(running=False)

    def _loop(self, *, once: bool) -> int:
        self._idle()
        while not self._stop.is_set():
            result = self.step()
            if result.chain_break:
                return EXIT_CHAIN_BREAK
            if result.error is not None:
                self._failures += 1
                delay = min(self.poll_interval_s * (2 ** (self._failures - 1)), MAX_BACKOFF_S)
                if once:
                    log("worker.stop", reason=result.error, metrics=METRICS.snapshot())
                    return EXIT_FOLLOW_ERROR
                self._stop.wait(delay)
                continue
            self._failures = 0
            if result.caught_up:
                self._idle()
                if once:
                    break
                self._stop.wait(self.poll_interval_s)
        log("worker.stop", cursor_seq=self.store.state.cursor.seq, metrics=METRICS.snapshot())
        return EXIT_OK

    def _idle(self) -> None:
        """Background duties between pages (feedback retries). Never stops the loop."""
        try:
            self.on_idle()
        except Exception as exc:  # noqa: BLE001 - an idle duty never stops the loop
            METRICS.incr("worker.idle_failed")
            log("worker.idle-failed", level="warning", error=type(exc).__name__)

    # ------------------------------------------------------------------ one step

    def step(self) -> StepResult:
        cursor = self.store.state.cursor
        try:
            page = self.facade.follow_page(cursor, self.follow_limit)
        except ChainBreakError as brk:
            self.status.followed(
                ok=False, error=f"chain-break at seq {brk.at_seq}", chain="chain-break"
            )
            self._chain_break(brk)
            return StepResult(caught_up=False, records=0, chain_break=True)
        except CredentialError as exc:
            METRICS.incr("follow.credential_refused")
            log("follow.credential-refused", level="error", status=exc.status, code=exc.code)
            self.status.followed(ok=False, error=f"credential refused (HTTP {exc.status})")
            return StepResult(caught_up=False, records=0, error="credential")
        except FollowError as exc:
            METRICS.incr("follow.error")
            log("follow.error", level="warning", error=str(exc), cursor_seq=cursor.seq)
            self.status.followed(ok=False, error=str(exc))
            return StepResult(caught_up=False, records=0, error="follow")

        decided_later = {
            r.action_key for r in page.records if r.event in TERMINAL_EVENTS and r.action_key
        }
        for record in page.records:
            if self._stop.is_set() and record.event == REQUEST_EVENT:
                # Stop before starting a new judgement; the cursor stays before this record.
                self.store.save()
                return StepResult(caught_up=False, records=0)
            self._handle(record, decided_later)
            # Track progress in memory: every save inside the page (each claim and settle)
            # then persists how far it got, and a crash replays only records after that save,
            # which are idempotent.
            self.store.state.cursor = Cursor(seq=record.seq, hash=record.hash)
        self.store.advance(page.cursor)
        METRICS.incr("follow.pages")
        self.status.followed(
            ok=True,
            chain="verified",
            head_seq=page.cursor.seq,
            caught_up=page.caught_up,
            pages=self.status.snapshot()["pages"] + 1,
        )
        return StepResult(caught_up=page.caught_up, records=len(page.records))

    # ------------------------------------------------------------------ records

    def _handle(self, record: LogRecord, decided_later: set[str]) -> None:
        if record.event == TASK_EVENT:
            self._remember_task(record)
        elif record.event == REQUEST_EVENT:
            self._on_request(record, decided_later)
        elif record.event in TERMINAL_EVENTS:
            self._on_decision(record)

    def _remember_task(self, record: LogRecord) -> None:
        actions = record.payload.get("actions")
        if not isinstance(actions, list):
            return
        for action in actions:
            if isinstance(action, dict):
                key = _str(action.get("idempotency_key"))
                summary = _str(action.get("summary"))
                if key and summary and not self.store.is_known(key):
                    self.store.remember_task_summary(key, summary)

    def _request_for(self, record: LogRecord) -> JudgeRequest:
        payload = record.payload
        action_key = record.action_key or ""
        action_class = _str(payload.get("class"))
        task_summary = self.store.task_summary(action_key) if action_key else None
        return JudgeRequest(
            action_key=action_key,
            action_class=action_class,
            seq=record.seq,
            requested_ts=record.ts,
            summary=_str(payload.get("summary")),
            command=_str(payload.get("command")),
            task_summary=task_summary,
            est_cost_usd=_str(payload.get("est_cost_usd")),
            execution=_str(payload.get("execution")),
            policy_rule=policy_rule_for(self.policy_file, action_class),
        )

    def _skip(self, action_key: str, seq: int, reason: str) -> None:
        if self.store.is_known(action_key):
            return
        METRICS.incr(f"judge.skipped.{reason}")
        log("judge.skipped", action_key=action_key, seq=seq, reason=reason)
        self.store.settle(action_key, JudgedEntry(status="skipped", seq=seq, reason=reason))

    def _on_request(self, record: LogRecord, decided_later: set[str]) -> None:
        action_key = record.action_key
        if not action_key:
            METRICS.incr("judge.skipped.no-action-key")
            log("judge.skipped", seq=record.seq, reason="no-action-key")
            return
        if self.store.is_known(action_key):
            METRICS.incr("judge.duplicate")
            return
        if action_key in decided_later:
            self._skip(action_key, record.seq, "already-decided")
            return
        requested = _parse_ts(record.ts)
        if requested is not None and self._clock() - requested > self.max_age_s:
            self._skip(action_key, record.seq, "stale")
            return

        request = self._request_for(record)
        outcome = self.judge.judge_once(request, self.store)
        if outcome is None:
            return
        self._deliver(request, outcome)

    def _deliver(self, request: JudgeRequest, outcome: JudgeOutcome) -> None:
        verdict = outcome.verdict
        requested_ts = request.requested_ts
        action_class = request.action_class
        summary = request.summary[:SUMMARY_CHARS] if request.summary else None
        if verdict is None:
            self.store.settle(
                request.action_key,
                JudgedEntry(
                    status="absent",
                    seq=request.seq,
                    reason=outcome.absent_reason,
                    requested_ts=requested_ts,
                    action_class=action_class,
                    summary=summary,
                ),
            )
            return
        sent = self.notifier.send(format_advisory(verdict, outcome.trace_url, request.action_class))
        self.store.settle(
            request.action_key,
            JudgedEntry(
                status="notified" if sent else "silent",
                seq=request.seq,
                decision=verdict.decision.value,
                call_id=outcome.call_id,
                trace_url=outcome.trace_url,
                requested_ts=requested_ts,
                action_class=action_class,
                summary=summary,
            ),
        )

    def _on_decision(self, record: LogRecord) -> None:
        action_key = record.action_key
        if not action_key:
            return
        decision = DecisionRecord(
            event=record.event, actor=record.actor, seq=record.seq, ts=record.ts
        )
        if not self.store.record_decision(action_key, decision):
            return
        judged = self.store.state.judged.get(action_key)
        METRICS.incr(f"decision.{record.event.removeprefix('approval.')}")
        log(
            "decision.recorded",
            action_key=action_key,
            seq=record.seq,
            decision_event=record.event,
            judged=judged.status if judged else None,
        )
        try:
            self.on_decision(action_key, self.store.state.decisions[action_key], judged)
        except Exception as exc:  # noqa: BLE001 - a feedback hook never stops the loop
            METRICS.incr("decision.hook_failed")
            log("decision.hook-failed", level="warning", error=type(exc).__name__)

    # ------------------------------------------------------------------ chain break

    def _chain_break(self, brk: ChainBreakError) -> None:
        METRICS.incr("follow.chain_break")
        self.store.mark_chain_break(brk.reason, brk.at_seq)
        log(
            "chain-break",
            level="error",
            at_seq=brk.at_seq,
            reason=brk.reason,
            cursor_seq=self.store.state.cursor.seq,
            detail="judging stopped; cursor kept; nothing skipped",
        )
        self.notifier.send(format_chain_break(brk.at_seq, brk.reason))
