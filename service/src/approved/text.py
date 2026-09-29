"""Untrusted-text handling: redaction and one-line sanitisation.

Two kinds of text in this service come from parties nobody vouches for: the agent's own words
inside an ``approval.requested`` record (its summary or command), and the reviewer model's
reply. Both are treated the way approval.md's gloss treats model text
(approval-md ``src/cli/gloss.ts``: "collapsed to a single line, capped ... and goes through the
same escapeHtml every other claimed value does").
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any

__all__ = ["MAX_ADVISORY_CHARS", "one_line", "redact", "sanitize_for_telegram"]

#: Hard cap on any model-derived text that reaches a channel.
MAX_ADVISORY_CHARS = 300

# Token-shaped material: bearer values, 40+ hex runs (digests and hex tokens), common key
# prefixes, and Telegram bot tokens (<digits>:<35 chars>). Ported from judgy
# weave_observability._REDACTIONS and widened for the credentials this service handles.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-~+/=]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "[REDACTED]"),
    (
        re.compile(r"\b(sk|pk|rk|wandb|api|ghp|gho|ghs|xox[abp])[-_][A-Za-z0-9_\-]{16,}\b"),
        "[REDACTED]",
    ),
    (re.compile(r"\b[0-9a-fA-F]{40,}\b"), "[REDACTED]"),
)

_WHITESPACE = re.compile(r"\s+")
# C0/C1 controls and the Unicode bidi/zero-width format characters that can reorder or hide
# text on a phone screen.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def redact(value: Any) -> Any:
    """Strip token-shaped material from strings, mappings and sequences."""
    if isinstance(value, str):
        out = value
        for pattern, replacement in _REDACTIONS:
            out = pattern.sub(replacement, out)
        return out
    if isinstance(value, Mapping):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(redact(v) for v in value)
    return value


def one_line(text: str, limit: int = MAX_ADVISORY_CHARS) -> str:
    """Collapse to a single line of printable text, capped at ``limit`` characters."""
    flat = _CONTROL.sub(" ", str(text))
    flat = _WHITESPACE.sub(" ", flat).strip()
    if len(flat) > limit:
        flat = flat[: max(0, limit - 1)].rstrip() + "…"
    return flat


def sanitize_for_telegram(text: str, limit: int = MAX_ADVISORY_CHARS) -> str:
    """One line, capped, redacted, then HTML-escaped for ``parse_mode=HTML``.

    The cap applies before escaping, so the visible text is at most ``limit`` characters and
    no escape sequence is ever cut in half.
    """
    return html.escape(one_line(redact(text), limit), quote=False)
