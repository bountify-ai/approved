"""Telegram delivery of the advisory line. Bounded, and a failure is logged, never raised.

The message is assembled from fixed text plus three values: the verdict enum (closed
vocabulary), the reviewer's one-line reason (untrusted: collapsed, capped at 300 characters,
redacted and HTML-escaped by :func:`approved.text.sanitize_for_telegram`), and the Weave trace
URL (built by this service from a call id, escaped anyway). ``parse_mode`` is HTML, so escaping
is what keeps model text from becoming markup.

The bot token is part of the Bot API URL. It is never logged: a failure reports the exception
class or the HTTP status and Telegram's numeric ``error_code``, nothing else.
"""

from __future__ import annotations

import html
from typing import Protocol

import httpx
from pydantic import SecretStr

from .logs import METRICS, log
from .reviewer import Verdict
from .text import sanitize_for_telegram

__all__ = [
    "Notifier",
    "NullNotifier",
    "TelegramNotifier",
    "format_advisory",
    "format_chain_break",
]

ADVISORY_PREFIX = "Judge (advisory, AI)"


def format_advisory(verdict: Verdict, trace_url: str | None, action_class: str | None) -> str:
    """``Judge (advisory, AI): NEEDS_HUMAN, <reason>. Trace: <url>`` plus the class line."""
    reason = sanitize_for_telegram(verdict.reason()).rstrip(".")
    trace = html.escape(trace_url, quote=False) if trace_url else "not traced (offline)"
    lines = [f"{ADVISORY_PREFIX}: {verdict.decision.value}, {reason}. Trace: {trace}"]
    if action_class:
        lines.append(f"Request class: {sanitize_for_telegram(action_class, 80)}")
    return "\n".join(lines)


def format_chain_break(at_seq: int, reason: str) -> str:
    return (
        f"{ADVISORY_PREFIX}: halted. The approval log did not continue from where the judge "
        f"left off (chain-break at seq {at_seq}, {sanitize_for_telegram(reason, 64)}). "
        "No further advisories until an operator investigates. Your approvals are unaffected."
    )


class Notifier(Protocol):
    def send(self, text: str) -> bool: ...


class NullNotifier:
    """Telegram not configured: every send is a logged no-op."""

    def send(self, text: str) -> bool:
        METRICS.incr("notify.disabled")
        log("notify.disabled", chars=len(text))
        return False


class TelegramNotifier:
    def __init__(
        self,
        bot_token: SecretStr,
        chat_id: str,
        *,
        api_base: str = "https://api.telegram.org",
        timeout_s: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout_s
        self._client = client or httpx.Client(timeout=timeout_s, follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def send(self, text: str) -> bool:
        url = f"{self._api_base}/bot{self._token.get_secret_value()}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            response = self._client.post(url, json=payload, timeout=self._timeout)
        except httpx.HTTPError as exc:
            return self._failed(error=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - delivery never takes the loop down
            return self._failed(error=type(exc).__name__)
        try:
            body = response.json()
        except ValueError:
            body = None
        ok = isinstance(body, dict) and body.get("ok") is True
        if response.status_code != 200 or not ok:
            code = body.get("error_code") if isinstance(body, dict) else None
            return self._failed(status=response.status_code, tg_error_code=code)
        METRICS.incr("notify.sent")
        log("notify.sent", chars=len(text))
        return True

    @staticmethod
    def _failed(**fields: object) -> bool:
        METRICS.incr("notify.failed")
        log("notify.failed", level="warning", **fields)
        return False
