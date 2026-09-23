"""SQLite repository for controller-owned lifecycle state.

Tables:

- `runs`: one row per experiment. The deadline is fixed at launch. A stop request
  and the run's outcome are persisted before any cleanup happens.
- `operations`: a durable intent written before each external side effect, keyed by
  a stable id that is also put on the container as a docker label.
- `demos`: the demo the agent asked for, and the loopback host port it is published
  on. The controller relaunches exactly this spec if the sandbox has to restart.
- `recoveries`: each time the controller brought a run's inference or sandbox back
  after it went down (a crash, a DGX restart), with what it found and did.
- `controller_lock`: the one controller allowed to write. See `acquire_writer`.
- `criteria`: the acceptance criteria frozen at launch (frozen.py), automated or
  awaiting human judgment.
- `evaluations`: each run of the frozen checks against a pinned project snapshot,
  with its per-criterion results and where its evidence is.
- `reviews`: manually requested stronger-model reviews, kept apart from the checks.
- `plans`: interactive planning sessions (planning.py). A plan has no deadline; its
  launch freezes the agreed draft and creates the run, which gets one then.
- `conversations`: every OpenHands conversation a run has had, in order. A rollover
  (checkpoints.py) replaces the active conversation with a fresh one; the run's
  `conversation_id` always names the active one.
- `checkpoints`: the structured handoff each rollover starts from, written by the
  agent (validated) or, failing that, recorded by the controller.

`pragma user_version` records the schema version. Older databases are migrated in
place on open.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

RunPhase = Literal["launched", "running", "stopping", "stopped", "finished", "failed"]
RunOutcome = Literal["finished", "expired", "stopped", "blocked", "failed"]
OperationKind = Literal["inference.start", "workspace.create", "conversation.start"]
OperationStatus = Literal["intended", "done", "failed"]
DemoState = Literal["reserved", "starting", "running", "failed", "refused"]
RecoveryStatus = Literal["intended", "done", "failed"]
EvaluationResult = Literal["passed", "failed", "inconclusive", "infra_error"]
EvaluationStatus = Literal["intended", "passed", "failed", "inconclusive", "infra_error"]
EvaluationTrigger = Literal["claim", "final", "requested"]
ReviewStatus = Literal["requested", "recorded"]
PlanState = Literal["starting", "open", "launched", "closed", "failed"]
ConversationRowStatus = Literal["handoff", "starting", "active", "ended", "abandoned"]
CheckpointSource = Literal["agent_handoff", "controller_fallback"]

SCHEMA_VERSION = 6
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


_RECOVERIES_TABLE = """
create table if not exists recoveries (
    id            text primary key,   -- <run_id>.recover.<n>
    run_id        text not null references runs(id),
    n             integer not null,
    cause         text not null,
    status        text not null check (status in ('intended', 'done', 'failed')),
    -- The conversation's persisted status before anything was restarted: the Agent
    -- Server rewrites an interrupted RUNNING conversation to ERROR when it loads it.
    status_before text,
    steps         text not null default '{}',   -- JSON: what this attempt did
    error         text,
    started_at    text not null,
    attempted_at  text not null,   -- timeouts count from here; reset on controller start
    finished_at   text,
    unique (run_id, n)
);
"""

_CONTROLLER_LOCK_TABLE = """
create table if not exists controller_lock (
    id          integer primary key check (id = 1),
    token       text not null,
    pid         integer not null,
    host        text not null,
    boot_id     text not null,
    acquired_at text not null
);
"""


_CRITERIA_TABLE = """
create table if not exists criteria (
    run_id               text not null references runs(id),
    key                  text not null,
    position             integer not null,
    kind                 text not null check (kind in ('automated', 'human_judgment')),
    required             integer not null check (required in (0, 1)),
    description          text not null,
    test                 text,              -- automated: the check file under checks/
    runner               text,              -- automated: pytest | playwright
    latest_evaluation_id text,              -- the last evaluation that reported on it
    latest_status        text,
    primary key (run_id, key)
);
"""

_EVALUATIONS_TABLE = """
create table if not exists evaluations (
    id                    text primary key,   -- <run_id>.eval.<n>
    run_id                text not null references runs(id),
    n                     integer not null,
    trigger               text not null check (trigger in ('claim', 'final', 'requested')),
    -- The builder's completion claim this evaluation answers (claim trigger).
    claim_event_id        text,
    claim_text            text,
    check_digest          text not null,      -- the frozen agreement it ran
    workspace_snapshot_id text,               -- git tree sha of the checked project
    snapshot_commit       text,
    status                text not null check (status in
                              ('intended', 'passed', 'failed', 'inconclusive', 'infra_error')),
    detail                text,
    steps                 text not null default '{}',   -- JSON: progress of this attempt
    results               text not null default '{}',   -- JSON: criterion key -> result
    evidence_dir          text not null,      -- controller-owned
    started_at            text not null,
    attempted_at          text not null,
    finished_at           text,
    delivered_at          text,               -- failures handed to the builder
    unique (run_id, n)
);
"""

_REVIEWS_TABLE = """
create table if not exists reviews (
    id              text primary key,   -- <run_id>.review.<n>
    run_id          text not null references runs(id),
    n               integer not null,
    status          text not null check (status in ('requested', 'recorded')),
    note            text,
    evaluation_id   text,               -- the latest evaluation when it was requested
    snapshot_commit text,
    bundle_dir      text not null,
    requested_at    text not null,
    reviewer        text,
    result_path     text,
    recorded_at     text,
    unique (run_id, n)
);
"""


_PLANS_TABLE = """
create table if not exists plans (
    id              text primary key,
    state           text not null check (state in
                        ('starting', 'open', 'launched', 'closed', 'failed')),
    model_key       text not null,
    request         text not null,      -- the operator's opening request
    created_at      text not null,
    updated_at      text not null,
    conversation_id text,
    error           text,
    -- The last dry run of the draft checks (JSON), bound to the draft digest.
    dry_run         text,
    -- Set once, at launch: the run the plan became and the digest it froze.
    run_id          text unique references runs(id),
    launched_digest text
);
"""


_CONVERSATIONS_TABLE = """
create table if not exists conversations (
    id                 text primary key,   -- <run_id>.conv.<n>
    run_id             text not null references runs(id),
    n                  integer not null,
    conversation_id    text not null,      -- the SDK conversation id (derived, stable)
    -- handoff: the previous conversation is asked for its handoff; starting: the
    -- checkpoint is recorded and this conversation is being started; active: the
    -- run's conversation; ended: replaced; abandoned: the rollover was given up
    status             text not null check (status in
                           ('handoff', 'starting', 'active', 'ended', 'abandoned')),
    reason             text not null,      -- launch | stuck | errors | context | failures
                                           -- | blocked-review | forced
    detail             text,
    from_checkpoint_id text,               -- the checkpoint it starts from (n > 1)
    handoff_request_id text,               -- the request sent to the previous conversation
    handoff_attempts   integer not null default 0,
    started_at         text not null,
    attempted_at       text not null,
    active_at          text,
    ended_at           text,
    unique (run_id, n),
    unique (conversation_id)
);
"""

_CHECKPOINTS_TABLE = """
create table if not exists checkpoints (
    id              text primary key,   -- <run_id>.ckpt.<n>
    run_id          text not null references runs(id),
    n               integer not null,
    conversation_id text not null,      -- the conversation it hands off from
    event_position  integer,            -- events in that conversation when recorded
    workspace_sha   text,               -- project snapshot (tree) when recorded
    supersedes_id   text,
    source          text not null check (source in ('agent_handoff', 'controller_fallback')),
    reason          text not null,
    handoff         text not null,      -- JSON: the validated handoff, or the fallback
    verified        text not null,      -- JSON: what the controller itself established
    problems        text,               -- JSON list: why an agent handoff was not used
    created_at      text not null,
    unique (run_id, n)
);
"""


class StateError(RuntimeError):
    """A lifecycle rule would be broken."""


class WriterLockError(StateError):
    """Another controller holds the state store, or this one lost it."""


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
    # frozen.bundle_digest of the brief and checks, recorded at launch. None for
    # runs launched before the agreement was frozen (schema < 4).
    frozen_digest: str | None = None
    # The checkpoint the active conversation started from (None: the first one).
    current_checkpoint_id: str | None = None
    # JSON: the blocker a fresh conversation confirmed (outcome `blocked`).
    blocked: str | None = None

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
    # When the current attempt began. Equal to created_at unless startup
    # reconciliation retried the operation.
    attempted_at: datetime


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
    # The project snapshot (tree sha) the demo was last started from.
    snapshot_id: str | None = None


@dataclass(frozen=True)
class Recovery:
    id: str
    run_id: str
    n: int
    cause: str
    status: RecoveryStatus
    status_before: str | None
    steps: dict[str, Any]
    error: str | None
    started_at: datetime
    attempted_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class WriterHolder:
    """Who holds (or last held) the controller writer lock."""

    token: str
    pid: int
    host: str
    boot_id: str
    acquired_at: datetime


@dataclass(frozen=True)
class CriterionRow:
    run_id: str
    key: str
    position: int
    kind: str
    required: bool
    description: str
    test: str | None
    runner: str | None
    latest_evaluation_id: str | None
    latest_status: str | None


@dataclass(frozen=True)
class Evaluation:
    id: str
    run_id: str
    n: int
    trigger: EvaluationTrigger
    claim_event_id: str | None
    claim_text: str | None
    check_digest: str
    snapshot_id: str | None
    snapshot_commit: str | None
    status: EvaluationStatus
    detail: str | None
    steps: dict[str, Any]
    results: dict[str, Any]
    evidence_dir: str
    started_at: datetime
    attempted_at: datetime
    finished_at: datetime | None
    delivered_at: datetime | None

    @property
    def open(self) -> bool:
        return self.status == "intended"


@dataclass(frozen=True)
class ConversationRow:
    id: str
    run_id: str
    n: int
    conversation_id: str
    status: ConversationRowStatus
    reason: str
    detail: str | None
    from_checkpoint_id: str | None
    handoff_request_id: str | None
    handoff_attempts: int
    started_at: datetime
    attempted_at: datetime
    active_at: datetime | None
    ended_at: datetime | None

    @property
    def rolling_over(self) -> bool:
        return self.status in ("handoff", "starting")


@dataclass(frozen=True)
class Checkpoint:
    id: str
    run_id: str
    n: int
    conversation_id: str
    event_position: int | None
    workspace_sha: str | None
    supersedes_id: str | None
    source: CheckpointSource
    reason: str
    handoff: dict[str, Any]
    verified: dict[str, Any]
    problems: list[str]
    created_at: datetime


@dataclass(frozen=True)
class Plan:
    id: str
    state: PlanState
    model_key: str
    request: str
    created_at: datetime
    updated_at: datetime
    conversation_id: str | None
    error: str | None
    dry_run: dict[str, Any] | None
    run_id: str | None
    launched_digest: str | None

    @property
    def active(self) -> bool:
        """Its planner sandbox should be up."""
        return self.state in ("starting", "open")


@dataclass(frozen=True)
class Review:
    id: str
    run_id: str
    n: int
    status: ReviewStatus
    note: str | None
    evaluation_id: str | None
    snapshot_commit: str | None
    bundle_dir: str
    requested_at: datetime
    reviewer: str | None
    result_path: str | None
    recorded_at: datetime | None


def operation_id(run_id: str, kind: OperationKind) -> str:
    return f"{run_id}.{kind}"


def _migrate(db: sqlite3.Connection) -> None:
    version = int(db.execute("pragma user_version").fetchone()[0])
    if version >= SCHEMA_VERSION:
        return
    # Table rebuilds need foreign keys off, and that pragma is a no-op inside a
    # transaction (https://sqlite.org/lang_altertable.html#otheralter).
    db.execute("pragma foreign_keys = off")
    db.execute("begin immediate")
    try:
        if version < 2:
            _migrate_to_v2(db)
        if version < 3:
            # v2 -> v3: operations can be retried after a restart; recoveries and the
            # writer lock are new.
            db.execute("alter table operations add column attempted_at text")
            db.execute(_RECOVERIES_TABLE)
            db.execute(_CONTROLLER_LOCK_TABLE)
        if version < 4:
            # v3 -> v4: the frozen agreement, its criteria, evaluations and reviews;
            # the snapshot a demo was started from.
            db.execute("alter table runs add column frozen_digest text")
            db.execute("alter table demos add column snapshot_id text")
            db.execute(_CRITERIA_TABLE)
            db.execute(_EVALUATIONS_TABLE)
            db.execute(_REVIEWS_TABLE)
        if version < 5:
            # v4 -> v5: interactive planning sessions.
            db.execute(_PLANS_TABLE)
        if version < 6:
            # v5 -> v6: conversation history, checkpoints, the confirmed blocker. A
            # run's existing conversation becomes its first one.
            db.execute("alter table runs add column current_checkpoint_id text")
            db.execute("alter table runs add column blocked text")
            db.execute(_CONVERSATIONS_TABLE)
            db.execute(_CHECKPOINTS_TABLE)
            db.execute(
                "insert into conversations (id, run_id, n, conversation_id, status, reason,"
                " started_at, attempted_at, active_at)"
                " select id || '.conv.1', id, 1, conversation_id,"
                " case when phase in ('stopped', 'finished', 'failed') then 'ended'"
                " else 'active' end, 'launch', launched_at, launched_at, launched_at"
                " from runs where conversation_id is not null"
            )
        problems = db.execute("pragma foreign_key_check").fetchall()
        if problems:
            raise StateError(f"schema migration broke foreign keys: {problems}")
        db.execute(f"pragma user_version = {SCHEMA_VERSION}")
    except BaseException:
        db.execute("rollback")
        raise
    db.execute("commit")


def _migrate_to_v2(db: sqlite3.Connection) -> None:
    has_runs = db.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'runs'"
    ).fetchone()
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


class StateStore:
    """Thread-safe: the control socket, the reconcile loop and the deadline watchdog
    share one store.

    The controller calls `acquire_writer` before anything else. From then on every
    write checks that it still holds the lock, so a controller that lost it cannot
    change lifecycle state. A store that never acquired it (tests, tooling) is not
    fenced.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._writer_token: str | None = None
        self._lock_fd: int | None = None
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
            self._release_file_lock()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("begin immediate")
            try:
                if self._writer_token is not None:
                    self._check_writer(self._db)
                yield self._db
            except BaseException:
                self._db.execute("rollback")
                raise
            self._db.execute("commit")

    # --- the writer lock -----------------------------------------------------------

    @property
    def lock_path(self) -> Path | None:
        return None if self._path == ":memory:" else Path(self._path).with_suffix(".lock")

    def acquire_writer(
        self, *, pid: int, host: str, boot_id: str, now: datetime
    ) -> WriterHolder | None:
        """Become the only controller writing this store. Returns the previous holder.

        Liveness comes from an exclusive flock on a file next to the database: the
        kernel releases it when the holder dies, however it dies, so a restarted
        controller takes over at once, while a second live controller is refused.
        The lock row records who holds it (and fences writes, see `_tx`); its boot
        id tells a restart of the controller from a restart of the DGX.
        """
        path = self.lock_path
        if path is not None and self._lock_fd is None:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                holder = self.writer_holder()
                who = (
                    f"pid {holder.pid} on {holder.host} since {holder.acquired_at.isoformat()}"
                    if holder
                    else "an unknown process"
                )
                raise WriterLockError(f"another controller is running ({who})") from None
            self._lock_fd = fd
        token = uuid.uuid4().hex
        with self._lock:
            self._db.execute("begin immediate")
            try:
                row = self._db.execute("select * from controller_lock where id = 1").fetchone()
                self._db.execute(
                    "insert or replace into controller_lock"
                    " (id, token, pid, host, boot_id, acquired_at) values (1, ?, ?, ?, ?, ?)",
                    (token, pid, host, boot_id, _iso(now)),
                )
            except BaseException:
                self._db.execute("rollback")
                raise
            self._db.execute("commit")
            self._writer_token = token
        return _holder(row) if row else None

    def writer_holder(self) -> WriterHolder | None:
        with self._lock:
            row = self._db.execute("select * from controller_lock where id = 1").fetchone()
        return _holder(row) if row else None

    def _check_writer(self, db: sqlite3.Connection) -> None:
        row = db.execute("select token, pid, host from controller_lock where id = 1").fetchone()
        if row is None or row["token"] != self._writer_token:
            who = f"pid {row['pid']} on {row['host']}" if row else "nobody"
            raise WriterLockError(f"this controller lost the writer lock (now held by {who})")

    def _release_file_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing the descriptor drops the flock
            self._lock_fd = None

    # --- runs ----------------------------------------------------------------------

    def create_run(
        self,
        *,
        run_id: str,
        model_key: str,
        launched_at: datetime,
        deadline_at: datetime,
        brief_path: str,
        frozen_digest: str | None = None,
        criteria: Sequence[Mapping[str, Any]] = (),
    ) -> Run:
        """The run and its frozen criteria, in one transaction."""
        with self._tx() as db:
            self._insert_run(
                db,
                run_id=run_id,
                model_key=model_key,
                launched_at=launched_at,
                deadline_at=deadline_at,
                brief_path=brief_path,
                frozen_digest=frozen_digest,
                criteria=criteria,
            )
        return self._require_run(run_id)

    @staticmethod
    def _insert_run(
        db: sqlite3.Connection,
        *,
        run_id: str,
        model_key: str,
        launched_at: datetime,
        deadline_at: datetime,
        brief_path: str,
        frozen_digest: str | None,
        criteria: Sequence[Mapping[str, Any]],
    ) -> None:
        if deadline_at <= launched_at:
            raise StateError("deadline must be after launch")
        db.execute(
            "insert into runs (id, phase, model_key, launched_at, deadline_at, brief_path,"
            " frozen_digest) values (?, 'launched', ?, ?, ?, ?, ?)",
            (run_id, model_key, _iso(launched_at), _iso(deadline_at), brief_path, frozen_digest),
        )
        for i, c in enumerate(criteria):
            db.execute(
                "insert into criteria (run_id, key, position, kind, required, description,"
                " test, runner) values (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    c["key"],
                    i,
                    c["kind"],
                    int(bool(c.get("required", True))),
                    c["description"],
                    c.get("test"),
                    c.get("runner"),
                ),
            )

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

    def retry_operation(
        self, op_id: str, now: datetime, *, resource_id: str | None = None
    ) -> Operation:
        """Start a new attempt of an operation that is still intended.

        Startup reconciliation uses this after inspecting what the interrupted attempt
        left behind: `resource_id` is what it observed (None when nothing exists), and
        the attempt's timeouts count from `now`, not from before the downtime.
        """
        with self._tx() as db:
            db.execute(
                "update operations set resource_id = ?, attempted_at = ?"
                " where id = ? and status = 'intended'",
                (resource_id, _iso(now), op_id),
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

    # --- recoveries ----------------------------------------------------------------

    def begin_recovery(
        self, run_id: str, *, cause: str, status_before: str | None, now: datetime
    ) -> Recovery:
        """Persist the intent to bring a run's resources back, before touching them.

        Returns the open recovery instead if the run already has one.
        """
        with self._tx() as db:
            open_row = db.execute(
                "select id from recoveries where run_id = ? and status = 'intended'", (run_id,)
            ).fetchone()
            if open_row is not None:
                rec_id = str(open_row["id"])
            else:
                n = int(
                    db.execute(
                        "select coalesce(max(n), 0) + 1 from recoveries where run_id = ?",
                        (run_id,),
                    ).fetchone()[0]
                )
                rec_id = f"{run_id}.recover.{n}"
                db.execute(
                    "insert into recoveries (id, run_id, n, cause, status, status_before,"
                    " started_at, attempted_at) values (?, ?, ?, ?, 'intended', ?, ?, ?)",
                    (rec_id, run_id, n, cause, status_before, _iso(now), _iso(now)),
                )
        return self._require_recovery(rec_id)

    def recoveries(self, run_id: str) -> list[Recovery]:
        with self._lock:
            rows = self._db.execute(
                "select * from recoveries where run_id = ? order by n", (run_id,)
            ).fetchall()
        return [_recovery(r) for r in rows]

    def open_recovery(self, run_id: str) -> Recovery | None:
        with self._lock:
            row = self._db.execute(
                "select * from recoveries where run_id = ? and status = 'intended'", (run_id,)
            ).fetchone()
        return _recovery(row) if row else None

    def record_recovery_steps(self, rec_id: str, steps: dict[str, Any]) -> Recovery:
        with self._tx() as db:
            db.execute(
                "update recoveries set steps = ? where id = ? and status = 'intended'",
                (json.dumps(steps, sort_keys=True), rec_id),
            )
        return self._require_recovery(rec_id)

    def retry_recovery(self, rec_id: str, now: datetime) -> Recovery:
        """A new attempt of an open recovery (the controller restarted in the middle).

        What the interrupted attempt did is inspected again rather than trusted, so its
        steps are cleared; cause and status_before are kept.
        """
        with self._tx() as db:
            db.execute(
                "update recoveries set steps = '{}', attempted_at = ?"
                " where id = ? and status = 'intended'",
                (_iso(now), rec_id),
            )
        return self._require_recovery(rec_id)

    def finish_recovery(
        self, rec_id: str, *, status: Literal["done", "failed"], error: str | None, now: datetime
    ) -> Recovery:
        with self._tx() as db:
            db.execute(
                "update recoveries set status = ?, error = ?, finished_at = ?"
                " where id = ? and status = 'intended'",
                (status, error, _iso(now), rec_id),
            )
        return self._require_recovery(rec_id)

    def _require_recovery(self, rec_id: str) -> Recovery:
        with self._lock:
            row = self._db.execute("select * from recoveries where id = ?", (rec_id,)).fetchone()
        if row is None:
            raise StateError(f"no recovery {rec_id}")
        return _recovery(row)

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

    def demos_in_state(self, state: DemoState) -> list[Demo]:
        with self._lock:
            rows = self._db.execute(
                "select * from demos where state = ? order by run_id", (state,)
            ).fetchall()
        return [_demo(r) for r in rows]

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

    def set_demo_snapshot(self, run_id: str, snapshot_id: str | None) -> Demo:
        """The project snapshot the demo was (re)started from."""
        with self._tx() as db:
            db.execute("update demos set snapshot_id = ? where run_id = ?", (snapshot_id, run_id))
        return self._require_demo(run_id)

    # --- criteria and evaluations --------------------------------------------------

    def criteria(self, run_id: str) -> list[CriterionRow]:
        with self._lock:
            rows = self._db.execute(
                "select * from criteria where run_id = ? order by position", (run_id,)
            ).fetchall()
        return [_criterion(r) for r in rows]

    def begin_evaluation(
        self,
        run_id: str,
        *,
        trigger: EvaluationTrigger,
        check_digest: str,
        evidence_root: str,
        now: datetime,
        claim_event_id: str | None = None,
        claim_text: str | None = None,
    ) -> Evaluation:
        """Persist the intent to evaluate before anything runs. Returns the open
        evaluation instead if the run already has one."""
        with self._tx() as db:
            row = db.execute(
                "select id from evaluations where run_id = ? and status = 'intended'", (run_id,)
            ).fetchone()
            if row is not None:
                ev_id = str(row["id"])
            else:
                n = int(
                    db.execute(
                        "select coalesce(max(n), 0) + 1 from evaluations where run_id = ?",
                        (run_id,),
                    ).fetchone()[0]
                )
                ev_id = f"{run_id}.eval.{n}"
                db.execute(
                    "insert into evaluations (id, run_id, n, trigger, claim_event_id, claim_text,"
                    " check_digest, status, evidence_dir, started_at, attempted_at)"
                    " values (?, ?, ?, ?, ?, ?, ?, 'intended', ?, ?, ?)",
                    (
                        ev_id,
                        run_id,
                        n,
                        trigger,
                        claim_event_id,
                        claim_text,
                        check_digest,
                        f"{evidence_root.rstrip('/')}/eval-{n}",
                        _iso(now),
                        _iso(now),
                    ),
                )
        return self._require_evaluation(ev_id)

    def evaluations(self, run_id: str) -> list[Evaluation]:
        with self._lock:
            rows = self._db.execute(
                "select * from evaluations where run_id = ? order by n", (run_id,)
            ).fetchall()
        return [_evaluation(r) for r in rows]

    def open_evaluation(self, run_id: str) -> Evaluation | None:
        with self._lock:
            row = self._db.execute(
                "select * from evaluations where run_id = ? and status = 'intended'", (run_id,)
            ).fetchone()
        return _evaluation(row) if row else None

    def open_evaluations(self) -> list[Evaluation]:
        with self._lock:
            rows = self._db.execute(
                "select * from evaluations where status = 'intended' order by run_id, n"
            ).fetchall()
        return [_evaluation(r) for r in rows]

    def record_evaluation_steps(self, ev_id: str, steps: dict[str, Any]) -> Evaluation:
        with self._tx() as db:
            db.execute(
                "update evaluations set steps = ? where id = ? and status = 'intended'",
                (json.dumps(steps, sort_keys=True), ev_id),
            )
        return self._require_evaluation(ev_id)

    def pin_evaluation(self, ev_id: str, *, snapshot_id: str, snapshot_commit: str) -> Evaluation:
        """The project snapshot this evaluation checks. Written once per attempt."""
        with self._tx() as db:
            db.execute(
                "update evaluations set workspace_snapshot_id = ?, snapshot_commit = ?"
                " where id = ? and status = 'intended' and workspace_snapshot_id is null",
                (snapshot_id, snapshot_commit, ev_id),
            )
        return self._require_evaluation(ev_id)

    def retry_evaluation(self, ev_id: str, now: datetime) -> Evaluation:
        """Start an open evaluation over (the controller restarted in the middle).

        Nothing it did is trusted: steps, snapshot and results are cleared, the
        evaluator containers are removed by the caller, and it runs again.
        """
        with self._tx() as db:
            db.execute(
                "update evaluations set steps = '{}', results = '{}', workspace_snapshot_id = null,"
                " snapshot_commit = null, attempted_at = ? where id = ? and status = 'intended'",
                (_iso(now), ev_id),
            )
        return self._require_evaluation(ev_id)

    def finish_evaluation(
        self,
        ev_id: str,
        *,
        status: EvaluationResult,
        detail: str | None,
        results: Mapping[str, Mapping[str, Any]],
        now: datetime,
    ) -> Evaluation:
        """Record the outcome and point each reported criterion at it."""
        with self._tx() as db:
            row = db.execute(
                "select run_id, status from evaluations where id = ?", (ev_id,)
            ).fetchone()
            if row is None:
                raise StateError(f"no evaluation {ev_id}")
            if row["status"] == "intended":
                db.execute(
                    "update evaluations set status = ?, detail = ?, results = ?, finished_at = ?"
                    " where id = ?",
                    (status, detail, json.dumps(results, sort_keys=True), _iso(now), ev_id),
                )
                for key, result in results.items():
                    db.execute(
                        "update criteria set latest_evaluation_id = ?, latest_status = ?"
                        " where run_id = ? and key = ?",
                        (ev_id, str(result.get("status")), row["run_id"], key),
                    )
        return self._require_evaluation(ev_id)

    def mark_delivered(self, ev_id: str, now: datetime) -> Evaluation:
        with self._tx() as db:
            db.execute(
                "update evaluations set delivered_at = coalesce(delivered_at, ?) where id = ?",
                (_iso(now), ev_id),
            )
        return self._require_evaluation(ev_id)

    def _require_evaluation(self, ev_id: str) -> Evaluation:
        with self._lock:
            row = self._db.execute("select * from evaluations where id = ?", (ev_id,)).fetchone()
        if row is None:
            raise StateError(f"no evaluation {ev_id}")
        return _evaluation(row)

    # --- manual reviews ------------------------------------------------------------

    def request_review(
        self,
        run_id: str,
        *,
        note: str | None,
        evaluation_id: str | None,
        snapshot_commit: str | None,
        bundle_root: str,
        now: datetime,
    ) -> Review:
        with self._tx() as db:
            n = int(
                db.execute(
                    "select coalesce(max(n), 0) + 1 from reviews where run_id = ?", (run_id,)
                ).fetchone()[0]
            )
            review_id = f"{run_id}.review.{n}"
            db.execute(
                "insert into reviews (id, run_id, n, status, note, evaluation_id, snapshot_commit,"
                " bundle_dir, requested_at) values (?, ?, ?, 'requested', ?, ?, ?, ?, ?)",
                (
                    review_id,
                    run_id,
                    n,
                    note,
                    evaluation_id,
                    snapshot_commit,
                    f"{bundle_root.rstrip('/')}/review-{n}",
                    _iso(now),
                ),
            )
        return self._require_review(review_id)

    def record_review(
        self, run_id: str, n: int, *, reviewer: str, result_path: str, now: datetime
    ) -> Review:
        review_id = f"{run_id}.review.{n}"
        with self._tx() as db:
            db.execute(
                "update reviews set status = 'recorded', reviewer = ?, result_path = ?,"
                " recorded_at = ? where id = ?",
                (reviewer, result_path, _iso(now), review_id),
            )
        return self._require_review(review_id)

    def reviews(self, run_id: str) -> list[Review]:
        with self._lock:
            rows = self._db.execute(
                "select * from reviews where run_id = ? order by n", (run_id,)
            ).fetchall()
        return [_review(r) for r in rows]

    def get_review(self, run_id: str, n: int) -> Review | None:
        with self._lock:
            row = self._db.execute(
                "select * from reviews where id = ?", (f"{run_id}.review.{n}",)
            ).fetchone()
        return _review(row) if row else None

    def _require_review(self, review_id: str) -> Review:
        with self._lock:
            row = self._db.execute("select * from reviews where id = ?", (review_id,)).fetchone()
        if row is None:
            raise StateError(f"no review {review_id}")
        return _review(row)

    # --- conversations and checkpoints ---------------------------------------------

    def record_first_conversation(
        self, run_id: str, conversation_id: str, now: datetime
    ) -> ConversationRow:
        """The run's first conversation (from the launch step). Idempotent."""
        with self._tx() as db:
            db.execute(
                "insert or ignore into conversations (id, run_id, n, conversation_id, status,"
                " reason, started_at, attempted_at, active_at)"
                " values (?, ?, 1, ?, 'active', 'launch', ?, ?, ?)",
                (f"{run_id}.conv.1", run_id, conversation_id, _iso(now), _iso(now), _iso(now)),
            )
        return self._require_conversation(f"{run_id}.conv.1")

    def conversations(self, run_id: str) -> list[ConversationRow]:
        with self._lock:
            rows = self._db.execute(
                "select * from conversations where run_id = ? order by n", (run_id,)
            ).fetchall()
        return [_conversation(r) for r in rows]

    def open_rollover(self, run_id: str) -> ConversationRow | None:
        with self._lock:
            row = self._db.execute(
                "select * from conversations where run_id = ? and status in"
                " ('handoff', 'starting')",
                (run_id,),
            ).fetchone()
        return _conversation(row) if row else None

    def begin_rollover(
        self,
        run_id: str,
        *,
        conversation_id_for_n: Any,
        reason: str,
        detail: str | None,
        now: datetime,
    ) -> ConversationRow:
        """Persist the intent to replace the run's conversation before anything is sent.

        Returns the open rollover instead if there is one. `conversation_id_for_n`
        derives the new conversation's id from its number, so a retried start after
        a crash attaches to the same conversation.
        """
        with self._tx() as db:
            row = db.execute(
                "select id from conversations where run_id = ? and status in"
                " ('handoff', 'starting')",
                (run_id,),
            ).fetchone()
            if row is not None:
                conv_id = str(row["id"])
            else:
                n = int(
                    db.execute(
                        "select coalesce(max(n), 0) + 1 from conversations where run_id = ?",
                        (run_id,),
                    ).fetchone()[0]
                )
                conv_id = f"{run_id}.conv.{n}"
                db.execute(
                    "insert into conversations (id, run_id, n, conversation_id, status, reason,"
                    " detail, started_at, attempted_at) values (?, ?, ?, ?, 'handoff', ?, ?, ?, ?)",
                    (
                        conv_id,
                        run_id,
                        n,
                        str(conversation_id_for_n(n)),
                        reason,
                        detail,
                        _iso(now),
                        _iso(now),
                    ),
                )
        return self._require_conversation(conv_id)

    def note_handoff_request(self, conv_id: str, request_id: str, now: datetime) -> ConversationRow:
        with self._tx() as db:
            db.execute(
                "update conversations set handoff_request_id = ?,"
                " handoff_attempts = handoff_attempts + 1, attempted_at = ?"
                " where id = ? and status = 'handoff'",
                (request_id, _iso(now), conv_id),
            )
        return self._require_conversation(conv_id)

    def retry_rollover(self, conv_id: str, now: datetime) -> ConversationRow:
        """A controller restart: the attempt's timeouts count from now."""
        with self._tx() as db:
            db.execute(
                "update conversations set attempted_at = ? where id = ?"
                " and status in ('handoff', 'starting')",
                (_iso(now), conv_id),
            )
        return self._require_conversation(conv_id)

    def record_checkpoint(
        self,
        conv_id: str,
        *,
        from_conversation_id: str,
        event_position: int | None,
        workspace_sha: str | None,
        source: CheckpointSource,
        reason: str,
        handoff: Mapping[str, Any],
        verified: Mapping[str, Any],
        problems: Sequence[str],
        now: datetime,
    ) -> Checkpoint:
        """The checkpoint a rollover starts from, in one transaction with the rollover
        moving on (`starting`) and the run pointing at it. Newer checkpoints supersede
        older ones; nothing older is changed."""
        with self._tx() as db:
            row = db.execute(
                "select run_id, status from conversations where id = ?", (conv_id,)
            ).fetchone()
            if row is None or row["status"] != "handoff":
                raise StateError(f"conversation {conv_id} is not waiting for a handoff")
            run_id = str(row["run_id"])
            current = db.execute(
                "select current_checkpoint_id from runs where id = ?", (run_id,)
            ).fetchone()
            n = int(
                db.execute(
                    "select coalesce(max(n), 0) + 1 from checkpoints where run_id = ?", (run_id,)
                ).fetchone()[0]
            )
            ckpt_id = f"{run_id}.ckpt.{n}"
            db.execute(
                "insert into checkpoints (id, run_id, n, conversation_id, event_position,"
                " workspace_sha, supersedes_id, source, reason, handoff, verified, problems,"
                " created_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ckpt_id,
                    run_id,
                    n,
                    from_conversation_id,
                    event_position,
                    workspace_sha,
                    current["current_checkpoint_id"] if current else None,
                    source,
                    reason,
                    json.dumps(handoff, sort_keys=True),
                    json.dumps(verified, sort_keys=True),
                    json.dumps(list(problems)),
                    _iso(now),
                ),
            )
            db.execute("update runs set current_checkpoint_id = ? where id = ?", (ckpt_id, run_id))
            db.execute(
                "update conversations set status = 'starting', from_checkpoint_id = ?,"
                " attempted_at = ? where id = ?",
                (ckpt_id, _iso(now), conv_id),
            )
            db.execute(
                "update conversations set status = 'ended', ended_at = ?"
                " where run_id = ? and status = 'active'",
                (_iso(now), run_id),
            )
        return self._require_checkpoint(ckpt_id)

    def activate_conversation(self, conv_id: str, now: datetime) -> Run:
        """The new conversation has started: it is the run's conversation from now on."""
        with self._tx() as db:
            row = db.execute(
                "select run_id, conversation_id, status from conversations where id = ?",
                (conv_id,),
            ).fetchone()
            if row is None or row["status"] != "starting":
                raise StateError(f"conversation {conv_id} is not starting")
            db.execute(
                "update conversations set status = 'active', active_at = ? where id = ?",
                (_iso(now), conv_id),
            )
            db.execute(
                "update runs set conversation_id = ? where id = ?",
                (row["conversation_id"], row["run_id"]),
            )
        return self._require_run(str(row["run_id"]))

    def abandon_rollover(self, run_id: str, detail: str, now: datetime) -> None:
        """Give an open rollover up (the run is ending). A rollover that already
        recorded its checkpoint keeps it; the previous conversation stays the run's."""
        with self._tx() as db:
            db.execute(
                "update conversations set status = 'abandoned', ended_at = ?,"
                " detail = coalesce(detail || '; ', '') || ?"
                " where run_id = ? and status in ('handoff', 'starting')",
                (_iso(now), detail, run_id),
            )

    def checkpoints(self, run_id: str) -> list[Checkpoint]:
        with self._lock:
            rows = self._db.execute(
                "select * from checkpoints where run_id = ? order by n", (run_id,)
            ).fetchall()
        return [_checkpoint(r) for r in rows]

    def get_checkpoint(self, ckpt_id: str) -> Checkpoint | None:
        with self._lock:
            row = self._db.execute("select * from checkpoints where id = ?", (ckpt_id,)).fetchone()
        return _checkpoint(row) if row else None

    def record_blocked(self, run_id: str, blocker_json: str) -> Run:
        """The confirmed blocker. Persisted before the stop that ends the run."""
        with self._tx() as db:
            db.execute(
                "update runs set blocked = coalesce(blocked, ?) where id = ?",
                (blocker_json, run_id),
            )
        return self._require_run(run_id)

    def _require_conversation(self, conv_id: str) -> ConversationRow:
        with self._lock:
            row = self._db.execute(
                "select * from conversations where id = ?", (conv_id,)
            ).fetchone()
        if row is None:
            raise StateError(f"no conversation {conv_id}")
        return _conversation(row)

    def _require_checkpoint(self, ckpt_id: str) -> Checkpoint:
        ckpt = self.get_checkpoint(ckpt_id)
        if ckpt is None:
            raise StateError(f"no checkpoint {ckpt_id}")
        return ckpt

    # --- planning ------------------------------------------------------------------

    def create_plan(self, *, plan_id: str, model_key: str, request: str, now: datetime) -> Plan:
        with self._tx() as db:
            db.execute(
                "insert into plans (id, state, model_key, request, created_at, updated_at)"
                " values (?, 'starting', ?, ?, ?, ?)",
                (plan_id, model_key, request, _iso(now), _iso(now)),
            )
        return self._require_plan(plan_id)

    def get_plan(self, plan_id: str) -> Plan | None:
        with self._lock:
            row = self._db.execute("select * from plans where id = ?", (plan_id,)).fetchone()
        return _plan(row) if row else None

    def plan_for_run(self, run_id: str) -> Plan | None:
        with self._lock:
            row = self._db.execute("select * from plans where run_id = ?", (run_id,)).fetchone()
        return _plan(row) if row else None

    def list_plans(self) -> list[Plan]:
        with self._lock:
            rows = self._db.execute("select * from plans order by created_at, rowid").fetchall()
        return [_plan(r) for r in rows]

    def active_plans(self) -> list[Plan]:
        return [p for p in self.list_plans() if p.active]

    def set_plan_state(
        self, plan_id: str, state: PlanState, now: datetime, *, error: str | None = None
    ) -> Plan:
        """Move an active plan on. A plan that was launched, closed or failed stays so."""
        with self._tx() as db:
            db.execute(
                "update plans set state = ?, error = ?, updated_at = ?"
                " where id = ? and state in ('starting', 'open')",
                (state, error, _iso(now), plan_id),
            )
        return self._require_plan(plan_id)

    def set_plan_conversation(self, plan_id: str, conversation_id: str, now: datetime) -> Plan:
        with self._tx() as db:
            row = db.execute(
                "select conversation_id from plans where id = ?", (plan_id,)
            ).fetchone()
            if row is None:
                raise StateError(f"no plan {plan_id}")
            if row["conversation_id"] not in (None, conversation_id):
                raise StateError(f"plan {plan_id} already has conversation {row[0]}")
            db.execute(
                "update plans set conversation_id = ?, updated_at = ? where id = ?",
                (conversation_id, _iso(now), plan_id),
            )
        return self._require_plan(plan_id)

    def record_dry_run(self, plan_id: str, result: Mapping[str, Any], now: datetime) -> Plan:
        with self._tx() as db:
            db.execute(
                "update plans set dry_run = ?, updated_at = ? where id = ?",
                (json.dumps(result, sort_keys=True), _iso(now), plan_id),
            )
        return self._require_plan(plan_id)

    def launch_plan(
        self,
        plan_id: str,
        *,
        run_id: str,
        model_key: str,
        launched_at: datetime,
        deadline_at: datetime,
        brief_path: str,
        frozen_digest: str,
        criteria: Sequence[Mapping[str, Any]],
    ) -> Run:
        """The run a plan becomes, in one transaction with the plan's `launched` state.

        Only an open plan launches, and only once.
        """
        with self._tx() as db:
            row = db.execute("select state from plans where id = ?", (plan_id,)).fetchone()
            if row is None:
                raise StateError(f"no plan {plan_id}")
            if row["state"] != "open":
                raise StateError(f"plan {plan_id} is {row['state']}; only an open plan launches")
            self._insert_run(
                db,
                run_id=run_id,
                model_key=model_key,
                launched_at=launched_at,
                deadline_at=deadline_at,
                brief_path=brief_path,
                frozen_digest=frozen_digest,
                criteria=criteria,
            )
            db.execute(
                "update plans set state = 'launched', run_id = ?, launched_digest = ?,"
                " updated_at = ? where id = ?",
                (run_id, frozen_digest, _iso(launched_at), plan_id),
            )
        return self._require_run(run_id)

    def _require_plan(self, plan_id: str) -> Plan:
        plan = self.get_plan(plan_id)
        if plan is None:
            raise StateError(f"no plan {plan_id}")
        return plan

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
        frozen_digest=row["frozen_digest"],
        current_checkpoint_id=row["current_checkpoint_id"],
        blocked=row["blocked"],
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
        attempted_at=_parse(row["attempted_at"] or row["created_at"]),
    )


