"""SQLite repository for controller-owned lifecycle state.

Tables:

- `runs`: one row per experiment. The deadline is fixed at launch. A stop request
  and the run's outcome are persisted before any cleanup happens.
- `operations`: a durable intent written before each external side effect, keyed by
  a stable id that is also put on the container as a docker label.
- `demos`: the demo the agent asked for, and the loopback host port it is published
  on. The controller relaunches exactly this spec if the sandbox has to restart.

`pragma user_version` records the schema version. Version 1 (Phase 1) databases are
migrated in place on open.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

RunPhase = Literal["launched", "running", "stopping", "stopped", "finished", "failed"]
RunOutcome = Literal["finished", "expired", "stopped", "blocked", "failed"]
OperationKind = Literal["inference.start", "workspace.create", "conversation.start"]
OperationStatus = Literal["intended", "done", "failed"]
DemoState = Literal["reserved", "starting", "running", "failed", "refused"]

SCHEMA_VERSION = 2
TERMINAL_PHASES: frozenset[str] = frozenset({"stopped", "finished", "failed"})
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "launched": frozenset({"running", "failed", "stopping"}),
    "running": frozenset({"finished", "failed", "stopping"}),
    # Once stop is requested nothing else may happen to the run; only the verified
    # end of agent execution moves it on.
    "stopping": frozenset({"stopped"}),
    "stopped": frozenset(),
    "finished": frozenset(),
    "failed": frozenset(),
}

_RUNS_TABLE = """
create table {name} (
    id              text primary key,
    phase           text not null check (phase in
                        ('launched', 'running', 'stopping', 'stopped', 'finished', 'failed')),
    model_key       text not null,
    launched_at     text not null,
    deadline_at     text not null,
    brief_path      text not null,
    conversation_id text,
    stop_requested  integer not null default 0 check (stop_requested in (0, 1)),
    outcome         text check (outcome in ('finished', 'expired', 'stopped', 'blocked', 'failed')),
    stop_evidence   text
)
"""

_DEADLINE_TRIGGER = """
-- The deadline is written once at launch. Nothing may move it, including recovery.
create trigger if not exists runs_deadline_immutable
before update of deadline_at on runs
when new.deadline_at is not old.deadline_at
begin
    select raise(abort, 'runs.deadline_at is immutable');
end;
"""

_OPERATIONS_TABLE = """
create table if not exists operations (
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

_DEMOS_TABLE = """
create table if not exists demos (
    run_id      text primary key references runs(id),
    command     text,              -- null until the agent asks for a demo
    port        integer not null,  -- container port, published to the host
    host_port   integer not null unique,  -- bound to 127.0.0.1 on the DGX
    request_id  text,              -- the last start_demo request handled
    session_id  integer,           -- demo session leader pid inside the sandbox
    state       text not null check (state in
                    ('reserved', 'starting', 'running', 'failed', 'refused')),
    message     text,
    updated_at  text not null
);
"""


class StateError(RuntimeError):
    """A lifecycle rule would be broken."""


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return ts.astimezone(UTC).isoformat(timespec="seconds")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


@dataclass(frozen=True)
class Run:
    id: str
    phase: RunPhase
    model_key: str
    launched_at: datetime
    deadline_at: datetime
    brief_path: str
    conversation_id: str | None
    stop_requested: bool = False
    outcome: RunOutcome | None = None
    stop_evidence: str | None = None  # JSON, see controller.StopEvidence

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES


@dataclass(frozen=True)
class Operation:
    id: str
    run_id: str
    kind: OperationKind
    status: OperationStatus
    resource_id: str | None
    error: str | None
    created_at: datetime


@dataclass(frozen=True)
class Demo:
    run_id: str
    command: str | None
    port: int
    host_port: int
    request_id: str | None
    session_id: int | None
    state: DemoState
    message: str | None
    updated_at: datetime


def operation_id(run_id: str, kind: OperationKind) -> str:
    return f"{run_id}.{kind}"


def _migrate(db: sqlite3.Connection) -> None:
    version = int(db.execute("pragma user_version").fetchone()[0])
    if version >= SCHEMA_VERSION:
        return
    has_runs = db.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'runs'"
    ).fetchone()
    # Table rebuilds need foreign keys off, and that pragma is a no-op inside a
    # transaction (https://sqlite.org/lang_altertable.html#otheralter).
    db.execute("pragma foreign_keys = off")
    db.execute("begin immediate")
    try:
        if not has_runs:
            db.execute(_RUNS_TABLE.format(name="runs"))
        else:
            # v1 -> v2: the phase check gains stopping/stopped, three columns are added.
            db.execute(_RUNS_TABLE.format(name="runs_v2"))
            db.execute(
                "insert into runs_v2 (id, phase, model_key, launched_at, deadline_at,"
                " brief_path, conversation_id, outcome)"
                " select id, phase, model_key, launched_at, deadline_at, brief_path,"
                " conversation_id, case when phase in ('finished', 'failed') then phase end"
                " from runs"
            )
            db.execute("drop trigger if exists runs_deadline_immutable")
            db.execute("drop table runs")
            db.execute("alter table runs_v2 rename to runs")
        # execute(), not executescript(): the latter would commit this transaction.
        db.execute(_DEADLINE_TRIGGER)
        db.execute(_OPERATIONS_TABLE)
        db.execute(_DEMOS_TABLE)
        problems = db.execute("pragma foreign_key_check").fetchall()
        if problems:
            raise StateError(f"schema migration broke foreign keys: {problems}")
        db.execute(f"pragma user_version = {SCHEMA_VERSION}")
    except BaseException:
        db.execute("rollback")
        raise
    db.execute("commit")


