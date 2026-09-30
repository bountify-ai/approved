"""Console authentication and CSRF.

* **Operator token.** One shared token (``CONSOLE_TOKEN`` / ``CONSOLE_TOKEN_FILE``). ``/login``
  checks it in constant time and sets a session cookie ``<issued_at>.<HMAC(token, issued_at)>``:
  never the token itself, expired server-side after 12 hours, and invalidated for everyone when
  the token rotates. ``HttpOnly``, ``SameSite=Strict``, ``Secure`` (except in demo mode), and
  ``Path`` scoped to the console's public prefix (``PUBLIC_BASE_PATH``).
* **Shared origin.** On Maritime every public agent shares ``https://api.maritime.sh``; the
  cookie ``Path`` keeps this console's cookies off other agents' paths, but cookies are not
  an origin boundary: another agent's pages on the same origin can still send requests with
  them. SameSite does not help within one site, so the CSRF token (below) is what protects
  state-changing requests, and a console that matters belongs on its own origin.
* **No token configured.** Only allowed in demo mode (``APPROVED_DEMO=1``); every page is then
  open and says so. ``serve`` refuses to start otherwise.
* **CSRF.** Double-submit: a random ``approved_csrf`` cookie (``SameSite=Strict``) must match
  the ``csrf`` form field or the ``X-CSRF-Token`` header on every state-changing request.
* **Header sessions (no Cookie request header).** Maritime's public proxy drops the ``Cookie``
  request header, so a browser there can never present the cookie. The console's own script
  (``static/session.js``) then keeps the same server-issued session value in
  ``sessionStorage`` and sends it as ``X-Approved-Session``. A request carrying that header is
  judged by the header alone (any cookie is ignored), and its CSRF token is
  ``HMAC(token, session)`` rather than a cookie: a cross-origin page cannot send the custom
  header without a CORS preflight, which this app never answers, and cannot mint the token
  without the session. The sign-in that issues such a session is marked by
  ``X-Approved-Login`` and is taken only when the CSRF cookie did not arrive; when it did, the
  ordinary cookie flow (and its double-submit check) applies.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Request
from pydantic import SecretStr

__all__ = [
    "CSRF_COOKIE",
    "LOGIN_HEADER",
    "SESSION_COOKIE",
    "SESSION_HEADER",
    "ConsoleAuth",
]

SESSION_COOKIE = "approved_console"
CSRF_COOKIE = "approved_csrf"
SESSION_MAX_AGE_S = 12 * 3600
#: The session value, sent by ``static/session.js`` where the Cookie request header is dropped.
SESSION_HEADER = "x-approved-session"
#: Marks a sign-in POST made by ``static/session.js``; the answer is JSON instead of a redirect.
LOGIN_HEADER = "x-approved-login"
#: Longer than any value :meth:`ConsoleAuth.session_cookie` issues; anything longer is refused
#: before it is parsed or MAC-compared.
MAX_SESSION_CHARS = 128
_SESSION_FORMAT = re.compile(r"(0|[1-9][0-9]{0,11})\.([0-9a-f]{64})", re.ASCII)


FUTURE_SKEW_S = 60


class SessionEpoch:
    """A counter in ``STATE_DIR/console-epoch`` mixed into every session MAC. Signing out bumps
    it, which revokes every session at once (there is one shared operator token, so there is
    no per-user session to revoke)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def get(self) -> int:
        if self.path is None:
            return 0
        try:
            text = self.path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            return 0
        return int(text) if text.isdigit() else 0

    def bump(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, str(self.get() + 1).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)


