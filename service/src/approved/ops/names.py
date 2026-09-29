"""Machines this CLI must never touch.

The dogfood tenant and the HOSTED-16 experiment machines are live and hold evidence
(approval-md-hosted doc 02 section 4.6, "Machines retained"). Any name matching one of these
is refused before a single ``maritime`` call is made.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .maritime import Maritime

__all__ = ["ProtectedName", "is_protected", "protected_ids", "refuse_protected", "resolve_target"]

# Matched against the casefolded, stripped name.
_PROTECTED = (
    re.compile(r"^approval-dogfood$"),
    re.compile(r"^approval-hermes"),
    re.compile(r"^approval-x16-"),
    re.compile(r"dogfood"),
)


class ProtectedName(ValueError):
    pass


def _norm(name: str) -> str:
    return name.strip().casefold()


def protected_ids() -> set[str]:
    """Agent ids known to be protected machines (``APPROVED_PROTECTED_AGENT_IDS``, comma list)."""
    raw = os.environ.get("APPROVED_PROTECTED_AGENT_IDS", "")
    return {_norm(x) for x in raw.split(",") if x.strip()}


def is_protected(name: str) -> bool:
    value = _norm(name)
    return any(p.search(value) for p in _PROTECTED) or value in protected_ids()


def refuse_protected(name: str) -> None:
    if is_protected(name):
        raise ProtectedName(
            f"refusing {name.strip()!r}: the dogfood tenant and approval-hermes* / "
            "approval-x16-* machines are live and never touched by this CLI"
        )


def resolve_target(maritime: Maritime, name: str) -> dict[str, Any]:
    """Resolve ``name`` (a name or an id) through ``maritime --json status`` and refuse if what
    it RESOLVES to is protected, by name or by id. The typed name is checked first."""
    refuse_protected(name)
    agent = maritime.status(name.strip())
    for key in ("name", "agentName", "id", "agentId"):
        value = agent.get(key)
        if isinstance(value, str):
            refuse_protected(value)
    return agent
