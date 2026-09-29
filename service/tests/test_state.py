"""Persisted state: atomic writes, at-most-once claims across restarts, fail closed."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from approved.state import Cursor, DecisionRecord, JudgedEntry, StateError, StateStore

URL = "https://facade.test/a/tenant-1"


def test_fresh_store_starts_at_genesis(tmp_path: Path) -> None:
    store = StateStore(tmp_path, URL)
    assert store.state.cursor == Cursor()
    assert not store.path.exists()


def test_claim_is_persisted_before_returning_and_survives_restart(tmp_path: Path) -> None:
    store = StateStore(tmp_path, URL)
    assert store.claim("k1", 3) is True
    reloaded = StateStore(tmp_path, URL)
    assert reloaded.state.judged["k1"].status == "claimed"
    assert reloaded.claim("k1", 3) is False


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    store = StateStore(tmp_path, URL)
    store.advance(Cursor(seq=5, hash="a" * 64))
    store.settle("k", JudgedEntry(status="notified", seq=5, decision="READY"))
    assert sorted(os.listdir(tmp_path)) == ["judge-state.json"]
    data = json.loads(store.path.read_text())
    assert data["cursor"] == {"seq": 5, "hash": "a" * 64}
    assert data["facade_url"] == URL


def test_failed_write_keeps_previous_state(tmp_path: Path, monkeypatch) -> None:
    store = StateStore(tmp_path, URL)
    store.advance(Cursor(seq=1, hash="b" * 64))
    before = store.path.read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.advance(Cursor(seq=2, hash="c" * 64))
    assert store.path.read_bytes() == before
    assert sorted(os.listdir(tmp_path)) == ["judge-state.json"]


def test_corrupt_state_refuses_to_load(tmp_path: Path) -> None:
    (tmp_path / "judge-state.json").write_text("{not json")
    with pytest.raises(StateError, match="refusing to start"):
        StateStore(tmp_path, URL)


def test_state_for_another_facade_refuses(tmp_path: Path) -> None:
    StateStore(tmp_path, URL).advance(Cursor(seq=1, hash="d" * 64))
    with pytest.raises(StateError, match="different facade"):
        StateStore(tmp_path, "https://facade.test/a/tenant-2")


def test_decisions_recorded_once_and_marked_when_not_judged(tmp_path: Path) -> None:
    store = StateStore(tmp_path, URL)
    store.claim("judged", 1)
    rec = DecisionRecord(event="approval.granted", actor="human:c", seq=2, ts="t")
    assert store.record_decision("judged", rec) is True
    assert store.record_decision("judged", rec) is False
    assert store.record_decision("never-judged", rec) is True
    assert store.state.decisions["judged"].feedback == "pending"
    assert store.state.decisions["never-judged"].feedback == "not-applicable"


def test_ledger_is_bounded(tmp_path: Path) -> None:
    store = StateStore(tmp_path, URL, max_entries=3)
    for i in range(5):
        store.claim(f"k{i}", i)
    assert list(store.state.judged) == ["k2", "k3", "k4"]
