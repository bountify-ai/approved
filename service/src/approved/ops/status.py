"""``approved status``: machine state, public health, and ``approval log verify`` (counts and
head only; no record, payload or path is printed)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from ..console.bundle import validate_tenant
from .maritime import Maritime, MaritimeError, agent_id
from .names import refuse_protected, resolve_target
from .provision import public_url_of

__all__ = ["status"]


def status(
    maritime: Maritime,
    tenant: str,
    *,
    http: httpx.Client | None = None,
    out: Callable[[str], None] = print,
    allow_target: str | None = None,
) -> dict[str, Any]:
    """``allow_target`` names the daemon machine exactly (a protected machine such as the
    dogfood daemon is not called ``<tenant>-daemon``); ``tenant`` then only names the store."""
    tenant = validate_tenant(tenant)
    if allow_target is None:
        daemon = f"{tenant}-daemon"
        refuse_protected(tenant)
    else:
        daemon = allow_target
        out(f"warning: --allow-target lets this read-only status reach {allow_target!r} only")
    report: dict[str, Any] = {"daemon": daemon}

    agent = maritime.find(daemon)
    if agent is None:
        if allow_target is None:
            refuse_protected(daemon)
        out(f"{daemon}: not found")
        report["machine"] = "missing"
        return report
    detail = resolve_target(maritime, daemon, allow=allow_target)
    state = next(
        (detail.get(k) for k in ("status", "state") if isinstance(detail.get(k), str)), "unknown"
    )
    report["machine"] = state
    out(f"{daemon}: {state} ({agent_id(agent) or '?'})")

    base = public_url_of({**agent, **detail})
    client = http or httpx.Client(timeout=10.0, follow_redirects=False)
    try:
        code = client.get(f"{base}/health").status_code
    except httpx.HTTPError as exc:
        code = f"unreachable ({type(exc).__name__})"
    report["health"] = code
    out(f"public /health: {code}")

    try:
        result = maritime.exec(
            daemon, f'cd /data/{tenant} && node "$APPROVAL_CLI" log verify --json\n'
        )
        body = json.loads(result.stdout)
        head = body.get("head") or {}
        report["verify"] = {
            "status": body.get("status"),
            "records": body.get("records"),
            "head_seq": head.get("seq"),
            "head_hash": str(head.get("hash", ""))[:12],
        }
        v = report["verify"]
        out(
            f"log verify: {v['status']}, {v['records']} records, head seq {v['head_seq']} "
            f"({v['head_hash']}...)"
        )
    except (MaritimeError, ValueError, AttributeError) as exc:
        report["verify"] = {"status": "unavailable"}
        out(f"log verify: unavailable ({type(exc).__name__})")
    return report
