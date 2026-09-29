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
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from typing import Any

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


FUTURE_SKEW_S = 60


class ConsoleAuth:
    def __init__(
        self,
        token: SecretStr | None,
        *,
        demo: bool,
        base_path: str = "",
        max_age_s: int = SESSION_MAX_AGE_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
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
        message = f"approved-console-session-v2|{issued_at}".encode()
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

    def is_authenticated(self, request: Request) -> bool:
        if self._token is None:
            return True  # demo mode without a token: open, and every page says so
        cookie = request.cookies.get(SESSION_COOKIE, "")
        issued_text, _, mac = cookie.partition(".")
        if not issued_text.isdigit() or len(issued_text) > 12 or not mac:
            return False
        issued = int(issued_text)
        if not hmac.compare_digest(mac.encode(), self._mac(issued).encode()):
            return False
        age = self._clock() - issued
        return -FUTURE_SKEW_S <= age <= self.max_age_s

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