class ConsoleAuth:
    def __init__(
        self,
        token: SecretStr | None,
        *,
        demo: bool,
        base_path: str = "",
        max_age_s: int = SESSION_MAX_AGE_S,
        clock: Callable[[], float] = time.time,
        epoch: SessionEpoch | None = None,
    ) -> None:
        self.epoch = epoch or SessionEpoch(None)
        self._token = token
        self.demo = demo
        self.base_path = base_path
        self.max_age_s = max_age_s
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._token is not None

    def cookie_options(self) -> dict[str, Any]:
        """Cookie attributes for a shared origin (Maritime serves every public agent under
        ``https://api.maritime.sh/a/<id>``): scoped to this console's own path prefix,
        ``Secure`` unless demo mode, ``HttpOnly``, ``SameSite=Strict``."""
        return {
            "httponly": True,
            "samesite": "strict",
            "secure": not self.demo,
            "path": self.base_path or "/",
        }

    def _mac(self, issued_at: int) -> str:
        assert self._token is not None
        key = self._token.get_secret_value().encode()
        message = f"approved-console-session-v3|{self.epoch.get()}|{issued_at}".encode()
        return hmac.new(key, message, hashlib.sha256).hexdigest()

    def check_token(self, candidate: str) -> bool:
        if self._token is None:
            return False
        return hmac.compare_digest(candidate.encode(), self._token.get_secret_value().encode())

    def session_cookie(self) -> str:
        """``<issued_at>.<HMAC(token, issued_at)>``: expires server-side after ``max_age_s``,
        and a rotated CONSOLE_TOKEN invalidates every session at once."""
        issued = int(self._clock())
        return f"{issued}.{self._mac(issued)}"

    def revoke_all(self) -> None:
        """Sign-out: bump the epoch, so every session cookie issued so far stops verifying."""
        if self._token is not None:
            self.epoch.bump()

    def verify_session(self, value: str) -> bool:
        """True for a session value this token issued, under the current epoch, not expired."""
        if self._token is None or len(value) > MAX_SESSION_CHARS:
            return False
        # One accepted spelling per session: ASCII digits without a sign or leading zero, a
        # dot, and the MAC exactly as hexdigest() writes it. ``str.isdigit`` would admit
        # superscripts (int() then raises) and other scripts' digits (int() parses them).
        match = _SESSION_FORMAT.fullmatch(value)
        if match is None:
            return False
        issued = int(match.group(1))
        if not hmac.compare_digest(match.group(2).encode(), self._mac(issued).encode()):
            return False
        age = self._clock() - issued
        return -FUTURE_SKEW_S <= age <= self.max_age_s

    def header_flow(self, request: Request) -> bool:
        """The request presents its session in ``X-Approved-Session`` (any value, even an empty
        or forged one). Such a request is judged by that header alone. Never with no token
        configured: demo mode without a token has no sessions at all."""
        return self._token is not None and SESSION_HEADER in request.headers

    def header_session(self, request: Request) -> str | None:
        """The verified session value from ``X-Approved-Session``, or ``None``."""
        if not self.header_flow(request):
            return None
        value = request.headers.get(SESSION_HEADER, "")
        return value if self.verify_session(value) else None

    def is_authenticated(self, request: Request) -> bool:
        if self._token is None:
            return True  # demo mode without a token: open, and every page says so
        if self.header_flow(request):
            return self.header_session(request) is not None
        return self.verify_session(request.cookies.get(SESSION_COOKIE, ""))

    def flow(self, request: Request) -> str:
        """How this request is signed in: ``open`` (demo, no token), ``header``, ``cookie`` or
        ``none``. Rendered into each page for ``static/session.js``; not a secret."""
        if self._token is None:
            return "open"
        if self.header_flow(request):
            return "header" if self.header_session(request) is not None else "none"
        return "cookie" if self.verify_session(request.cookies.get(SESSION_COOKIE, "")) else "none"

    # ------------------------------------------------------------------ CSRF

    def session_csrf(self, session: str) -> str:
        """The header flow's CSRF token: bound to one session value by HMAC under the console
        token, so a token from another session (or a cookie's random token) never matches."""
        assert self._token is not None
        key = self._token.get_secret_value().encode()
        message = f"approved-console-csrf-v1|{session}".encode()
        return hmac.new(key, message, hashlib.sha256).hexdigest()

    def csrf_for(self, request: Request) -> tuple[str, bool]:
        """The request's CSRF token, and whether it is new (and must be set as a cookie). A
        request with a valid header session gets its session-bound token, never a cookie."""
        session = self.header_session(request)
        if session is not None:
            return self.session_csrf(session), False
        existing = request.cookies.get(CSRF_COOKIE)
        if existing and 20 <= len(existing) <= 100:
            return existing, False
        return secrets.token_urlsafe(24), True

    def csrf_ok(self, request: Request, submitted: str | None) -> bool:
        """Header flow: a valid header session and its HMAC-bound token. Otherwise the cookie
        double-submit check, unchanged."""
        if self.header_flow(request):
            session = self.header_session(request)
            if session is None or not submitted:
                return False
            return hmac.compare_digest(submitted.encode(), self.session_csrf(session).encode())
        return self.cookie_csrf_ok(request, submitted)

    @staticmethod
    def cookie_csrf_ok(request: Request, submitted: str | None) -> bool:
        """Double-submit: the ``approved_csrf`` cookie equals the submitted token."""
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not cookie or not submitted:
            return False
        return hmac.compare_digest(cookie.encode(), submitted.encode())
