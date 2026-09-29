"""Structured JSON logging and in-process counters.

Every line the service writes is one JSON object on stderr: ``{"ts", "level", "event", ...}``.
Callers pass only fixed event names, codes, counts and identifiers the log itself produced.
Nothing here ever receives a credential; :func:`log` additionally refuses keys that look like
one, so a careless call site fails loudly in tests instead of leaking in production.
"""

from __future__ import annotations

import json
import sys
import threading
from collections import Counter
from datetime import UTC, datetime
from typing import Any, TextIO

__all__ = ["METRICS", "Metrics", "log", "set_stream"]

_FORBIDDEN_KEY_PARTS = ("token", "secret", "password", "api_key", "authorization")

_stream: TextIO = sys.stderr
_lock = threading.Lock()


def set_stream(stream: TextIO) -> None:
    """Redirect log output (tests capture it)."""
    global _stream
    _stream = stream


def log(event: str, level: str = "info", **fields: Any) -> None:
    """Write one structured log line. Never raises on a broken stream."""
    for key in fields:
        lowered = key.lower()
        if any(part in lowered for part in _FORBIDDEN_KEY_PARTS):
            raise ValueError(f"refusing to log a credential-shaped field: {key!r}")
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "level": level,
        "event": event,
        **fields,
    }
    line = json.dumps(record, default=str, ensure_ascii=False, sort_keys=False)
    with _lock:
        try:
            _stream.write(line + "\n")
            _stream.flush()
        except (OSError, ValueError):  # a closed stream must not take the loop down
            pass


class Metrics:
    """Thread-safe named counters. Emitted as a log line; no exporter dependency."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()
        self._lock = threading.Lock()

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[name] += amount

    def get(self, name: str) -> int:
        with self._lock:
            return self._counts[name]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


METRICS = Metrics()
