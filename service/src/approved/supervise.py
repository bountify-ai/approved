"""Worker supervision for ``python -m approved serve``.

The console and the worker share one process. If the worker thread ends on its own (a chain
break, which is terminal, or an uncaught error), the process must not keep answering
``/health`` 200 as if the judge were running: the status turns ``degraded`` with a reason
(``/health`` then answers 503), and after a short grace, so the degraded answer is visible,
the server is told to exit and the process exits non-zero. The platform restarts it; a chain
break stays terminal across restarts because it is persisted in the judge's state.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .logs import METRICS, log

__all__ = ["DEGRADED_GRACE_S", "WorkerRunner"]

DEGRADED_GRACE_S = 3.0
EXIT_CHAIN_BREAK = 3
EXIT_WORKER_FAILED = 1


class WorkerRunner:
    """Runs ``target`` on a thread and, when it ends unrequested, degrades and shuts down."""

    def __init__(
        self,
        target: Callable[[], int],
        *,
        update_status: Callable[..., None],
        shutdown: Callable[[], None],
        grace_s: float = DEGRADED_GRACE_S,
        wait: Callable[[float], Any] | None = None,
    ) -> None:
        self._target = target
        self._update = update_status
        self._shutdown = shutdown
        self._grace_s = grace_s
        self._stopping = threading.Event()
        self._wait = wait or self._stopping.wait
        self.exit_code: int | None = None
        self.degraded_reason: str | None = None
        self._thread = threading.Thread(target=self._run, name="worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def request_stop(self) -> None:
        self._stopping.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        try:
            code = self._target()
        except Exception as exc:  # noqa: BLE001 - a crashed worker degrades, it does not vanish
            log("worker.crashed", level="error", error=type(exc).__name__)
            METRICS.incr("worker.crashed")
            code, reason = EXIT_WORKER_FAILED, f"worker-crashed:{type(exc).__name__}"
        else:
            reason = "chain-break" if code == EXIT_CHAIN_BREAK else "worker-exited"
        self.exit_code = code
        if self._stopping.is_set():
            return  # a requested stop (SIGTERM): not degraded
        self.degraded_reason = reason
        self.exit_code = code or EXIT_WORKER_FAILED
        self._update(degraded=True, degraded_reason=reason, running=False)
        log("worker.degraded", level="error", reason=reason, exit_code=self.exit_code)
        self._wait(self._grace_s)
        self._shutdown()
