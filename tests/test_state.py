from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dgx_autonomy.state import StateError, StateStore, operation_id

LAUNCH = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
DEADLINE = LAUNCH + timedelta(hours=40)


def _store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state" / "controller.sqlite3")


def _launch(store: StateStore, run_id: str = "r1") -> None:
    store.create_run(
        run_id=run_id,
        model_key="qwen3.6-35b-a3b",
        launched_at=LAUNCH,
        deadline_at=DEADLINE,
        brief_path="/data/runs/r1/brief.md",
    )


def test_create_run_persists_launch_and_deadline(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _launch(store)
    store.close()

    reopened = _store(tmp_path)
    run = reopened.get_run("r1")
    assert run is not None
    assert run.phase == "launched"
    assert run.launched_at == LAUNCH
    assert run.deadline_at == DEADLINE
    assert run.conversation_id is None
    assert [r.id for r in reopened.active_runs()] == ["r1"]


def test_deadline_is_immutable_even_through_raw_sql(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _launch(store)
    db = sqlite3.connect(tmp_path / "state" / "controller.sqlite3")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        db.execute(
            "update runs set deadline_at = ? where id = 'r1'",
            ((DEADLINE + timedelta(hours=1)).isoformat(),),
        )
    db.close()
    run = store.get_run("r1")
    assert run is not None and run.deadline_at == DEADLINE


def test_other_columns_stay_writable_under_the_deadline_trigger(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _launch(store)
    store.set_phase("r1", "running")
    run = store.get_run("r1")
    assert run is not None and run.phase == "running" and run.deadline_at == DEADLINE


def test_deadline_must_follow_launch(tmp_path: Path) -> None:
    with pytest.raises(StateError):
        _store(tmp_path).create_run(
            run_id="r1",
            model_key="m",
            launched_at=LAUNCH,
            deadline_at=LAUNCH,
            brief_path="b",
        )


def test_naive_timestamps_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _store(tmp_path).create_run(
            run_id="r1",
            model_key="m",
            launched_at=LAUNCH.replace(tzinfo=None),
            deadline_at=DEADLINE.replace(tzinfo=None),
            brief_path="b",
        )


def test_phase_transitions_are_one_way(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _launch(store)
    with pytest.raises(StateError):
        store.set_phase("r1", "finished")  # launched -> finished skips running
    store.set_phase("r1", "running")
    store.set_phase("r1", "finished")
    with pytest.raises(StateError):
        store.set_phase("r1", "running")
    assert store.active_runs() == []


def test_conversation_id_is_write_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _launch(store)
    store.set_conversation_id("r1", "c1")
    store.set_conversation_id("r1", "c1")  # same id again is fine
    with pytest.raises(StateError):
        store.set_conversation_id("r1", "c2")


def test_record_intent_is_idempotent_and_keeps_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _launch(store)
    first = store.record_intent("r1", "workspace.create", LAUNCH)
    assert first.id == operation_id("r1", "workspace.create") == "r1.workspace.create"
    assert first.status == "intended"

    store.complete_operation(first.id, "container-123")
    again = store.record_intent("r1", "workspace.create", LAUNCH + timedelta(minutes=5))
    assert again.status == "done"
    assert again.resource_id == "container-123"
    assert again.created_at == LAUNCH  # the original intent time is kept


def test_failed_operations_are_not_resurrected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _launch(store)
    op = store.record_intent("r1", "inference.start", LAUNCH)
    store.fail_operation(op.id, "OOM")
    store.complete_operation(op.id, "late-success")
    failed = store.get_operation("r1", "inference.start")
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error == "OOM"


def test_latest_run_and_listing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _launch(store, "r1")
    store.create_run(
        run_id="r2",
        model_key="m",
        launched_at=LAUNCH + timedelta(minutes=1),
        deadline_at=DEADLINE,
        brief_path="b",
    )
    latest = store.latest_run()
    assert latest is not None and latest.id == "r2"
    assert [r.id for r in store.list_runs()] == ["r1", "r2"]
