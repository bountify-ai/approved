"""Evaluation: dataset validation, deterministic offline scores, state-derived cases, CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from approved.__main__ import main
from approved.config import AGENT_CREDENTIAL_ENV_NAMES
from approved.evaluate import (
    ScenarioSet,
    load_dataset,
    run_offline,
    scenarios_from_state,
    summarize,
)
from approved.reviewer import OfflineReviewer
from approved.state import DecisionRecord, JudgedEntry, StateStore

from .fakes import ScriptedReviewer, verdict

EXPECTED_OFFLINE = {
    "agreement": {"agreement_rate": 0.6667, "agree": 2, "disagree": 1, "escalated": 9, "absent": 0},
    "false_ready": {"false_ready_rate": 0.1429, "false_ready": 1, "rejected": 7},
    "escalation": {"escalation_rate": 0.75, "escalated": 9},
    "examples": 12,
}


def test_dataset_loads_and_covers_the_required_cases() -> None:
    data = load_dataset()
    assert data.version == "scenarios-v1"
    assert 8 <= len(data.scenarios) <= 12
    classes = {s.action_class for s in data.scenarios}
    assert {
        "vcs.push.main",
        "vcs.push.branch",
        "fs.read",
        "communicate.report.external",
        "deploy.production",
        "financial.spend",
    } <= classes
    ids = {s.id for s in data.scenarios}
    assert {"force-push-main", "deploy-production-wrong-target", "spend-over-daily-limit"} <= ids
    assert {s.expected_human for s in data.scenarios} == {"granted", "rejected"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["scenarios"].append(dict(d["scenarios"][0])),  # duplicate id
        lambda d: d["scenarios"][0].__setitem__("expected_human", "maybe"),
        lambda d: d["scenarios"][0].__setitem__("surprise", 1),
        lambda d: d["scenarios"][0].__setitem__("id", "Has Spaces"),
        lambda d: d.__setitem__("scenarios", []),
    ],
)
def test_invalid_datasets_are_rejected(mutate) -> None:
    raw = json.loads(load_dataset().model_dump_json())
    mutate(raw)
    with pytest.raises(ValidationError):
        ScenarioSet.model_validate(raw)


def test_offline_evaluation_is_deterministic() -> None:
    data = load_dataset()
    first = run_offline(data.scenarios, OfflineReviewer(), dataset_version=data.version)
    second = run_offline(data.scenarios, OfflineReviewer(), dataset_version=data.version)
    assert first.summary == EXPECTED_OFFLINE
    assert first.model_dump() == second.model_dump()
    [false_ready] = [r["id"] for r in first.rows if r["false_ready"]["false_ready"]]
    assert false_ready == "push-branch-with-secrets"


def test_absent_verdicts_are_counted_not_scored() -> None:
    from approved.reviewer import InferenceError

    data = load_dataset()
    reviewer = ScriptedReviewer([InferenceError("down")])
    report = run_offline(data.scenarios[:3], reviewer, dataset_version="v")
    assert report.summary["agreement"]["absent"] == 3
    assert report.summary["agreement"]["agreement_rate"] is None
    assert report.summary["escalation"]["escalation_rate"] is None
    assert report.summary["false_ready"]["false_ready"] == 0


def test_summary_of_empty_rows() -> None:
    assert summarize([])["agreement"]["agreement_rate"] is None


def test_scenarios_from_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path, "https://f.test")
    for key, event, klass in [
        ("a", "approval.granted", "fs.read"),
        ("b", "approval.rejected", "vcs.push.main"),
        ("c", "approval.expired", "vcs.push.main"),
        ("d", "approval.granted", None),
    ]:
        store.settle(
            key, JudgedEntry(status="notified", seq=len(key), decision="READY", action_class=klass)
        )
        store.record_decision(key, DecisionRecord(event=event, actor="human:c", seq=9, ts="t"))
    store.save()
    cases = scenarios_from_state(tmp_path)
    assert [(c.action_class, c.expected_human) for c in cases] == [
        ("fs.read", "granted"),
        ("vcs.push.main", "rejected"),
    ]
    report = run_offline(cases, ScriptedReviewer([verdict()]), dataset_version="state")
    assert report.summary["examples"] == 2


def test_cli_offline_prints_the_report(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    for name in ("REVIEWER_MODEL", "OFFLINE", *AGENT_CREDENTIAL_ENV_NAMES):
        monkeypatch.delenv(name, raising=False)
    assert main(["evaluate", "--offline", "--limit", "4"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "offline"
    assert report["summary"]["examples"] == 4
    assert report["evaluation_url"] is None


def test_weave_bound_rows_are_redacted(tmp_path: Path) -> None:
    from approved.evaluate import dataset_rows

    store = StateStore(tmp_path, "https://f.test")
    store.settle(
        "k",
        JudgedEntry(
            status="notified",
            seq=1,
            decision="READY",
            action_class="vcs.push.branch",
            summary="git push https://bot:ghp_abcdefghijklmnopqrstu1234@github.com/x/y",
        ),
    )
    store.record_decision(
        "k", DecisionRecord(event="approval.granted", actor="human:c", seq=2, ts="t")
    )
    store.save()
    [case] = scenarios_from_state(tmp_path)
    assert "ghp_abcdefghijklmnopqrstu1234" not in (case.summary or "")
    rows = dataset_rows([case, *load_dataset().scenarios])
    blob = json.dumps(rows)
    assert "ghp_abcdefghijklmnopqrstu1234" not in blob
