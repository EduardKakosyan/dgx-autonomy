"""SQLite repository for controller-owned lifecycle state.

Two tables in this phase: `runs` (one row per experiment, with the deadline fixed at
launch) and `operations` (a durable intent written before each external side effect,
keyed by a stable id that is also put on the container as a docker label).
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

RunPhase = Literal["launched", "running", "finished", "failed"]
OperationKind = Literal["inference.start", "workspace.create", "conversation.start"]
OperationStatus = Literal["intended", "done", "failed"]

TERMINAL_PHASES: frozenset[str] = frozenset({"finished", "failed"})
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "launched": frozenset({"running", "failed"}),
    "running": frozenset({"finished", "failed"}),
    "finished": frozenset(),
    "failed": frozenset(),
}

SCHEMA = """
create table if not exists runs (
    id              text primary key,
    phase           text not null check (phase in ('launched', 'running', 'finished', 'failed')),
    model_key       text not null,
    launched_at     text not null,
    deadline_at     text not null,
    brief_path      text not null,
    conversation_id text
);

-- The deadline is written once at launch. Nothing may move it, including recovery.
create trigger if not exists runs_deadline_immutable
before update of deadline_at on runs
when new.deadline_at is not old.deadline_at
begin
    select raise(abort, 'runs.deadline_at is immutable');
end;

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


def operation_id(run_id: str, kind: OperationKind) -> str:
    return f"{run_id}.{kind}"


class StateStore:
    """Thread-safe: the control socket and the reconcile loop share one store."""

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("pragma foreign_keys = on")
        self._db.execute("pragma busy_timeout = 5000")
        if str(path) != ":memory:":
            self._db.execute("pragma journal_mode = wal")
        self._db.executescript(SCHEMA)

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
        with self._tx() as db:
            row = db.execute("select phase from runs where id = ?", (run_id,)).fetchone()
            if row is None:
                raise StateError(f"no run {run_id}")
            current = str(row["phase"])
            if phase != current:
                if phase not in _ALLOWED_TRANSITIONS[current]:
                    raise StateError(f"run {run_id}: {current} -> {phase} is not allowed")
                db.execute("update runs set phase = ? where id = ?", (phase, run_id))
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


def _run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        phase=cast(RunPhase, row["phase"]),
        model_key=row["model_key"],
        launched_at=_parse(row["launched_at"]),
        deadline_at=_parse(row["deadline_at"]),
        brief_path=row["brief_path"],
        conversation_id=row["conversation_id"],
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
