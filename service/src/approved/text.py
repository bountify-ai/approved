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

__all__ = [
    "LINK_REMOVED",
    "MAX_ADVISORY_CHARS",
    "one_line",
    "redact",
    "sanitize_for_telegram",
    "strip_links",
]

#: Hard cap on any model-derived text that reaches a channel.
MAX_ADVISORY_CHARS = 300

# Token-shaped material, in the order applied. Started from judgy's
# weave_observability._REDACTIONS and widened for what an agent's command line can carry.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # credentials in a URL: scheme://user:pass@host
    (
        re.compile(r"\b([a-z][a-z0-9+.-]*://)[^\s/@:]+(?::[^\s/@]*)?@", re.IGNORECASE),
        r"\1[REDACTED]@",
    ),
    # HTTP auth schemes
    (re.compile(r"(?i)\b((?:bearer|basic|token)\s+)[A-Za-z0-9._\-~+/=]{8,}"), r"\1[REDACTED]"),
    # JSON Web Tokens
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), "[REDACTED]"),
    # Telegram bot tokens: <bot id>:<35 chars>
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "[REDACTED]"),
    # AWS access key ids
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    # key-prefixed secrets
    (
        re.compile(r"\b(sk|pk|rk|mk|wandb|api|ghp|gho|ghs|xox[abp])[-_][A-Za-z0-9_\-]{16,}\b"),
        "[REDACTED]",
    ),
    # password flags: --password=x, --password x, -pSECRET (mysql style; "-p dir" is untouched)
    (re.compile(r"(--pass(?:word)?[= ])\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?<!\S)-p(?=[^\s-])\S+"), "-p[REDACTED]"),
    # long hex runs (digests and hex tokens), 32 or more
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[REDACTED]"),
)

# Long base64-ish runs are secrets only when they mix character classes, so ordinary long
# words and lowercase paths survive.
_B64 = re.compile(r"[A-Za-z0-9+/_-]{32,}={0,2}")


def _b64(match: re.Match[str]) -> str:
    run = match.group(0)
    classes = sum(
        (
            any(c.isupper() for c in run),
            any(c.islower() for c in run),
            any(c.isdigit() for c in run),
        )
    )
    return "[REDACTED]" if classes == 3 else run


# URLs, e-mail addresses, anything domain-like, @mentions and /commands in model text: Telegram
# makes all of them tappable. Order matters (URLs and e-mails before bare domains).
_LINKS = (
    re.compile(r"\b(?:https?|ftp|tg)://\S+", re.IGNORECASE),
    re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),
    re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.[A-Za-z][\w-]*\b(?:/\S*)?"),
    re.compile(r"(?<![\w@])@[A-Za-z0-9_]{2,}"),
    re.compile(r"(?<![\w/<.~])/[A-Za-z][A-Za-z0-9_]{0,31}\b(?![/.])"),
)
LINK_REMOVED = "[link removed]"


def strip_links(text: str) -> str:
    """Replace URLs, bare domains and /commands with a marker (model text only)."""
    for pattern in _LINKS:
        text = pattern.sub(LINK_REMOVED, text)
    return text


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
        return _B64.sub(_b64, out)
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


def sanitize_for_telegram(
    text: str, limit: int = MAX_ADVISORY_CHARS, *, links: bool = False
) -> str:
    """One line, capped, redacted, links stripped, then HTML-escaped for ``parse_mode=HTML``.

    The cap applies before escaping, so the visible text is at most ``limit`` characters and
    no escape sequence is ever cut in half.
    """
    clean = redact(text)
    if not links:
        clean = strip_links(clean)
    return html.escape(one_line(clean, limit), quote=False)
