"""Test doubles: a fake approval.md facade, a fake Telegram Bot API, scripted reviewers.

The fake facade is the enforcement point for the judge's two hardest invariants:

* **Read-only.** It answers ``GET /log/follow`` only. Any other method or route is recorded as
  a violation and answered 418.
* **Tenant credential only.** A request carrying the agent credential, in either the
  ``Authorization`` or the ``X-Approval-Authorization`` header, is a violation.

The ``facade`` fixture in ``conftest.py`` fails the test at teardown if any violation was
recorded, so every worker test enforces both invariants whether or not it asserts them.

Synthetic chains (``build_chain``) are in-memory records for a fake facade, hashed with the
runtime's own scheme (SHA-256 over JCS), because the judge recomputes every hash.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from approved.follow import record_hash
from approved.reviewer import (
    Decision,
    InferenceError,
    Issue,
    IssueCode,
    JudgeRequest,
    Verdict,
)

FIXTURES = Path(__file__).parent / "fixtures" / "facade"
FACADE_URL = "https://facade.test/a/tenant-1"
TENANT_TOKEN = "tenant-credential-0123456789abcdef"
AGENT_TOKEN = "agent-credential-fedcba9876543210"

ALLOWED_ROUTES = {("GET", "/a/tenant-1/log/follow")}


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _digest(record: dict[str, Any]) -> str:
    """The runtime's own scheme (sha256 over JCS without ``hash``), so fake chains verify."""
    return record_hash(record)


def build_chain(
    events: Iterable[dict[str, Any]], *, start_seq: int = 1, start_prev: str | None = None
) -> list[dict[str, Any]]:
    """Link ``events`` into records with contiguous seq, prev and hash."""
    out: list[dict[str, Any]] = []
    prev = start_prev
    for seq, event in enumerate(events, start=start_seq):
        record = {
            "actor": "agent:hermes-test",
            "alg": "sha256/jcs",
            "ts": "2026-09-29T12:00:00.000Z",
            **event,
            "seq": seq,
            "prev": prev,
        }
        record["hash"] = _digest(record)
        out.append(record)
        prev = record["hash"]
    return out


def request_event(
    key: str,
    klass: str = "vcs.push.main",
    summary: str = "git push origin main",
    ts: str = "2026-09-29T12:00:00.000Z",
    **payload: Any,
) -> dict[str, Any]:
    return {
        "event": "approval.requested",
        "action_key": key,
        "task": f"task-{key}",
        "ts": ts,
        "payload": {"class": klass, "summary": summary, "est_cost_usd": "0", **payload},
    }


def task_event(key: str, summary: str) -> dict[str, Any]:
    return {
        "event": "task.registered",
        "task": f"task-{key}",
        "payload": {"actions": [{"idempotency_key": key, "summary": summary}]},
    }


def decision_event(key: str, event: str = "approval.granted", actor: str = "human:carter"):
    return {"event": event, "action_key": key, "task": f"task-{key}", "actor": actor}


@dataclass
class FakeFacade:
    """An in-memory ``approval serve`` answering ``/log/follow`` pages from ``records``."""

    records: list[dict[str, Any]] = field(default_factory=list)
    tenant_token: str = TENANT_TOKEN
    agent_token: str = AGENT_TOKEN
    violations: list[str] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    #: Scripted answers consumed before normal paging: a Response, or an Exception to raise.
    script: list[httpx.Response | Exception] = field(default_factory=list)

    def append(self, *events: dict[str, Any]) -> None:
        prev = self.records[-1]["hash"] if self.records else None
        self.records.extend(build_chain(events, start_seq=len(self.records) + 1, start_prev=prev))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = (request.method, request.url.path)
        auth_values = [
            request.headers.get("authorization", ""),
            request.headers.get("x-approval-authorization", ""),
        ]
        if any(self.agent_token in value for value in auth_values):
            self.violations.append(f"agent credential used on {route}")
        if route not in ALLOWED_ROUTES:
            self.violations.append(f"forbidden route {route}")
            return httpx.Response(418, json={"error": {"code": "test-forbidden-route"}})
        if not any(value == f"Bearer {self.tenant_token}" for value in auth_values):
            return httpx.Response(401, json={"error": {"code": "serve-unauthorized"}})
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if route[0] != "GET":
            return httpx.Response(200, json={"items": []})
        return self._page(request)

    def _page(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        start = int(params.get("from", "0"))
        limit = int(params.get("limit", "200"))
        cursor_hash = params.get("cursor_hash")
        if start > 0 and (
            start > len(self.records) or self.records[start - 1]["hash"] != cursor_hash
        ):
            return httpx.Response(409, json=load_fixture("follow-cursor-mismatch.json")["body"])
        page = self.records[start : start + limit]
        end = start + len(page)
        head_hash = self.records[end - 1]["hash"] if end > 0 else None
        return httpx.Response(
            200,
            json={
                "records": page,
                "cursor": {"seq": end, "hash": head_hash},
                "caught_up": end >= len(self.records),
                "exit_code": 0,
            },
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@dataclass
class ReplayFacade(FakeFacade):
    """Answers every follow with one captured body (real runtime output), unchanged."""

    body: dict[str, Any] = field(default_factory=dict)
    status: int = 200

    def _page(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.status, json=self.body)


@dataclass
class FakeTelegram:
    fail_with: int | None = None
    raise_exc: Exception | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, json={"ok": False, "error_code": self.fail_with})
        self.messages.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.messages)}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    @property
    def texts(self) -> list[str]:
        return [m["text"] for m in self.messages]


def verdict(decision: Decision = Decision.NEEDS_HUMAN, why: str = "Push to main.") -> Verdict:
    issues = [] if decision is Decision.READY else [Issue(code=IssueCode.OTHER, explanation=why)]
    return Verdict(decision=decision, issues=issues, reviewer_version="test")


@dataclass
class ScriptedReviewer:
    """Returns (or raises) the scripted items in order, then repeats the last."""

    items: list[Verdict | BaseException | Callable[[JudgeRequest], Verdict]]
    name: str = "scripted"
    seen: list[JudgeRequest] = field(default_factory=list)

    def review(self, request: JudgeRequest) -> Verdict:
        self.seen.append(request)
        item = self.items[min(len(self.seen) - 1, len(self.items) - 1)]
        if isinstance(item, BaseException):
            raise item
        if callable(item) and not isinstance(item, Verdict):
            return item(request)
        return item


class BlockingReviewer:
    """Blocks until released: stands in for a reviewer that outlives the deadline."""

    name = "blocking"

    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls = 0

    def review(self, request: JudgeRequest) -> Verdict:
        self.calls += 1
        self.release.wait(5)
        raise InferenceError("released after the deadline")


class CallIdTracer:
    """Stands in for Weave: each reviewer call gets a deterministic call id."""

    def __init__(self) -> None:
        self.count = 0

    def run(self, reviewer: Any, request: JudgeRequest) -> tuple[Verdict, str | None]:
        self.count += 1
        return reviewer.review(request), f"call-{request.action_key}"