class StateStore:
    """Thread-safe: the control socket, the reconcile loop and the deadline watchdog
    share one store."""

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("pragma busy_timeout = 5000")
        if str(path) != ":memory:":
            self._db.execute("pragma journal_mode = wal")
        _migrate(self._db)
        self._db.execute("pragma foreign_keys = on")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("begin immediate")
            try:
                yield self._db
            except BaseException:
                self._db.execute("rollback")
                raise
            self._db.execute("commit")

    # --- runs ----------------------------------------------------------------------

    def create_run(
        self,
        *,
        run_id: str,
        model_key: str,
        launched_at: datetime,
        deadline_at: datetime,
        brief_path: str,
    ) -> Run:
        if deadline_at <= launched_at:
            raise StateError("deadline must be after launch")
        with self._tx() as db:
            db.execute(
                "insert into runs (id, phase, model_key, launched_at, deadline_at, brief_path)"
                " values (?, 'launched', ?, ?, ?, ?)",
                (run_id, model_key, _iso(launched_at), _iso(deadline_at), brief_path),
            )
        return self._require_run(run_id)

    def get_run(self, run_id: str) -> Run | None:
        with self._lock:
            row = self._db.execute("select * from runs where id = ?", (run_id,)).fetchone()
        return _run(row) if row else None

    def latest_run(self) -> Run | None:
        with self._lock:
            row = self._db.execute(
                "select * from runs order by launched_at desc, rowid desc limit 1"
            ).fetchone()
        return _run(row) if row else None

    def list_runs(self) -> list[Run]:
        with self._lock:
            rows = self._db.execute("select * from runs order by launched_at, rowid").fetchall()
        return [_run(r) for r in rows]

    def active_runs(self) -> list[Run]:
        return [r for r in self.list_runs() if not r.terminal]

    def set_phase(self, run_id: str, phase: RunPhase) -> Run:
        """Move the run on. Raises StateError on a transition the lifecycle forbids."""
        if not self.try_set_phase(run_id, phase):
            run = self._require_run(run_id)
            raise StateError(f"run {run_id}: {run.phase} -> {phase} is not allowed")
        return self._require_run(run_id)

    def try_set_phase(self, run_id: str, phase: RunPhase) -> bool:
        """Like set_phase, but a forbidden transition returns False instead of raising.

        The reconcile loop uses this: a stop may have moved the run on while it was
        blocked in an SDK call, and then its own transition must simply not happen.
        """
        with self._tx() as db:
            row = db.execute("select phase from runs where id = ?", (run_id,)).fetchone()
            if row is None:
                raise StateError(f"no run {run_id}")
            current = str(row["phase"])
            if phase == current:
                return True
            if phase not in _ALLOWED_TRANSITIONS[current]:
                return False
            outcome = phase if phase in ("finished", "failed") else None
            db.execute(
                "update runs set phase = ?, outcome = coalesce(outcome, ?) where id = ?",
                (phase, outcome, run_id),
            )
        return True

    def request_stop(self, run_id: str, outcome: RunOutcome) -> Run:
        """Persist the stop intent before any cleanup. The first reason recorded wins.

        A run that already ended is returned unchanged.
        """
        with self._tx() as db:
            row = db.execute("select phase from runs where id = ?", (run_id,)).fetchone()
            if row is None:
                raise StateError(f"no run {run_id}")
            if str(row["phase"]) not in TERMINAL_PHASES:
                db.execute(
                    "update runs set stop_requested = 1, phase = 'stopping',"
                    " outcome = coalesce(outcome, ?) where id = ?",
                    (outcome, run_id),
                )
        return self._require_run(run_id)

    def record_stop(self, run_id: str, evidence_json: str, *, verified: bool) -> Run:
        """Store the stop evidence; `verified` (no agent execution left) ends the run."""
        with self._tx() as db:
            db.execute(
                "update runs set stop_evidence = ? where id = ? and stop_requested = 1",
                (evidence_json, run_id),
            )
            if verified:
                db.execute(
                    "update runs set phase = 'stopped' where id = ? and phase = 'stopping'",
                    (run_id,),
                )
        return self._require_run(run_id)

    def set_conversation_id(self, run_id: str, conversation_id: str) -> Run:
        """Write-once: a run keeps the conversation it started with in this phase."""
        with self._tx() as db:
            row = db.execute("select conversation_id from runs where id = ?", (run_id,)).fetchone()
            if row is None:
                raise StateError(f"no run {run_id}")
            existing = row["conversation_id"]
            if existing is not None and existing != conversation_id:
                raise StateError(f"run {run_id} already has conversation {existing}")
            db.execute(
                "update runs set conversation_id = ? where id = ?", (conversation_id, run_id)
            )
        return self._require_run(run_id)

    def _require_run(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run is None:
            raise StateError(f"no run {run_id}")
        return run

    # --- operations ----------------------------------------------------------------

    def record_intent(self, run_id: str, kind: OperationKind, now: datetime) -> Operation:
        """Persist the intent before the side effect. Idempotent: returns the existing row."""
        op_id = operation_id(run_id, kind)
        with self._tx() as db:
            db.execute(
                "insert or ignore into operations (id, run_id, kind, status, created_at)"
                " values (?, ?, ?, 'intended', ?)",
                (op_id, run_id, kind, _iso(now)),
            )
        return self._require_op(op_id)

    def note_resource(self, op_id: str, resource_id: str) -> Operation:
        """Record what the side effect created while the operation is still in progress."""
        with self._tx() as db:
            db.execute(
                "update operations set resource_id = ? where id = ? and status = 'intended'",
                (resource_id, op_id),
            )
        return self._require_op(op_id)

    def complete_operation(self, op_id: str, resource_id: str) -> Operation:
        with self._tx() as db:
            db.execute(
                "update operations set status = 'done', resource_id = ?, error = null"
                " where id = ? and status != 'failed'",
                (resource_id, op_id),
            )
        return self._require_op(op_id)

    def fail_operation(self, op_id: str, error: str) -> Operation:
        with self._tx() as db:
            db.execute(
                "update operations set status = 'failed', error = ? where id = ?",
                (error, op_id),
            )
        return self._require_op(op_id)

    def get_operation(self, run_id: str, kind: OperationKind) -> Operation | None:
        with self._lock:
            row = self._db.execute(
                "select * from operations where id = ?", (operation_id(run_id, kind),)
            ).fetchone()
        return _op(row) if row else None

    def operations(self, run_id: str) -> list[Operation]:
        with self._lock:
            rows = self._db.execute(
                "select * from operations where run_id = ? order by created_at, rowid", (run_id,)
            ).fetchall()
        return [_op(r) for r in rows]

    def _require_op(self, op_id: str) -> Operation:
        with self._lock:
            row = self._db.execute("select * from operations where id = ?", (op_id,)).fetchone()
        if row is None:
            raise StateError(f"no operation {op_id}")
        return _op(row)

    # --- demos ---------------------------------------------------------------------

    def reserve_demo(
        self, run_id: str, *, port: int, host_port_base: int, host_port_count: int, now: datetime
    ) -> Demo:
        """The run's demo row with a loopback host port of its own. Idempotent.

        Host ports are never reused across runs, so a retained demo of an old run
        keeps its tunnel command.
        """
        with self._tx() as db:
            if db.execute("select 1 from demos where run_id = ?", (run_id,)).fetchone() is None:
                used = {int(r["host_port"]) for r in db.execute("select host_port from demos")}
                free = next(
                    (
                        p
                        for p in range(host_port_base, host_port_base + host_port_count)
                        if p not in used
                    ),
                    None,
                )
                if free is None:
                    last = host_port_base + host_port_count - 1
                    raise StateError(f"no free demo port in {host_port_base}..{last}")
                db.execute(
                    "insert into demos (run_id, port, host_port, state, updated_at)"
                    " values (?, ?, ?, 'reserved', ?)",
                    (run_id, port, free, _iso(now)),
                )
        demo = self.get_demo(run_id)
        assert demo is not None
        return demo

    def get_demo(self, run_id: str) -> Demo | None:
        with self._lock:
            row = self._db.execute("select * from demos where run_id = ?", (run_id,)).fetchone()
        return _demo(row) if row else None

    def record_demo_request(
        self,
        run_id: str,
        *,
        request_id: str,
        command: str | None,
        state: DemoState,
        message: str | None,
        now: datetime,
    ) -> Demo:
        """Persist the requested spec (or the refusal) before anything is started.

        A refused request keeps the previous command: the demo that is running stays
        the one to relaunch.
        """
        with self._tx() as db:
            db.execute(
                "update demos set request_id = ?, command = coalesce(?, command), state = ?,"
                " message = ?, updated_at = ? where run_id = ?",
                (request_id, command, state, message, _iso(now), run_id),
            )
        return self._require_demo(run_id)

    def set_demo_state(
        self,
        run_id: str,
        *,
        state: DemoState,
        message: str | None,
        now: datetime,
        session_id: int | None = None,
    ) -> Demo:
        """Update the demo's state; `session_id=None` keeps the recorded session."""
        with self._tx() as db:
            db.execute(
                "update demos set state = ?, message = ?, updated_at = ?,"
                " session_id = coalesce(?, session_id) where run_id = ?",
                (state, message, _iso(now), session_id, run_id),
            )
        return self._require_demo(run_id)

    def _require_demo(self, run_id: str) -> Demo:
        demo = self.get_demo(run_id)
        if demo is None:
            raise StateError(f"run {run_id} has no demo row")
        return demo


def _run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        phase=cast(RunPhase, row["phase"]),
        model_key=row["model_key"],
        launched_at=_parse(row["launched_at"]),
        deadline_at=_parse(row["deadline_at"]),
        brief_path=row["brief_path"],
        conversation_id=row["conversation_id"],
        stop_requested=bool(row["stop_requested"]),
        outcome=cast(RunOutcome | None, row["outcome"]),
        stop_evidence=row["stop_evidence"],
    )


def _op(row: sqlite3.Row) -> Operation:
    return Operation(
        id=row["id"],
        run_id=row["run_id"],
        kind=cast(OperationKind, row["kind"]),
        status=cast(OperationStatus, row["status"]),
        resource_id=row["resource_id"],
        error=row["error"],
        created_at=_parse(row["created_at"]),
    )


def _demo(row: sqlite3.Row) -> Demo:
    return Demo(
        run_id=row["run_id"],
        command=row["command"],
        port=int(row["port"]),
        host_port=int(row["host_port"]),
        request_id=row["request_id"],
        session_id=row["session_id"],
        state=cast(DemoState, row["state"]),
        message=row["message"],
        updated_at=_parse(row["updated_at"]),
    )