def _recovery(row: sqlite3.Row) -> Recovery:
    return Recovery(
        id=row["id"],
        run_id=row["run_id"],
        n=int(row["n"]),
        cause=row["cause"],
        status=cast(RecoveryStatus, row["status"]),
        status_before=row["status_before"],
        steps=json.loads(row["steps"] or "{}"),
        error=row["error"],
        started_at=_parse(row["started_at"]),
        attempted_at=_parse(row["attempted_at"]),
        finished_at=_parse(row["finished_at"]) if row["finished_at"] else None,
    )


def _holder(row: sqlite3.Row) -> WriterHolder:
    return WriterHolder(
        token=row["token"],
        pid=int(row["pid"]),
        host=row["host"],
        boot_id=row["boot_id"],
        acquired_at=_parse(row["acquired_at"]),
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
        snapshot_id=row["snapshot_id"],
    )


def _criterion(row: sqlite3.Row) -> CriterionRow:
    return CriterionRow(
        run_id=row["run_id"],
        key=row["key"],
        position=int(row["position"]),
        kind=row["kind"],
        required=bool(row["required"]),
        description=row["description"],
        test=row["test"],
        runner=row["runner"],
        latest_evaluation_id=row["latest_evaluation_id"],
        latest_status=row["latest_status"],
    )


