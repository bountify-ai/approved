"""The judge's persisted state: follow cursor, per-action ledger, decisions, chain break.

This file is a cache of what the judge has already done. It is never the truth about the
approval log (that is the log), and nothing in it is ever written back to the log.

Durability rules:

* **Atomic replacement.** Every save writes a sibling temp file, fsyncs it, ``os.replace``-s
  it over the old file and fsyncs the directory. A crash leaves either the old state or the
  new one, never half of each.
* **At most once per action_key.** The worker claims an action_key (and saves) *before* the
  reviewer runs, so a crash mid-review leaves the key claimed and a restart does not judge it
  again. The judge fails toward silence, never toward a duplicate message.
* **Fail closed on a bad file.** A state file that exists but does not parse refuses to load.
  Starting over from an empty ledger would re-judge requests already judged.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "HASH_PATTERN",
    "MAX_FEEDBACK_ATTEMPTS",
    "MAX_LEDGER_ENTRIES",
    "MAX_TASK_CONTEXT",
    "ChainBreak",
    "Cursor",
    "DecisionRecord",
    "JudgeState",
    "JudgedEntry",
    "StateError",
    "StateStore",
]

HASH_PATTERN = r"^[0-9a-f]{64}$"

#: Task summaries held for requests not yet closed (agent text, capped per entry).
MAX_TASK_CONTEXT = 1_000
TASK_SUMMARY_CHARS = 1_000

#: Feedback delivery attempts per decision before it is left as ``failed`` for good.
MAX_FEEDBACK_ATTEMPTS = 3

#: Ledger entries kept per map. The follow cursor only moves forward and stale requests are
#: never judged, so an evicted key cannot come back to be judged twice.
MAX_LEDGER_ENTRIES = 10_000

STATE_FILENAME = "judge-state.json"


def _now() -> datetime:
    return datetime.now(UTC)


class StateError(RuntimeError):
    """The state file cannot be trusted; the worker refuses to start."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Cursor(_Model):
    """Exclusive follow cursor: consumed through ``seq``, whose record hash is ``hash``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: Annotated[int, Field(ge=0)] = 0
    hash: Annotated[str, Field(pattern=HASH_PATTERN)] | None = None


JudgedStatus = Literal["claimed", "notified", "silent", "absent", "skipped"]


class JudgedEntry(_Model):
    """What the judge did with one ``approval.requested``.

    ``claimed``: reviewer started (a crash here stays claimed and is never re-judged).
    ``notified``: verdict produced and the advisory message delivered.
    ``silent``: verdict produced, message not delivered (Telegram off or failed).
    ``absent``: no verdict (timeout, parse error, inference error, open circuit).
    ``skipped``: not judged on purpose (already decided, or older than JUDGE_MAX_AGE_S).
    """

    status: JudgedStatus
    seq: int
    at: datetime = Field(default_factory=_now)
    decision: str | None = None
    reason: str | None = None
    call_id: str | None = None
    trace_url: str | None = None
    #: Request context, kept so feedback can compute latency and evaluation can replay it.
    requested_ts: str | None = None
    action_class: str | None = None
    summary: str | None = None


FeedbackStatus = Literal["pending", "sent", "failed", "not-applicable"]


def _advisory(judged: JudgedEntry) -> str:
    if judged.status == "notified":
        return "sent"
    if judged.status == "silent":
        return "silent:delivery-failed"
    if judged.status == "claimed":
        return "absent:interrupted"
    return f"absent:{judged.reason or judged.status}"


class DecisionRecord(_Model):
    """The human's (or the TTL sweep's) terminal answer, and its Weave feedback status.

    ``pending`` means the judge produced a traced verdict (a Weave call id) for this
    action_key and the feedback has not been delivered yet. ``not-applicable`` means there is
    no call to attach feedback to (not judged, judged offline, or no verdict).
    """

    event: str
    actor: str
    seq: int
    ts: str
    feedback: FeedbackStatus = "pending"
    feedback_attempts: int = 0
    feedback_error: str | None = None
    #: Whether the approver had the judge's advisory before deciding: ``sent``, or
    #: ``silent:<why>`` / ``absent:<why>`` when they decided without one.
    advisory: str = "unknown"


class UnverifiableRecord(_Model):
    seq: int
    event: str
    reason: str
    action_key: str | None = None
    at: datetime = Field(default_factory=_now)


MAX_UNVERIFIABLE = 200


class ChainBreak(_Model):
    reason: str
    at_seq: int
    detected_at: datetime = Field(default_factory=_now)


class JudgeState(_Model):
    version: Literal[1] = 1
    facade_url: str | None = None
    cursor: Cursor = Field(default_factory=Cursor)
    judged: dict[str, JudgedEntry] = Field(default_factory=dict)
    decisions: dict[str, DecisionRecord] = Field(default_factory=dict)
    #: action_key -> the task summary its ``task.registered`` gave, until the request closes.
    task_context: dict[str, str] = Field(default_factory=dict)
    #: Records whose links held but whose content did not verify (newest last, capped).
    unverifiable: list[UnverifiableRecord] = Field(default_factory=list)
    chain_break: ChainBreak | None = None


def _trim(mapping: dict[str, object], limit: int) -> None:
    """Drop the oldest entries (insertion order) beyond ``limit``."""
    excess = len(mapping) - limit
    if excess > 0:
        for key in list(mapping)[:excess]:
            del mapping[key]


_F = TypeVar("_F", bound=Callable[..., Any])


def _locked(method: _F) -> _F:
    """Serialise a StateStore method: the follow loop and the feedback thread share the store."""

    @functools.wraps(method)
    def wrapper(self: StateStore, *args: Any, **kwargs: Any) -> Any:
        with self.lock:
            return method(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


class StateStore:
    """Load and atomically persist :class:`JudgeState` under ``state_dir``."""

    def __init__(self, state_dir: Path, facade_url: str, *, max_entries: int = MAX_LEDGER_ENTRIES):
        self.dir = Path(state_dir)
        self.path = self.dir / STATE_FILENAME
        self.max_entries = max_entries
        self.lock = threading.RLock()
        self.state = self._load(facade_url)

    def _load(self, facade_url: str) -> JudgeState:
        if not self.path.exists():
            return JudgeState(facade_url=facade_url)
        try:
            state = JudgeState.model_validate_json(self.path.read_bytes())
        except (OSError, ValidationError, ValueError) as exc:
            raise StateError(
                f"{self.path} exists but cannot be read as judge state ({type(exc).__name__}); "
                "refusing to start rather than re-judge requests already judged"
            ) from None
        if state.facade_url is not None and state.facade_url != facade_url:
            raise StateError(
                f"{self.path} belongs to a different facade; its cursor and ledger describe "
                "another log. Point STATE_DIR at a fresh directory for this facade."
            )
        if state.facade_url is None:
            state.facade_url = facade_url
        return state

    # ----------------------------------------------------------------- mutations

    def is_known(self, action_key: str) -> bool:
        return action_key in self.state.judged

    @_locked
    def claim(self, action_key: str, seq: int) -> bool:
        """Mark ``action_key`` as being judged and persist. False if already known."""
        if action_key in self.state.judged:
            return False
        self.state.judged[action_key] = JudgedEntry(status="claimed", seq=seq)
        self.save()
        return True

    @_locked
    def settle(self, action_key: str, entry: JudgedEntry) -> None:
        self.state.judged[action_key] = entry
        self.save()

    @_locked
    def record_decision(self, action_key: str, decision: DecisionRecord) -> bool:
        """Remember a terminal decision once and close its task context.

        False if this action_key already has one. Feedback is ``pending`` only when there is a
        Weave call to attach it to.
        """
        self.state.task_context.pop(action_key, None)
        if action_key in self.state.decisions:
            return False
        judged = self.state.judged.get(action_key)
        if judged is None:
            # Decided before the judge ever saw it (backlog, restart, or a request older than
            # the judge's cursor). Recorded, so silence is visible, never mistaken for assent.
            judged = JudgedEntry(status="absent", seq=decision.seq, reason="not-seen")
            self.state.judged[action_key] = judged
        status = "pending" if judged.call_id else "not-applicable"
        self.state.decisions[action_key] = decision.model_copy(
            update={"feedback": status, "advisory": _advisory(judged)}
        )
        return True

    @_locked
    def remember_task_summary(self, action_key: str, summary: str) -> None:
        """Hold a task summary until the request closes. Persisted with the next save."""
        self.state.task_context.pop(action_key, None)
        self.state.task_context[action_key] = summary[:TASK_SUMMARY_CHARS]

    def task_summary(self, action_key: str) -> str | None:
        return self.state.task_context.get(action_key)

    @_locked
    def pending_feedback(self, max_attempts: int = MAX_FEEDBACK_ATTEMPTS) -> list[str]:
        return [
            key
            for key, d in self.state.decisions.items()
            if d.feedback in ("pending", "failed") and d.feedback_attempts < max_attempts
        ]

    @_locked
    def mark_feedback(self, action_key: str, status: FeedbackStatus, error: str | None = None):
        current = self.state.decisions[action_key]
        attempts = current.feedback_attempts + (0 if status == "not-applicable" else 1)
        self.state.decisions[action_key] = current.model_copy(
            update={"feedback": status, "feedback_attempts": attempts, "feedback_error": error}
        )
        self.save()

    @_locked
    def note_unverifiable(self, record: UnverifiableRecord) -> None:
        self.state.unverifiable.append(record)
        del self.state.unverifiable[:-MAX_UNVERIFIABLE]
        if record.action_key and record.event == "approval.requested":
            self.state.judged.setdefault(
                record.action_key,
                JudgedEntry(status="skipped", seq=record.seq, reason="record-unverifiable"),
            )
        self.save()

    @_locked
    def advance(self, cursor: Cursor) -> None:
        self.state.cursor = cursor
        self.save()

    @_locked
    def mark_chain_break(self, reason: str, at_seq: int) -> ChainBreak:
        brk = ChainBreak(reason=reason, at_seq=at_seq)
        self.state.chain_break = brk
        self.save()
        return brk

    # --------------------------------------------------------------- persistence

    @_locked
    def save(self) -> None:
        _trim(self.state.judged, self.max_entries)  # type: ignore[arg-type]
        _trim(self.state.decisions, self.max_entries)  # type: ignore[arg-type]
        _trim(self.state.task_context, MAX_TASK_CONTEXT)  # type: ignore[arg-type]
        self.dir.mkdir(parents=True, exist_ok=True)
        data = self.state.model_dump_json(indent=1).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{STATE_FILENAME}.", suffix=".tmp", dir=self.dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        _fsync_dir(self.dir)

    @_locked
    def snapshot(self) -> dict[str, object]:
        return json.loads(self.state.model_dump_json())


def _fsync_dir(directory: Path) -> None:
    """Make the rename durable. Not every platform can open a directory; that is not fatal."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
