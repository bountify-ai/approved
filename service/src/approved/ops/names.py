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

__all__ = [
    "ProtectedName",
    "identities",
    "is_protected",
    "protected_ids",
    "refuse_any_protected",
    "refuse_protected",
    "resolve_target",
]

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


_IDENTITY_KEYS = ("name", "agentName", "agent_name", "id", "agentId", "agent_id")


def identities(value: Any, _depth: int = 0) -> list[str]:
    """Every name or id anywhere in a ``maritime --json`` answer, however nested (``agent``,
    ``result``, ``created`` lists, ...)."""
    found: list[str] = []
    if _depth > 8:
        return found
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _IDENTITY_KEYS and isinstance(item, str) and item.strip():
                found.append(item)
            elif isinstance(item, dict | list):
                found += identities(item, _depth + 1)
    elif isinstance(value, list):
        for item in value:
            found += identities(item, _depth + 1)
    return found


def refuse_any_protected(answer: Any, *, what: str) -> list[str]:
    """Refuse if ANY name or id in ``answer`` is protected, and fail CLOSED when there is none:
    a target this CLI cannot identify is a target it will not touch."""
    found = identities(answer)
    if not found:
        raise ProtectedName(f"refusing: maritime's answer for {what} names no agent or id")
    for value in found:
        refuse_protected(value)
    return found


def resolve_target(maritime: Maritime, name: str) -> dict[str, Any]:
    """Resolve ``name`` (a name or an id) through ``maritime --json status`` and refuse if
    anything it resolves to is protected, by name or id, at any depth; or if it resolves to
    nothing identifiable. The typed name is checked first."""
    refuse_protected(name)
    agent = maritime.status(name.strip())
    refuse_any_protected(agent, what=name.strip())
    return agent
