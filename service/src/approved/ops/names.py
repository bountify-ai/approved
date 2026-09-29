"""Machines this CLI must never touch.

The dogfood tenant and the HOSTED-16 experiment machines are live and hold evidence
(approval-md-hosted doc 02 section 4.6, "Machines retained"). Any name matching one of these
is refused before a single ``maritime`` call is made.
"""

from __future__ import annotations

import re

__all__ = ["ProtectedName", "is_protected", "refuse_protected"]

_PROTECTED = (
    re.compile(r"^approval-dogfood$"),
    re.compile(r"^approval-hermes"),
    re.compile(r"^approval-x16-"),
    re.compile(r"dogfood", re.IGNORECASE),
)


class ProtectedName(ValueError):
    pass


def is_protected(name: str) -> bool:
    return any(p.search(name) for p in _PROTECTED)


def refuse_protected(name: str) -> None:
    if is_protected(name):
        raise ProtectedName(
            f"refusing {name!r}: the dogfood tenant and approval-hermes* / approval-x16-* "
            "machines are live and never touched by this CLI"
        )
