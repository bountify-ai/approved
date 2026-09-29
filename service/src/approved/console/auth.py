"""Console authentication and CSRF.

* **Operator token.** One shared token (``CONSOLE_TOKEN`` / ``CONSOLE_TOKEN_FILE``). ``/login``
  checks it in constant time and sets a session cookie whose value is an HMAC derived from the
  token, never the token itself, so a leaked cookie does not reveal the credential and rotating
  the token logs everyone out. ``HttpOnly``, ``SameSite=Strict``, ``Secure`` on https.
* **No token configured.** Only allowed in demo mode (``APPROVED_DEMO=1``); every page is then
  open and says so. ``serve`` refuses to start otherwise.
* **CSRF.** Double-submit: a random ``approved_csrf`` cookie (``SameSite=Strict``) must match
  the ``csrf`` form field or the ``X-CSRF-Token`` header on every state-changing request.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Request
from pydantic import SecretStr

__all__ = [
    "CSRF_COOKIE",
    "SESSION_COOKIE",
    "ConsoleAuth",
]

SESSION_COOKIE = "approved_console"
CSRF_COOKIE = "approved_csrf"
SESSION_MAX_AGE_S = 12 * 3600


class ConsoleAuth:
    def __init__(self, token: SecretStr | None, *, demo: bool) -> None:
        self._token = token
        self.demo = demo

    @property
    def enabled(self) -> bool:
        return self._token is not None

    def _session_value(self) -> str:
        assert self._token is not None
        key = self._token.get_secret_value().encode()
        return hmac.new(key, b"approved-console-session-v1", hashlib.sha256).hexdigest()

    def check_token(self, candidate: str) -> bool:
        if self._token is None:
            return False
        return hmac.compare_digest(candidate.encode(), self._token.get_secret_value().encode())

    def session_cookie(self) -> str:
        return self._session_value()

    def is_authenticated(self, request: Request) -> bool:
        if self._token is None:
            return True  # demo mode without a token: open, and every page says so
        cookie = request.cookies.get(SESSION_COOKIE, "")
        return hmac.compare_digest(cookie.encode(), self._session_value().encode())

    # ------------------------------------------------------------------ CSRF

    @staticmethod
    def csrf_for(request: Request) -> tuple[str, bool]:
        """The request's CSRF token, and whether it is new (and must be set as a cookie)."""
        existing = request.cookies.get(CSRF_COOKIE)
        if existing and 20 <= len(existing) <= 100:
            return existing, False
        return secrets.token_urlsafe(24), True

    @staticmethod
    def csrf_ok(request: Request, submitted: str | None) -> bool:
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not cookie or not submitted:
            return False
        return hmac.compare_digest(cookie.encode(), submitted.encode())
