"""Run a blocking call with a hard deadline, on its own daemon thread.

Used wherever a network call sits beside a waiting human: the reviewer (``judge.py``) and
Weave feedback (``feedback.py``). A call that outlives its deadline is abandoned, never
joined again, and its eventual result is discarded; it must itself carry a transport timeout
so the abandoned thread ends.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

__all__ = ["Bounded", "run_bounded"]

T = TypeVar("T")


@dataclass(frozen=True)
class Bounded(Generic[T]):
    timed_out: bool
    value: T | None = None
    error: BaseException | None = None


def run_bounded(fn: Callable[[], T], timeout_s: float, *, name: str) -> Bounded[T]:
    """Call ``fn`` with a deadline. Never raises: the outcome says what happened."""
    box: dict[str, object] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller, classified there
            box["error"] = exc

    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        return Bounded(timed_out=True)
    error = box.get("error")
    if isinstance(error, BaseException):
        return Bounded(timed_out=False, error=error)
    return Bounded(timed_out=False, value=box.get("value"))  # type: ignore[arg-type]
