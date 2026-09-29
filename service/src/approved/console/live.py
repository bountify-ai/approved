"""The live tenant view's data: read from the judge's state file, never from the facade.

The worker writes ``judge-state.json`` atomically (``state.py``), so reading it from the web
thread needs no lock and never sees half a write. Everything here is a pure function of that
file plus the worker's in-process :class:`~approved.worker.FollowStatus` snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..feedback import agreement, human_decision
from ..state import STATE_FILENAME, JudgeState

__all__ = ["LiveView", "OpenRequest", "RecentDecision", "counters", "load_view"]

RECENT_DECISIONS = 20


@dataclass(frozen=True)
class OpenRequest:
    action_key: str
    seq: int
    action_class: str | None
    summary: str | None
    requested_ts: str | None
    verdict: str | None
    judge_status: str
    trace_url: str | None


@dataclass(frozen=True)
class RecentDecision:
    action_key: str
    seq: int
    action_class: str | None
    human: str | None
    actor: str
    ts: str
    verdict: str | None
    label: str | None
    trace_url: str | None
    feedback: str
    advisory: str


@dataclass(frozen=True)
class LiveView:
    readable: bool
    cursor_seq: int
    chain_break: dict[str, Any] | None
    open_requests: list[OpenRequest]
    recent: list[RecentDecision]
    counters: dict[str, Any]


def counters(state: JudgeState) -> dict[str, Any]:
    """Running agreement, false_ready and escalation, over decisions the judge had a verdict on.

    Same definitions as ``evaluate.py``: agreement_rate = agree / (agree + disagree);
    false_ready_rate = READY-then-rejected / rejected; escalation_rate = NEEDS_HUMAN / verdicts.
    """
    agree = disagree = escalated = false_ready = rejected = 0
    for key, decision in state.decisions.items():
        judged = state.judged.get(key)
        verdict = judged.decision if judged else None
        human = human_decision(decision.event)
        label = agreement(verdict, human)
        agree += label == "agree"
        disagree += label == "disagree"
        escalated += label == "escalated"
        if verdict is not None and human == "rejected":
            rejected += 1
            false_ready += verdict == "READY"
    verdicts = [j.decision for j in state.judged.values() if j.decision]
    needs_human = sum(1 for v in verdicts if v == "NEEDS_HUMAN")

    def rate(n: int, d: int) -> float | None:
        return round(n / d, 4) if d else None

    return {
        "verdicts": len(verdicts),
        "agree": agree,
        "disagree": disagree,
        "escalated": escalated,
        "agreement_rate": rate(agree, agree + disagree),
        "false_ready": false_ready,
        "rejected": rejected,
        "false_ready_rate": rate(false_ready, rejected),
        "escalation_rate": rate(needs_human, len(verdicts)),
        "absent": sum(1 for j in state.judged.values() if j.status == "absent"),
        "silence": silence(state),
    }


def silence(state: JudgeState) -> dict[str, int]:
    """Why the judge said nothing, by reason: timeouts, parse and inference failures, an open
    breaker, requests skipped as stale or already decided, and requests never seen."""
    out: dict[str, int] = {}
    for entry in state.judged.values():
        if entry.status in ("absent", "skipped"):
            key = entry.reason or entry.status
        elif entry.status == "silent":
            key = "delivery-failed"
        else:
            continue
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def load_view(state_dir: Path) -> LiveView:
    path = Path(state_dir) / STATE_FILENAME
    try:
        state = JudgeState.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError):
        return LiveView(False, 0, None, [], [], counters(JudgeState()))

    open_requests = [
        OpenRequest(
            action_key=key,
            seq=entry.seq,
            action_class=entry.action_class,
            summary=entry.summary,
            requested_ts=entry.requested_ts,
            verdict=entry.decision,
            judge_status=entry.status,
            trace_url=entry.trace_url,
        )
        for key, entry in state.judged.items()
        if key not in state.decisions and entry.status != "skipped"
    ]
    open_requests.sort(key=lambda r: r.seq, reverse=True)

    recent: list[RecentDecision] = []
    for key, decision in state.decisions.items():
        judged = state.judged.get(key)
        human = human_decision(decision.event)
        verdict = judged.decision if judged else None
        recent.append(
            RecentDecision(
                action_key=key,
                seq=decision.seq,
                action_class=judged.action_class if judged else None,
                human=human,
                actor=decision.actor,
                ts=decision.ts,
                verdict=verdict,
                label=agreement(verdict, human),
                trace_url=judged.trace_url if judged else None,
                feedback=decision.feedback,
                advisory=decision.advisory,
            )
        )
    recent.sort(key=lambda d: d.seq, reverse=True)

    brk = state.chain_break
    return LiveView(
        readable=True,
        cursor_seq=state.cursor.seq,
        chain_break=brk.model_dump(mode="json") if brk else None,
        open_requests=open_requests,
        recent=recent[:RECENT_DECISIONS],
        counters=counters(state),
    )