def _evaluation(row: sqlite3.Row) -> Evaluation:
    return Evaluation(
        id=row["id"],
        run_id=row["run_id"],
        n=int(row["n"]),
        trigger=cast(EvaluationTrigger, row["trigger"]),
        claim_event_id=row["claim_event_id"],
        claim_text=row["claim_text"],
        check_digest=row["check_digest"],
        snapshot_id=row["workspace_snapshot_id"],
        snapshot_commit=row["snapshot_commit"],
        status=cast(EvaluationStatus, row["status"]),
        detail=row["detail"],
        steps=json.loads(row["steps"] or "{}"),
        results=json.loads(row["results"] or "{}"),
        evidence_dir=row["evidence_dir"],
        started_at=_parse(row["started_at"]),
        attempted_at=_parse(row["attempted_at"]),
        finished_at=_parse(row["finished_at"]) if row["finished_at"] else None,
        delivered_at=_parse(row["delivered_at"]) if row["delivered_at"] else None,
    )


def _review(row: sqlite3.Row) -> Review:
    return Review(
        id=row["id"],
        run_id=row["run_id"],
        n=int(row["n"]),
        status=cast(ReviewStatus, row["status"]),
        note=row["note"],
        evaluation_id=row["evaluation_id"],
        snapshot_commit=row["snapshot_commit"],
        bundle_dir=row["bundle_dir"],
        requested_at=_parse(row["requested_at"]),
        reviewer=row["reviewer"],
        result_path=row["result_path"],
        recorded_at=_parse(row["recorded_at"]) if row["recorded_at"] else None,
    )


