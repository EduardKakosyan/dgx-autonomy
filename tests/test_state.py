from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dgx_autonomy.state import SCHEMA_VERSION, StateError, StateStore, operation_id

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


# The Phase 1 schema, verbatim, as databases on the DGX still have it.
_V1_SCHEMA = """
create table runs (
    id              text primary key,
    phase           text not null check (phase in ('launched', 'running', 'finished', 'failed')),
    model_key       text not null,
    launched_at     text not null,
    deadline_at     text not null,
    brief_path      text not null,
    conversation_id text
);
create trigger runs_deadline_immutable
before update of deadline_at on runs
when new.deadline_at is not old.deadline_at
begin
    select raise(abort, 'runs.deadline_at is immutable');
end;
create table operations (
    id          text primary key,
    run_id      text not null references runs(id),
    kind        text not null,
    status      text not null check (status in ('intended', 'done', 'failed')),
    resource_id text,
    error       text,
    created_at  text not null,
    unique (run_id, kind)
);
"""


def test_a_phase_1_database_is_migrated_in_place(tmp_path: Path) -> None:
    path = tmp_path / "state" / "controller.sqlite3"
    path.parent.mkdir()
    db = sqlite3.connect(path)
    db.executescript(_V1_SCHEMA)
    db.execute(
        "insert into runs values ('old', 'finished', 'm', ?, ?, 'b', 'c1')",
        (LAUNCH.isoformat(), DEADLINE.isoformat()),
    )
    db.execute(
        "insert into operations values ('old.workspace.create', 'old', 'workspace.create',"
        " 'done', 'cid', null, ?)",
        (LAUNCH.isoformat(),),
    )
    db.commit()
    db.close()

    store = StateStore(path)
    old = store.get_run("old")
    assert old is not None
    assert (old.phase, old.outcome, old.stop_requested, old.conversation_id) == (
        "finished",
        "finished",
        False,
        "c1",
    )
    assert [o.status for o in store.operations("old")] == ["done"]
    # The new phases are accepted, and the deadline trigger survived the rebuild.
    _launch(store, "r1")
    store.request_stop("r1", "expired")
    raw = sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        raw.execute("update runs set deadline_at = 'x' where id = 'r1'")
    assert raw.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION
    assert raw.execute("pragma foreign_key_check").fetchall() == []
    raw.close()
    store.close()
    StateStore(path).close()  # opening again is a no-op


def test_a_phase_3_database_gains_retries_recoveries_and_the_writer_lock(tmp_path: Path) -> None:
    from dgx_autonomy import state as st

    path = tmp_path / "state" / "controller.sqlite3"
    path.parent.mkdir()
    db = sqlite3.connect(path)
    db.execute(st._RUNS_TABLE.format(name="runs"))
    for ddl in (st._DEADLINE_TRIGGER, st._OPERATIONS_TABLE, st._DEMOS_TABLE):
        db.execute(ddl)
    db.execute(
        "insert into runs (id, phase, model_key, launched_at, deadline_at, brief_path)"
        " values ('r1', 'running', 'm', ?, ?, 'b')",
        (LAUNCH.isoformat(), DEADLINE.isoformat()),
    )
    db.execute(
        "insert into operations values ('r1.inference.start', 'r1', 'inference.start',"
        " 'intended', null, null, ?)",
        (LAUNCH.isoformat(),),
    )
    db.execute("pragma user_version = 2")
    db.commit()
    db.close()

    store = StateStore(path)
    [op] = store.operations("r1")
    assert op.attempted_at == op.created_at == LAUNCH
    later = LAUNCH + timedelta(hours=1)
    op = store.retry_operation(op.id, later, resource_id="cid")
    assert (op.attempted_at, op.created_at, op.resource_id) == (later, LAUNCH, "cid")
    rec = store.begin_recovery(
        "r1", cause="the agent sandbox is exited", status_before="running", now=later
    )
    assert rec.id == "r1.recover.1" and rec.status == "intended"
    assert store.begin_recovery("r1", cause="other", status_before=None, now=later) == rec
    store.finish_recovery(rec.id, status="done", error=None, now=later)
    assert store.begin_recovery("r1", cause="again", status_before=None, now=later).n == 2
    assert store.acquire_writer(pid=1, host="h", boot_id="b", now=later) is None
    run = store.get_run("r1")
    assert run is not None and run.deadline_at == DEADLINE
    store.close()


def test_stop_request_is_durable_and_first_reason_wins(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _launch(store)
    store.set_phase("r1", "running")
    run = store.request_stop("r1", "stopped")
    assert (run.phase, run.outcome, run.stop_requested) == ("stopping", "stopped", True)
    run = store.request_stop("r1", "expired")
    assert run.outcome == "stopped"
    # While stopping, nothing but the verified end of agent execution moves the run.
    assert store.try_set_phase("r1", "running") is False
    assert store.try_set_phase("r1", "finished") is False
    with pytest.raises(StateError):
        store.set_phase("r1", "failed")
    run = store.record_stop("r1", '{"failed": true}', verified=False)
    assert run.phase == "stopping" and run.stop_evidence == '{"failed": true}'
    run = store.record_stop("r1", '{"failed": false}', verified=True)
    assert run.phase == "stopped" and run.terminal
    assert store.active_runs() == []


def test_ended_runs_ignore_stop_requests(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _launch(store)
    store.set_phase("r1", "running")
    store.set_phase("r1", "finished")
    run = store.request_stop("r1", "expired")
    assert (run.phase, run.outcome, run.stop_requested) == ("finished", "finished", False)


def test_demo_ports_are_reserved_once_per_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _launch(store, "r1")
    _launch(store, "r2")
    a = store.reserve_demo("r1", port=3000, host_port_base=43000, host_port_count=2, now=LAUNCH)
    again = store.reserve_demo("r1", port=3000, host_port_base=43000, host_port_count=2, now=LAUNCH)
    b = store.reserve_demo("r2", port=3000, host_port_base=43000, host_port_count=2, now=LAUNCH)
    assert (a.host_port, again.host_port, b.host_port) == (43000, 43000, 43001)
    _launch(store, "r3")
    with pytest.raises(StateError, match="no free demo port"):
        store.reserve_demo("r3", port=3000, host_port_base=43000, host_port_count=2, now=LAUNCH)


def test_a_refused_demo_request_keeps_the_command_to_relaunch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _launch(store)
    store.reserve_demo("r1", port=3000, host_port_base=43000, host_port_count=10, now=LAUNCH)
    store.record_demo_request(
        "r1", request_id="a", command="pnpm start", state="starting", message=None, now=LAUNCH
    )
    store.set_demo_state("r1", state="running", message=None, now=LAUNCH, session_id=77)
    demo = store.record_demo_request(
        "r1", request_id="b", command=None, state="refused", message="port", now=LAUNCH
    )
    assert (demo.command, demo.session_id, demo.request_id) == ("pnpm start", 77, "b")
