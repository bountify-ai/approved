"""Request guards for the console's POST routes: body size and a per-IP rate limit.

Pure ASGI, so the size check runs BEFORE any handler or form/JSON parser reads the body: a
declared ``Content-Length`` over the limit is refused at once, and a body without one (or one
that lies) is read in chunks and refused as soon as it passes the limit.

The rate limit is a small in-memory sliding window per client IP on the public POST routes
(sign-in and the judge preview). Behind a proxy the client address may be the proxy's, in which
case the limit is shared by everyone behind it: coarse, but it still bounds a flood.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

__all__ = ["BODY_LIMITS", "RATE_LIMITED", "RateLimiter", "RequestGuard", "client_ip"]

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

BODY_LIMITS: dict[str, int] = {
    "/login": 8 * 1024,
    "/logout": 8 * 1024,
    "/connect": 8 * 1024,
    "/api/preview": 16 * 1024,
}
#: Rate-limited by client on every request. /login is limited on FAILED attempts only (in the
#: handler): a request carrying the valid 256-bit token is never throttled, so a flood from a
#: shared address can never lock the operator out.
RATE_LIMITED = frozenset({"/logout", "/api/preview"})


def client_ip(scope: Scope, trusted_proxy_hops: int = 0) -> str:
    """The client address: the socket peer, or with ``trusted_proxy_hops`` >= 1 the entry that
    many hops from the right of ``X-Forwarded-For`` (the address our own proxy saw). Entries
    further left are client-supplied and never trusted."""
    if trusted_proxy_hops >= 1:
        headers = dict(scope.get("headers") or [])
        forwarded = headers.get(b"x-forwarded-for")
        if forwarded:
            hops = [h.strip() for h in forwarded.decode("latin-1").split(",") if h.strip()]
            if len(hops) >= trusted_proxy_hops:
                return hops[-trusted_proxy_hops]
    return str((scope.get("client") or ("unknown", 0))[0])


class RateLimiter:
    def __init__(
        self,
        limit: int = 30,
        window_s: float = 60.0,
        *,
        max_clients: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self.max_clients = max_clients
        self._clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, client: str) -> bool:
        now = self._clock()
        with self._lock:
            hits = self._hits.pop(client, None) or deque()
            while hits and now - hits[0] > self.window_s:
                hits.popleft()
            allowed = len(hits) < self.limit
            if allowed:
                hits.append(now)
            self._hits[client] = hits
            while len(self._hits) > self.max_clients:
                self._hits.popitem(last=False)
            return allowed


async def _reply(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RequestGuard:
    def __init__(
        self,
        app: ASGIApp,
        limits: dict[str, int] | None = None,
        rate: RateLimiter | None = None,
        trusted_proxy_hops: int = 0,
    ) -> None:
        self.app = app
        self.limits = BODY_LIMITS if limits is None else limits
        self.rate = rate or RateLimiter()
        self.trusted_proxy_hops = trusted_proxy_hops

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope.get("type") != "http" or scope.get("method") != "POST" or path not in self.limits:
            await self.app(scope, receive, send)
            return
        client = client_ip(scope, self.trusted_proxy_hops)
        if path in RATE_LIMITED and not self.rate.allow(client):
            await _reply(send, 429, "too many requests; slow down")
            return
        limit = self.limits[path]
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            await _reply(send, 413, f"request body over {limit} bytes")
            return
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = bool(message.get("more_body", False))
            if len(body) > limit:
                await _reply(send, 413, f"request body over {limit} bytes")
                return
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
