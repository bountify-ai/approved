"""The judge: one advisory verdict per action_key, bounded, traced, and never load-bearing.

Every property here is about keeping a model out of the approver's path (the same design as
approval-md ``src/cli/gloss.ts``):

* **Bounded.** The reviewer runs in its own thread with a hard deadline
  (``JUDGE_TIMEOUT_S``). A reviewer that outlives it is abandoned, its eventual answer is
  discarded, and the request gets no message.
* **Fails toward absence.** Timeout, parse failure, inference error or an open circuit all
  return an outcome with ``verdict=None`` and a machine-readable ``absent_reason``, and bump a
  ``judge.absent.<reason>`` metric. Nothing raises out of :meth:`Judge.evaluate`, nothing
  retries, and nothing waits on a human.
* **Circuit breaker.** After ``threshold`` consecutive failures the breaker opens and the
  reviewer is not called at all until ``cooldown_s`` has passed; then one trial call is let
  through (half-open). Success closes it; failure re-opens it.
* **At most once per action_key.** :meth:`Judge.judge_once` claims the key in the persisted
  ledger before the reviewer runs.
* **Traced when configured.** With Weave initialised the reviewer call is a ``weave.op`` named
  ``approved.judge`` and the outcome carries the call id and its URL. Weave is optional: when
  offline, unconfigured, or failing to initialise, tracing is a no-op and never raises.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .config import Settings
from .logs import METRICS, log
from .reviewer import (
    InferenceError,
    JudgeRequest,
    Reviewer,
    ReviewerError,
    ReviewerParseError,
    Verdict,
)
from .text import redact

__all__ = [
    "AbsentReason",
    "CircuitBreaker",
    "Judge",
    "JudgeOutcome",
    "Ledger",
    "Tracer",
    "WeaveTracer",
    "init_weave",
]

AbsentReason = Literal["timeout", "parse", "inference", "circuit-open", "error"]

# Weave ships its own Sentry error reporting, which would send exception context (and with it
# fragments of tenant requests) to a third party. Off unless an operator opts in explicitly.
# Set before weave is first imported, which only ever happens inside this module.
os.environ.setdefault("WANDB_ERROR_REPORTING", "false")

WEAVE_OP_NAME = "approved.judge"


# ---------------------------------------------------------------- circuit breaker


class CircuitBreaker:
    """Closed -> open after N consecutive failures -> half-open after a cool-down."""

    def __init__(
        self,
        threshold: int = 3,
        cooldown_s: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> Literal["closed", "open", "half-open"]:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> Literal["closed", "open", "half-open"]:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.cooldown_s:
            return "half-open"
        return "open"

    def allow(self) -> bool:
        """Whether a call may go through now. Half-open admits exactly one trial."""
        with self._lock:
            state = self._state_locked()
            if state == "closed":
                return True
            if state == "half-open" and not self._trial_in_flight:
                self._trial_in_flight = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._trial_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._trial_in_flight or self._failures >= self.threshold:
                self._opened_at = self._clock()
            self._trial_in_flight = False


# ---------------------------------------------------------------- tracing


class Tracer(Protocol):
    """Runs a reviewer call under a trace and returns ``(verdict, call_id)``."""

    def run(self, reviewer: Reviewer, request: JudgeRequest) -> tuple[Verdict, str | None]: ...


class _NoTracer:
    def run(self, reviewer: Reviewer, request: JudgeRequest) -> tuple[Verdict, str | None]:
        return reviewer.review(request), None


def _redact_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in inputs.items():
        dump = getattr(value, "model_dump", None)
        out[key] = redact(dump(mode="json")) if callable(dump) else redact(value)
    return out


def _redact_output(output: Any) -> Any:
    dump = getattr(output, "model_dump", None)
    return redact(dump(mode="json")) if callable(dump) else redact(output)


class WeaveTracer:
    """Wraps the reviewer in a ``weave.op`` built once per reviewer instance.

    ``Op.call`` never raises (weave captures the exception on the call), so the wrapped
    function parks any exception in a thread-local and :meth:`run` re-raises it. Each judge
    runs on its own thread, so the slot cannot be crossed between requests.
    """

    def __init__(self) -> None:
        self._ops: dict[int, Any] = {}
        self._errors = threading.local()
        self._lock = threading.Lock()

    def _op_for(self, reviewer: Reviewer) -> Any:
        key = id(reviewer)
        with self._lock:
            op = self._ops.get(key)
            if op is None:
                import weave

                errors = self._errors

                def judge(request: JudgeRequest) -> Verdict:
                    try:
                        return reviewer.review(request)
                    except BaseException as exc:
                        errors.value = exc
                        raise

                op = weave.op(
                    judge,
                    name=WEAVE_OP_NAME,
                    postprocess_inputs=_redact_inputs,
                    postprocess_output=_redact_output,
                )
                self._ops[key] = op
            return op

    def run(self, reviewer: Reviewer, request: JudgeRequest) -> tuple[Verdict, str | None]:
        op = self._op_for(reviewer)
        self._errors.value = None
        result, call = op.call(request)
        error = getattr(self._errors, "value", None)
        if error is not None:
            raise error
        if not isinstance(result, Verdict):
            raise ReviewerError("traced reviewer returned no verdict")
        call_id = getattr(call, "id", None)
        return result, (str(call_id) if call_id else None)


def init_weave(settings: Settings) -> Tracer | None:
    """Initialise Weave for ``<entity>/<project>``; ``None`` when off. Never raises.

    Off when ``OFFLINE=1`` or no W&B key is configured. The wandb client reads the key from
    the environment, so a key supplied only as ``WANDB_API_KEY_FILE`` is placed there (as
    judgy ``weave_observability.init`` does); it is never logged.
    """
    if settings.offline or settings.wandb_api_key is None:
        log("weave.off", reason="offline" if settings.offline else "no-wandb-key")
        return None
    try:
        os.environ.setdefault("WANDB_API_KEY", settings.wandb_api_key.get_secret_value())
        import weave

        weave.init(settings.weave_project)
    except Exception as exc:  # noqa: BLE001 - observability is never load-bearing
        log("weave.init-failed", level="warning", error=type(exc).__name__)
        METRICS.incr("weave.init_failed")
        return None
    log("weave.on", project=settings.weave_project)
    return WeaveTracer()


# ---------------------------------------------------------------- judge


@dataclass(frozen=True)
class JudgeOutcome:
    action_key: str
    verdict: Verdict | None
    absent_reason: AbsentReason | None = None
    call_id: str | None = None
    trace_url: str | None = None
    latency_s: float = 0.0


class Ledger(Protocol):
    def claim(self, action_key: str, seq: int) -> bool: ...


class Judge:
    def __init__(
        self,
        reviewer: Reviewer,
        *,
        timeout_s: float,
        breaker: CircuitBreaker | None = None,
        tracer: Tracer | None = None,
        trace_url: Callable[[str | None], str | None] = lambda _id: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.reviewer = reviewer
        self.timeout_s = timeout_s
        self.breaker = breaker or CircuitBreaker()
        self.tracer: Tracer = tracer or _NoTracer()
        self._trace_url = trace_url
        self._clock = clock

    def judge_once(self, request: JudgeRequest, ledger: Ledger) -> JudgeOutcome | None:
        """Judge ``request`` unless its action_key was ever claimed. ``None`` if it was."""
        if not ledger.claim(request.action_key, request.seq):
            METRICS.incr("judge.duplicate")
            return None
        return self.evaluate(request)

    def evaluate(self, request: JudgeRequest) -> JudgeOutcome:
        """One bounded reviewer call. Never raises."""
        if not self.breaker.allow():
            return self._absent(request, "circuit-open", 0.0)

        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["result"] = self.tracer.run(self.reviewer, request)
            except BaseException as exc:  # noqa: BLE001 - handed to the caller, classified there
                box["error"] = exc

        started = self._clock()
        thread = threading.Thread(target=target, name=f"judge-seq-{request.seq}", daemon=True)
        thread.start()
        thread.join(self.timeout_s)
        latency = self._clock() - started

        if thread.is_alive():
            self.breaker.record_failure()
            return self._absent(request, "timeout", latency)
        error = box.get("error")
        if error is not None:
            self.breaker.record_failure()
            if isinstance(error, ReviewerParseError):
                reason: AbsentReason = "parse"
            elif isinstance(error, InferenceError | ReviewerError):
                reason = "inference"
            else:
                reason = "error"
            return self._absent(request, reason, latency, error=type(error).__name__)

        verdict, call_id = box["result"]
        self.breaker.record_success()
        METRICS.incr("judge.verdict")
        METRICS.incr(f"judge.verdict.{verdict.decision.value}")
        outcome = JudgeOutcome(
            action_key=request.action_key,
            verdict=verdict,
            call_id=call_id,
            trace_url=self._trace_url(call_id),
            latency_s=latency,
        )
        log(
            "judge.verdict",
            action_key=request.action_key,
            seq=request.seq,
            decision=verdict.decision.value,
            reviewer=verdict.reviewer_version,
            call_id=call_id,
            latency_s=round(latency, 3),
        )
        return outcome

    def _absent(
        self,
        request: JudgeRequest,
        reason: AbsentReason,
        latency: float,
        *,
        error: str | None = None,
    ) -> JudgeOutcome:
        METRICS.incr("judge.absent")
        METRICS.incr(f"judge.absent.{reason}")
        log(
            "judge.absent",
            level="warning",
            action_key=request.action_key,
            seq=request.seq,
            reason=reason,
            error=error,
            breaker=self.breaker.state,
            latency_s=round(latency, 3),
        )
        return JudgeOutcome(
            action_key=request.action_key,
            verdict=None,
            absent_reason=reason,
            latency_s=latency,
        )
