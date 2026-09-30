"""Durable, process-safe reservations for actual paid inference requests.

The ledger is initialized explicitly by the owning runtime. A missing ledger after
initialization is an error, never an invitation to reset the spend counter.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TypedDict, cast

_VERSION = 1
_MARKER = "inference-budget-v1\n"


class _Ledger(TypedDict):
    version: int
    limit: int
    hourly_limit: int
    used: int
    recent: list[float]


class BudgetError(RuntimeError):
    """A safe, value-free refusal to make an inference request."""


def _paths(path: Path) -> tuple[Path, Path]:
    return path.with_name(path.name + ".lock"), path.with_name(path.name + ".initialized")


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock, _ = _paths(path)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    except OSError as exc:
        raise BudgetError("inference budget unavailable") from exc


def _write(path: Path, value: str) -> None:
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # fdopen owns fd once constructed; the temporary file is removed below.
            raise
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise BudgetError("inference budget unavailable") from exc
    finally:
        with suppress(OSError):
            temp.unlink(missing_ok=True)


def _valid(data: object, limit: int, hourly_limit: int) -> _Ledger:
    expected = {"version", "limit", "hourly_limit", "used", "recent"}
    if not isinstance(data, dict) or set(data) != expected:
        raise BudgetError("inference budget unavailable")
    if (
        type(data["version"]) is not int
        or data["version"] != _VERSION
        or type(data["limit"]) is not int
        or data["limit"] != limit
        or type(data["hourly_limit"]) is not int
        or data["hourly_limit"] != hourly_limit
        or type(data["used"]) is not int
        or not 0 <= data["used"] <= limit
        or not isinstance(data["recent"], list)
        or len(data["recent"]) > limit
    ):
        raise BudgetError("inference budget unavailable")
    for stamp in data["recent"]:
        if not isinstance(stamp, int | float) or isinstance(stamp, bool) or not 0 <= stamp < 1e12:
            raise BudgetError("inference budget unavailable")
    if len(data["recent"]) > data["used"]:
        raise BudgetError("inference budget unavailable")
    return cast(_Ledger, data)


def _load(path: Path, limit: int, hourly_limit: int) -> _Ledger:
    _, marker = _paths(path)
    try:
        if marker.read_text(encoding="utf-8") != _MARKER:
            raise BudgetError("inference budget unavailable")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise BudgetError("inference budget unavailable") from exc
    return _valid(data, limit, hourly_limit)


def initialize_budget(path: Path, limit: int, *, hourly_limit: int) -> None:
    """Create one fresh ledger, or validate the existing one without resetting it."""
    if limit < 1 or hourly_limit < 1 or hourly_limit > limit:
        raise BudgetError("inference budget configuration invalid")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BudgetError("inference budget unavailable") from exc
    with _locked(path):
        _, marker = _paths(path)
        if marker.exists():
            _load(path, limit, hourly_limit)
            return
        if path.exists():
            # A crash between ledger and marker writes can be recovered without reset.
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as exc:
                raise BudgetError("inference budget unavailable") from exc
            _valid(data, limit, hourly_limit)
        else:
            data: _Ledger = {
                "version": _VERSION,
                "limit": limit,
                "hourly_limit": hourly_limit,
                "used": 0,
                "recent": [],
            }
            _write(path, json.dumps(data, separators=(",", ":")))
        _write(marker, _MARKER)


class InferenceCallBudget:
    """One reservation immediately before each SDK request, never refunded."""

    def __init__(self, path: Path, limit: int, *, hourly_limit: int) -> None:
        if limit < 1 or hourly_limit < 1 or hourly_limit > limit:
            raise BudgetError("inference budget configuration invalid")
        self.path = path
        self.limit = limit
        self.hourly_limit = hourly_limit

    def reserve(self) -> None:
        with _locked(self.path):
            data = _load(self.path, self.limit, self.hourly_limit)
            now = time.time()
            recent = [stamp for stamp in data["recent"] if stamp > now - 3600]
            if data["used"] >= self.limit or len(recent) >= self.hourly_limit:
                raise BudgetError("inference budget exhausted")
            data["used"] += 1
            data["recent"] = [*recent, now]
            _write(self.path, json.dumps(data, separators=(",", ":")))

    def check(self, calls: int = 1) -> str | None:
        """Read-only admission hint. ``reserve`` remains the authoritative spend gate."""
        if calls < 1:
            raise ValueError("calls must be positive")
        try:
            with _locked(self.path):
                data = _load(self.path, self.limit, self.hourly_limit)
                now = time.time()
                recent = [stamp for stamp in data["recent"] if stamp > now - 3600]
                if data["used"] + calls > self.limit or len(recent) + calls > self.hourly_limit:
                    return "inference-budget-exhausted"
        except BudgetError:
            return "inference-budget-unavailable"
        return None