def _plan(row: sqlite3.Row) -> Plan:
    return Plan(
        id=row["id"],
        state=cast(PlanState, row["state"]),
        model_key=row["model_key"],
        request=row["request"],
        created_at=_parse(row["created_at"]),
        updated_at=_parse(row["updated_at"]),
        conversation_id=row["conversation_id"],
        error=row["error"],
        dry_run=json.loads(row["dry_run"]) if row["dry_run"] else None,
        run_id=row["run_id"],
        launched_digest=row["launched_digest"],
    )


def _conversation(row: sqlite3.Row) -> ConversationRow:
    return ConversationRow(
        id=row["id"],
        run_id=row["run_id"],
        n=int(row["n"]),
        conversation_id=row["conversation_id"],
        status=cast(ConversationRowStatus, row["status"]),
        reason=row["reason"],
        detail=row["detail"],
        from_checkpoint_id=row["from_checkpoint_id"],
        handoff_request_id=row["handoff_request_id"],
        handoff_attempts=int(row["handoff_attempts"]),
        started_at=_parse(row["started_at"]),
        attempted_at=_parse(row["attempted_at"]),
        active_at=_parse(row["active_at"]) if row["active_at"] else None,
        ended_at=_parse(row["ended_at"]) if row["ended_at"] else None,
    )


def _checkpoint(row: sqlite3.Row) -> Checkpoint:
    return Checkpoint(
        id=row["id"],
        run_id=row["run_id"],
        n=int(row["n"]),
        conversation_id=row["conversation_id"],
        event_position=row["event_position"],
        workspace_sha=row["workspace_sha"],
        supersedes_id=row["supersedes_id"],
        source=cast(CheckpointSource, row["source"]),
        reason=row["reason"],
        handoff=json.loads(row["handoff"]),
        verified=json.loads(row["verified"]),
        problems=json.loads(row["problems"] or "[]"),
        created_at=_parse(row["created_at"]),
    )
